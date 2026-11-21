"""Phase G：15 分钟 state worker（B → C → D → E，可选 F）。

systemd timer 每 15 分钟触发一次 ``--once``，本模块只负责**单轮编排**，业务逻辑全部
复用既有 Phase B–F 实现，不重写：

1. ``run_video_route(use_db=True)``          → round + video_metric（Phase B）
2. ``run_window_sample(round_id=..., use_db=True)`` → WindowSample（Phase C）
3. ``run_pool_round(sample, use_db=True, round_id=...)`` → pool1/pool2（Phase D）
4. ``report_from_db`` / ``report_from_result`` → 原子写 ``HOTMEME_MEME_INFO_REPORT``（Phase E）
5. ``MEME_INFO_VERIFY=1`` 时才 ``run_audit_round``（Phase F，默认关闭）

保护：

- **单写者文件锁**（``fcntl.flock``，锁文件默认 ``var/state_worker.lock``）：拿不到锁说明
  上一轮还没跑完（timer 重叠/手工触发），本轮直接 ``skipped_overlap`` 并以 0 退出；
- **报告原子写**：失败不破坏旧报告；``degraded`` 轮仍写新报告（``status=degraded``），
  并在覆盖前把上一份 ok 报告备份为 ``<report>.last_good.json``；
- **失败退出码非 0**（1），不删除旧报告；异常栈写日志（连接串密码自动脱敏）。

用法::

    # 手工跑一轮（部署验证/影子运行）；systemd timer 也走这条
    python -m tools.state_worker --once

    # 只看流程不落盘：DB 事务最后回滚，不写报告、不跑审核
    python -m tools.state_worker --once --dry-run

    # 纯内存（不读也不写 Postgres）：--no-db 默认不写生产报告，给 --output 才落盘
    python -m tools.state_worker --no-db --output /tmp/report.json --json

    # 机器可读摘要（stdout 只有 JSON；日志仍写 stderr/日志文件）
    python -m tools.state_worker --once --json

环境：

- ``HOTMEME_MEME_INFO_REPORT``：报告路径（回退 ``MEME_INFO_REPORT``，再回退 ``var/meme_info_latest.json``）
- ``HOTMEME_STATE_WORKER_LOG``：日志路径（默认 ``var/state_worker.log``）
- ``HOTMEME_STATE_WORKER_LOCK``：锁文件路径（默认 ``var/state_worker.lock``）
- ``DATABASE_URL`` / ``ADMIN_DATABASE_URL``：worker/admin 角色连接串（systemd EnvironmentFile）
- ``MEME_INFO_VERIFY=1``：启用 Phase F LLM 审核；默认 0

退出码：0 = ok/degraded/skipped_overlap；1 = 运行失败；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

# 直接 `python tools/state_worker.py` 时 sys.path[0] 是 tools/，会被 tools/meme_info.py
# 遮蔽包 meme_info；显式把仓库根放最前。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meme_info import state as state_mod  # noqa: E402
from meme_info.audit import run_audit_round  # noqa: E402
from meme_info.models import iso, now_ts  # noqa: E402
from meme_info.pools import PoolConfig, run_pool_round  # noqa: E402
from meme_info.report import (  # noqa: E402
    report_from_db,
    report_from_result,
    write_report,
)
from meme_info.video_route import VideoRoutePlan, run_video_route  # noqa: E402
from meme_info.window import WindowSamplePlan, run_window_sample  # noqa: E402

# ---------------------------------------------------------------- 常量
SCHEMA_VERSION = "state-worker/v1"
REPORT_SCHEMA_V2 = "meme-info/v2"

REPORT_ENV = "HOTMEME_MEME_INFO_REPORT"
LEGACY_REPORT_ENV = "MEME_INFO_REPORT"
LOG_ENV = "HOTMEME_STATE_WORKER_LOG"
LOCK_ENV = "HOTMEME_STATE_WORKER_LOCK"
VERIFY_ENV = "MEME_INFO_VERIFY"

DEFAULT_REPORT = REPO_ROOT / "var" / "meme_info_latest.json"
DEFAULT_LOG = REPO_ROOT / "var" / "state_worker.log"
DEFAULT_LOCK = REPO_ROOT / "var" / "state_worker.lock"
LAST_GOOD_SUFFIX = ".last_good.json"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: 连接串密码脱敏（日志里绝不出现数据库密码）
_DSN_PASSWORD_RE = re.compile(r"(postgres(?:ql)?://[^:@/\s]+):([^@\s]+)@")


class WorkerError(RuntimeError):
    """单轮编排失败（会转成退出码 1）。"""


# ---------------------------------------------------------------- 环境 / 路径
def _env_truthy(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def report_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(
        os.environ.get(REPORT_ENV)
        or os.environ.get(LEGACY_REPORT_ENV)
        or DEFAULT_REPORT
    )


def log_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(os.environ.get(LOG_ENV) or DEFAULT_LOG)


def lock_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(os.environ.get(LOCK_ENV) or DEFAULT_LOCK)


def last_good_path(path: str | Path) -> Path:
    """``var/meme_info_latest.json`` → ``var/meme_info_latest.last_good.json``。"""
    target = Path(path)
    return target.with_name(target.stem + LAST_GOOD_SUFFIX)


def redact(text: Any) -> str:
    """把连接串里的密码替换为 ``***``（日志/摘要安全）。"""
    return _DSN_PASSWORD_RE.sub(r"\1:***@", str(text))


# ---------------------------------------------------------------- 日志
class WorkerLog:
    """stderr + 文件双写日志；文件失败不致命（journald 仍有 stderr）。"""

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        self._handle: Any = None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(self.path, "a", encoding="utf-8")
            except OSError as exc:  # 日志文件不可写不阻塞 worker
                self.path = None
                self._handle = None
                print(
                    f"state_worker: 无法打开日志文件（改用 stderr）：{type(exc).__name__}",
                    file=sys.stderr,
                )

    def __call__(self, message: Any, *, level: str = "info") -> None:
        line = f"{iso(now_ts())} [{level}] {redact(message)}"
        print(line, file=sys.stderr)
        if self._handle is not None:
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except OSError:  # pragma: no cover - 磁盘满等极端情况
                pass

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:  # pragma: no cover
                pass
            self._handle = None


# ---------------------------------------------------------------- 单写者锁
@contextmanager
def single_writer_lock(path: str | Path) -> Iterator[bool]:
    """非阻塞独占文件锁；``yield False`` 表示已有实例在跑（本轮跳过）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(target, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} started_at={iso(now_ts())}\n")
            handle.flush()
            yield True
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover
                pass
    finally:
        handle.close()


# ---------------------------------------------------------------- 报告 backup / 写
def backup_last_good(path: str | Path, log: Any) -> Path | None:
    """把当前报告原子备份为 ``.last_good.json``；不存在/不可读时返回 ``None``。"""
    source = Path(path)
    if not source.is_file():
        return None
    target = last_good_path(source)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
        return target
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _report_status(path: str | Path) -> str:
    """读旧报告状态（损坏/缺失返回空串，不抛异常）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("status") or "")


def persist_report(
    report: dict[str, Any],
    path: str | Path,
    log: Any,
) -> dict[str, Any]:
    """原子写报告 + ``.last_good.json`` 维护（失败抛异常，旧报告保持不变）。

    - 新报告 ok：写成功后同步刷新 ``.last_good.json``（始终保留最近一份 ok 报告）；
    - 新报告 degraded：覆盖前把上一份报告备份为 ``.last_good.json``（若还没有）。
    """
    target = Path(path)
    new_status = str(report.get("status") or "")
    existed = target.is_file()
    old_status = _report_status(target) if existed else ""
    last_good = last_good_path(target)

    if new_status != "ok":
        if last_good.is_file():
            log(f"degraded 轮：保留已有 last_good={last_good.name}", level="warn")
        elif existed:
            backup = backup_last_good(target, log)
            log(
                f"degraded 轮：上一份报告已备份为 {backup.name if backup else '（无）'}"
                f"（旧 status={old_status or 'unknown'}）",
                level="warn",
            )
    written = write_report(report, target)
    if new_status == "ok":
        backup = backup_last_good(target, log)
        log(f"报告已写入 {target}（{written} bytes）；last_good={backup.name if backup else '-'}")
    else:
        log(f"degraded 报告已写入 {target}（{written} bytes）", level="warn")
    return {
        "path": str(target),
        "bytes": int(written),
        "status": new_status,
        "schema_version": report.get("schema_version"),
        "last_good": str(last_good) if last_good.is_file() else None,
        "replaced_previous": existed,
        "previous_status": old_status or None,
    }


# ---------------------------------------------------------------- 单轮编排
@dataclass
class RoundOptions:
    """单轮编排参数（CLI 与测试共用）。"""

    use_db: bool = True
    dry_run: bool = False
    json_output: bool = False
    verify: bool | None = None
    report: Path | None = None
    log: Path | None = None
    lock: Path | None = None
    database_url: str | None = None
    limit: int | None = None
    now: int | None = None
    #: dry-run 事务连接（run_round 内部注入，不对外）
    db_conn: Any = None
    #: 测试注入：跳过真实锁（None=正常加锁）
    lock_enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.report is None:
            self.report = report_path()
        if self.log is None:
            self.log = log_path()
        if self.lock is None:
            self.lock = lock_path()
        if self.verify is None:
            self.verify = _env_truthy(VERIFY_ENV)


def _stage_summary(stage: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"stage": stage, **payload}


def _degraded_reasons(route: Any, sample: Any, pool: Any) -> list[str]:
    """把 B/C/D 的 degraded 原因合并成 round 级原因（空 list = ok）。"""
    reasons: list[str] = []

    def add(prefix: str, status: Any, stats: dict[str, Any]) -> None:
        status_text = str(status or "")
        if status_text and status_text != "ok":
            detail = str(stats.get("degraded_reason") or "")[:200]
            reasons.append(f"{prefix}:{status_text}:{detail}".rstrip(":"))

    add("phase_b", getattr(route, "status", ""), dict(getattr(route, "stats", None) or {}))
    add("phase_c", getattr(sample, "status", ""), dict(getattr(sample, "stats", None) or {}))
    add("phase_d", getattr(pool, "status", ""), dict(getattr(pool, "stats", None) or {}))
    return reasons


def finalize_round(
    conn: Any,
    round_id: int,
    *,
    status: str,
    degraded_reasons: Sequence[str] = (),
    phase_stats: dict[str, Any] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """收尾本轮 round：finished_at / status / degraded_reason / stats.phase_g。

    status 只升不降（failed 保持 failed，已 degraded 保持 degraded）。
    ``conn`` 非空时复用调用方事务；否则自行开事务（连接串由调用方选择）。
    """
    finished = int(now if now is not None else now_ts())
    reason = ";".join(str(item) for item in degraded_reasons if item) or None
    payload = dict(phase_stats or {})
    sql = """
        UPDATE round SET
            finished_at = to_timestamp(%s),
            status = CASE
                WHEN status = 'failed' THEN 'failed'
                WHEN status = 'degraded' THEN 'degraded'
                WHEN %s::text IS NOT NULL THEN 'degraded'
                ELSE 'ok'
            END,
            degraded_reason = CASE
                WHEN %s::text IS NULL THEN degraded_reason
                WHEN degraded_reason IS NULL OR degraded_reason = '' THEN %s::text
                ELSE degraded_reason || ';' || %s::text
            END,
            stats = COALESCE(stats, '{}'::jsonb) || %s::jsonb
        WHERE id = %s
        RETURNING id, status, degraded_reason
    """
    row = conn.execute(
        sql,
        (
            finished,
            reason,
            reason,
            reason,
            reason,
            json.dumps(payload, ensure_ascii=False, default=str),
            int(round_id),
        ),
    ).fetchone()
    if row is None:
        raise WorkerError(f"round id={round_id} 不存在，无法收尾")
    return {
        "round_id": int(row[0]),
        "status": str(row[1] or ""),
        "degraded_reason": str(row[2] or ""),
        "finished_at": iso(finished),
    }


def finalize_round_db(
    round_id: int,
    *,
    url: str | None,
    conn: Any = None,
    status: str,
    degraded_reasons: Sequence[str] = (),
    phase_stats: dict[str, Any] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """收尾 round：有 ``conn`` 复用事务，否则用 ``url`` 自开一个事务。"""
    kwargs: dict[str, Any] = {
        "status": status,
        "degraded_reasons": degraded_reasons,
        "phase_stats": phase_stats,
        "now": now,
    }
    if conn is not None:
        return finalize_round(conn, round_id, **kwargs)
    with state_mod.transaction(url) as own_conn:
        return finalize_round(own_conn, round_id, **kwargs)


def _run_stages(
    options: RoundOptions,
    summary: dict[str, Any],
    log: WorkerLog,
    started_ts: int,
) -> dict[str, Any]:
    """跑完 B→C→D→E（+F），返回最终报告；任何致命错误抛 ``WorkerError``/异常。"""
    stages = summary["stages"]
    db_conn = options.db_conn
    url: str | None = None
    if options.use_db:
        try:
            url = state_mod.database_url(options.database_url)
        except Exception as exc:  # noqa: BLE001 - 配置错误转退出码 2 由 main 处理
            raise WorkerError(f"数据库配置错误：{redact(exc)}") from exc

    # ---- 1) Phase B：候选视频 / 在线 / 选片（写 round + video_metric）
    route_plan = VideoRoutePlan(max_candidates=options.limit)
    route = run_video_route(
        None,
        route_plan,
        now=started_ts,
        use_db=options.use_db,
        database_url=url,
        db_conn=db_conn,
    )
    route_stats = dict(route.stats or {})
    stages["video_route"] = _stage_summary(
        "video_route",
        {
            "status": route.status,
            "round_id": route.round_id,
            "candidates": len(route.candidates),
            "metrics": len(route.metrics),
            "selected_comment": len(route.selected_comment),
            "selected_danmaku": len(route.selected_danmaku),
            "errors": len(route.errors),
            "degraded_reason": route_stats.get("degraded_reason") or "",
            "elapsed_seconds": route_stats.get("elapsed_seconds"),
        },
    )
    log(
        f"phase_b done status={route.status} round_id={route.round_id} "
        f"candidates={len(route.candidates)} "
        f"selected={len(route.selected_comment)}/{len(route.selected_danmaku)}"
    )
    if str(route.status) == "failed":
        if options.use_db and route.round_id is not None:
            finalize_round_db(
                int(route.round_id),
                url=url,
                conn=db_conn,
                status="failed",
                degraded_reasons=["phase_b:failed"],
                phase_stats={"phase_g_status": "failed"},
                now=started_ts,
            )
        raise WorkerError(
            f"Phase B 失败（无候选视频）：{route_stats.get('degraded_reason') or 'unknown'}"
        )

    round_id = int(route.round_id) if route.round_id is not None else None
    if options.use_db and round_id is None:
        raise WorkerError("Phase B 未返回 round_id（数据库写入缺失）")

    # ---- 2) Phase C：15 分钟窗口计数（回写 round）
    window_plan = WindowSamplePlan()
    sample = run_window_sample(
        None,
        window_plan,
        now=started_ts,
        video_route=route,
        use_db=options.use_db,
        database_url=url,
        round_id=round_id,
        db_conn=db_conn,
    )
    sample_stats = dict(sample.stats or {})
    stages["window_sample"] = _stage_summary(
        "window_sample",
        {
            "status": sample.status,
            "round_id": sample.round_id,
            "comments": len(sample.comments),
            "danmaku": len(sample.danmaku),
            "evidence_comments": len(sample.evidence_comments),
            "evidence_danmaku": len(sample.evidence_danmaku),
            "comment_targets": len(sample.comment_targets),
            "danmaku_targets": len(sample.danmaku_targets),
            "degraded_reason": sample_stats.get("degraded_reason") or "",
            "window_clamped": bool(sample_stats.get("window_clamped")),
            "db_written": bool(sample_stats.get("db_written")),
            "db_error": str(sample_stats.get("db_error") or "")[:200],
            "elapsed_seconds": sample_stats.get("elapsed_seconds"),
        },
    )
    log(
        f"phase_c done status={sample.status} comments={len(sample.comments)} "
        f"danmaku={len(sample.danmaku)}"
    )
    if sample_stats.get("db_error"):
        summary["warnings"].append(f"phase_c db_error: {sample_stats['db_error']}")

    # ---- 3) Phase D：family 状态与池迁移（写 pool1/pool2/family_round_stat）
    pool = run_pool_round(
        sample,
        use_db=options.use_db,
        database_url=url,
        round_id=round_id,
        now=started_ts,
        config=PoolConfig(),
    )
    pool_stats = dict(pool.stats or {})
    stages["pool_round"] = _stage_summary(
        "pool_round",
        {
            "status": pool.status,
            "round_id": pool.round_id,
            "degraded": bool(pool.degraded),
            "observations": len(pool.observations),
            "pool1": len(pool.pool1),
            "pool2": len(pool.pool2),
            "stats_rows": len(pool.stats_rows),
            "transitions": len(pool.transitions),
            "db_written": bool(pool_stats.get("db_written")),
        },
    )
    log(
        f"phase_d done status={pool.status} pool1={len(pool.pool1)} "
        f"pool2={len(pool.pool2)} transitions={len(pool.transitions)}"
    )

    degraded_reasons = _degraded_reasons(route, sample, pool)
    round_status = "degraded" if degraded_reasons else "ok"
    summary["round_status"] = round_status
    summary["degraded_reasons"] = degraded_reasons

    # ---- 3.5) 收尾 round（finished_at/status/stats.phase_g）
    if options.use_db and round_id is not None:
        finalized = finalize_round_db(
            round_id,
            url=url,
            conn=db_conn,
            status=round_status,
            degraded_reasons=degraded_reasons,
            phase_stats={
                "phase_g": {
                    "schema_version": SCHEMA_VERSION,
                    "status": round_status,
                    "degraded_reasons": degraded_reasons,
                    "has_db": True,
                    "dry_run": bool(options.dry_run),
                }
            },
            now=started_ts,
        )
        stages["finalize_round"] = _stage_summary("finalize_round", finalized)
        log(
            f"round finalized id={finalized['round_id']} status={finalized['status']}"
        )

    # ---- 4) Phase E：报告（原子写；degraded 也写）
    if options.use_db and round_id is not None:
        report = report_from_db(
            round_id=round_id,
            url=url,
            conn=db_conn,
            now=started_ts,
        )
    else:
        report = report_from_result(pool, now=started_ts)
    report_schema = str(report.get("schema_version") or "")
    report_status = str(report.get("status") or "")
    if report_schema != REPORT_SCHEMA_V2:
        summary["warnings"].append(
            f"报告 schema_version={report_schema or 'missing'}（期望 {REPORT_SCHEMA_V2}）"
        )
    if report_status != round_status:
        summary["warnings"].append(
            f"报告 status={report_status or 'missing'} 与 round_status={round_status} 不一致"
        )
        if round_status == "degraded":
            # round 已判 degraded 时，报告必须同样标 degraded（宁可多冻结一轮）
            report = dict(report)
            report["status"] = "degraded"
            report_status = "degraded"
    stages["report_build"] = _stage_summary(
        "report_build",
        {
            "status": report_status,
            "schema_version": report_schema,
            "counts": report.get("counts"),
            "round": report.get("round"),
            "degraded": report_status != "ok",
        },
    )

    if options.dry_run:
        stages["report_write"] = _stage_summary(
            "report_write", {"skipped": True, "reason": "dry_run", "path": str(options.report)}
        )
        log("dry_run：跳过报告落盘与审核")
        return report

    write_path = Path(options.report)
    if not options.use_db:
        write_enabled = options.extra.get("explicit_output", False)
        if not write_enabled:
            stages["report_write"] = _stage_summary(
                "report_write",
                {
                    "skipped": True,
                    "reason": "no_db_without_output",
                    "path": str(write_path),
                },
            )
            log("--no-db：未指定 --output，跳过报告落盘（避免误覆盖生产报告）")
            return report
    try:
        write_info = persist_report(report, write_path, log)
    except Exception as exc:  # noqa: BLE001 - 写失败必须保留旧报告
        raise WorkerError(f"报告写入失败（旧报告保持不变）：{redact(exc)}") from exc
    stages["report_write"] = _stage_summary("report_write", write_info)
    summary["report"] = write_info

    # ---- 5) Phase F（可选）：LLM 审核，默认关闭
    if options.verify:
        try:
            audit = run_audit_round(url=options.extra.get("audit_url"), limit=5)
            stages["audit"] = _stage_summary(
                "audit",
                {
                    "enabled": bool(audit.get("enabled")),
                    "queue": audit.get("queue"),
                    "audited": audit.get("audited"),
                    "skipped": audit.get("skipped"),
                    "errors": len(audit.get("errors") or []),
                    "reason": audit.get("reason") or "",
                },
            )
            log(
                f"phase_f done enabled={audit.get('enabled')} "
                f"queue={audit.get('queue')} audited={audit.get('audited')}"
            )
        except Exception as exc:  # noqa: BLE001 - 审核可选，失败不推翻本轮报告
            summary["warnings"].append(f"phase_f failed: {redact(exc)}")
            stages["audit"] = _stage_summary(
                "audit", {"enabled": True, "ok": False, "error": redact(exc)[:300]}
            )
            log(f"phase_f failed（不影响报告）：{redact(exc)}", level="warn")
    else:
        stages["audit"] = _stage_summary(
            "audit", {"skipped": True, "reason": f"{VERIFY_ENV}!=1"}
        )
    return report


def run_round(options: RoundOptions) -> dict[str, Any]:
    """跑一轮 state worker；返回可 JSON 序列化的 summary（不抛异常）。"""
    started_ts = int(options.now if options.now is not None else now_ts())
    wall_started = time.monotonic()
    log = WorkerLog(options.log)
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": iso(started_ts),
        "ok": False,
        "status": "failed",
        "use_db": bool(options.use_db),
        "dry_run": bool(options.dry_run),
        "report_path": str(options.report),
        "lock_path": str(options.lock),
        "stages": {},
        "warnings": [],
        "errors": [],
    }
    mode = "memory" if not options.use_db else ("dry-run" if options.dry_run else "db")
    log(f"round begin mode={mode} report={options.report}")

    def body() -> dict[str, Any]:
        if not options.use_db:
            report = _run_stages(options, summary, log, started_ts)
            summary["report_summary"] = _report_summary(report)
            return report
        if options.dry_run:
            # 整轮写库放进一个事务，最后回滚：验证真实链路但不在生产库落数据
            try:
                url = state_mod.database_url(options.database_url)
            except Exception as exc:  # noqa: BLE001
                raise WorkerError(f"数据库配置错误：{redact(exc)}") from exc
            conn = state_mod.connect(url, autocommit=False)
            options.db_conn = conn
            try:
                report = _run_stages(options, summary, log, started_ts)
                summary["report_summary"] = _report_summary(report)
            finally:
                try:
                    conn.rollback()
                except Exception:  # pragma: no cover - 回滚失败仅记录
                    log("dry_run 事务回滚失败", level="warn")
                conn.close()
                options.db_conn = None
            log("dry_run：数据库写事务已回滚")
            return report

        report = _run_stages(options, summary, log, started_ts)
        summary["report_summary"] = _report_summary(report)
        return report

    lock_cm = (
        single_writer_lock(options.lock)
        if options.lock_enabled
        else _always_locked()
    )
    try:
        with lock_cm as acquired:
            if not acquired:
                summary.update(
                    ok=True,
                    status="skipped_overlap",
                    reason="lock held by another state worker run",
                )
                log("skipped_overlap：已有 state worker 在跑，本轮跳过", level="warn")
                return summary
            try:
                body()
                summary.update(ok=True, status=summary.get("round_status", "ok"))
            except WorkerError as exc:
                summary.update(ok=False, status="failed")
                summary["errors"].append({"type": "WorkerError", "message": redact(exc)})
                log(f"round failed: {redact(exc)}", level="error")
            except Exception as exc:  # noqa: BLE001 - 一轮失败不影响下轮 timer
                summary.update(ok=False, status="failed")
                summary["errors"].append(
                    {"type": type(exc).__name__, "message": redact(exc)[:500]}
                )
                log(
                    "round crashed: "
                    + redact(exc)
                    + "\n"
                    + redact(traceback.format_exc()),
                    level="error",
                )
    finally:
        summary["elapsed_seconds"] = round(time.monotonic() - wall_started, 1)
        summary["finished_at"] = iso(int(options.now if options.now is not None else now_ts()))
        log(
            f"round end status={summary['status']} ok={summary['ok']} "
            f"elapsed={summary['elapsed_seconds']}s"
        )
        log.close()
    return summary


@contextmanager
def _always_locked() -> Iterator[bool]:
    yield True


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    return {
        "schema_version": report.get("schema_version"),
        "status": report.get("status"),
        "generated_at": report.get("generated_at"),
        "round_id": (report.get("round") or {}).get("id"),
        "counts": counts,
        "pool2": counts.get("pool2"),
    }


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.state_worker",
        description="Phase G：15 分钟 state worker（B→C→D→E，可选 F）单轮编排",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮（默认行为；显式给出便于 systemd/人工部署验证）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="真实链路试跑：DB 写在一个事务里最后回滚，不写报告、不跑审核",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="纯内存模式（不读不写 Postgres）；默认不写报告，需显式 --output",
    )
    parser.add_argument("--json", action="store_true", help="stdout 输出单行 JSON 摘要")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="覆盖报告路径（缺省 HOTMEME_MEME_INFO_REPORT）；--no-db 时显式开启落盘",
    )
    parser.add_argument("--report", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--log", type=Path, default=None, help="日志文件（缺省 HOTMEME_STATE_WORKER_LOG）")
    parser.add_argument("--lock", type=Path, default=None, help="锁文件（缺省 HOTMEME_STATE_WORKER_LOCK）")
    parser.add_argument(
        "--database-url",
        default=None,
        help="显式 worker 连接串（缺省 DATABASE_URL；不打印）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="smoke：只取前 N 个候选视频（缺省完整 300 候选）",
    )
    parser.add_argument("--now", type=int, default=None, help="固定当前时间戳（调试/可复算）")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="强制关闭 Phase F 审核（等价 MEME_INFO_VERIFY=0）",
    )
    return parser


def options_from_args(args: argparse.Namespace) -> RoundOptions:
    if args.report is not None and args.output is not None:
        raise SystemExit("--output 与 --report 只能给一个")
    report = args.output or args.report or None
    explicit_output = report is not None
    verify = None if not args.no_verify else False
    return RoundOptions(
        use_db=not args.no_db,
        dry_run=bool(args.dry_run),
        json_output=bool(args.json),
        verify=verify,
        report=report_path(report),
        log=log_path(args.log),
        lock=lock_path(args.lock),
        database_url=args.database_url,
        limit=args.limit,
        now=args.now,
        extra={"explicit_output": explicit_output},
    )


def _format_summary(summary: dict[str, Any]) -> str:
    stages = summary.get("stages") or {}
    route = stages.get("video_route") or {}
    sample = stages.get("window_sample") or {}
    pool = stages.get("pool_round") or {}
    report = summary.get("report_summary") or {}
    lines = [
        f"state_worker {summary.get('status')} ok={summary.get('ok')}"
        f"  elapsed={summary.get('elapsed_seconds')}s"
        f"  mode={'memory' if not summary.get('use_db') else ('dry-run' if summary.get('dry_run') else 'db')}",
        f"B: status={route.get('status')} round={route.get('round_id')}"
        f" candidates={route.get('candidates')}"
        f" selected={route.get('selected_comment')}/{route.get('selected_danmaku')}",
        f"C: status={sample.get('status')} comments={sample.get('comments')}"
        f" danmaku={sample.get('danmaku')} db_written={sample.get('db_written')}",
        f"D: status={pool.get('status')} pool1={pool.get('pool1')}"
        f" pool2={pool.get('pool2')} transitions={pool.get('transitions')}",
        f"E: schema={report.get('schema_version')} status={report.get('status')}"
        f" counts={report.get('counts')}",
    ]
    if summary.get("degraded_reasons"):
        lines.append("degraded_reasons: " + " | ".join(summary["degraded_reasons"]))
    if summary.get("report"):
        lines.append(
            f"报告: {summary['report'].get('path')}"
            f"（{summary['report'].get('bytes')} bytes）"
            f" last_good={summary['report'].get('last_good')}"
        )
    if summary.get("warnings"):
        lines.append("warnings: " + " | ".join(str(w) for w in summary["warnings"]))
    if summary.get("errors"):
        lines.append("errors: " + " | ".join(str(e.get("message")) for e in summary["errors"]))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        options = options_from_args(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    summary = run_round(options)
    if options.json_output:
        print(json.dumps(summary, ensure_ascii=False, default=str))
    else:
        print(_format_summary(summary))
    if summary.get("status") == "skipped_overlap":
        return EXIT_OK
    if summary.get("ok"):
        return EXIT_OK
    if any("数据库配置错误" in str(e.get("message", "")) for e in summary.get("errors") or []):
        return EXIT_USAGE
    return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
