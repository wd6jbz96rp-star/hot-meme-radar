"""Shadow 采集端：跑一轮 meme_info 查询，把 HotMemeReport 原子写入 /tmp 供 app 只读。

设计约束（docs/pm/BOARD.md §22）：

- 只读：不 import ``app``、不写 SQLite、不起 FastAPI、不改 ``/api/top``；
- 输出只写 ``--out``（默认 ``/tmp/meme_info_latest.json``），tmp + ``os.replace`` 原子替换；
- 默认不调 LLM（规则版）；带 ``--verify`` 且找到 key 才调用，key 只报 configured，
  绝不打印内容；找不到 key 时明确降级并继续出规则版；
- stdout 只打印一行摘要 JSON，便于调度/日志采集。

用法::

    python -m tools.meme_info_shadow \
        --out /tmp/meme_info_latest.json --limit 20 \
        --verify --verify-gate
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 直接 `python tools/meme_info_shadow.py` 时，sys.path[0] 是 tools/，
# 会被同目录的 tools/meme_info.py 遮蔽包 `meme_info`；显式把仓库根放最前。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from meme_info.bili import BiliSource
from meme_info.collect import CollectionPlan
from meme_info.models import now_ts, parse_iso
from meme_info.query import QueryConfig, find_hot_memes
from meme_info.verify import ChatClient, LlmConfig, SemanticVerifier

REPO_ROOT = _REPO_ROOT
DEFAULT_OUT = Path("/tmp/meme_info_latest.json")
DEFAULT_CACHE_DIR = Path("/tmp/meme_info_cache")
DB_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal"}
PRODUCTION_DB = (REPO_ROOT / "data" / "hotmeme.db").resolve()


def _now(value: str | None) -> int:
    if not value:
        return now_ts()
    try:
        return int(value)
    except ValueError:
        parsed = parse_iso(value)
        if not parsed:
            raise SystemExit(f"--now 无法解析：{value!r}（ISO 时间或 Unix 秒）")
        return parsed


def _assert_safe_out(path: Path) -> Path:
    """拒绝写数据库文件；其余路径展开成绝对路径。"""
    target = path.expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    else:
        target = target.resolve()
    lowered = target.name.lower()
    if any(lowered.endswith(suffix) for suffix in DB_SUFFIXES):
        raise SystemExit(f"拒绝写数据库文件：{target}（本工具只写 JSON 报告）")
    if target == PRODUCTION_DB or target.parent == PRODUCTION_DB.parent:
        raise SystemExit(f"拒绝写生产数据目录：{target}（shadow 报告必须写 /tmp）")
    return target


def _write_json_atomic(path: Path, payload: dict) -> int:
    """先写同目录临时文件再 os.replace，读端不会看到半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return len(text.encode("utf-8"))


def _build_verifier(cache_dir: str | None) -> tuple[object | None, dict]:
    """--verify 时构造语义审核器；无 key 明确 not_configured 并降级为规则版。"""
    llm = ChatClient(cache_dir=cache_dir)
    config = llm.config
    info = {
        "requested": True,
        "configured": bool(config.configured),
        "model": config.model,
        "cache_dir": cache_dir or "",
        "reason": "" if config.configured else "not_configured",
    }
    if not config.configured:
        print(
            "not_configured：未找到 LLM key（MEME_INFO_LLM_API_KEY / ~/.hotmeme_llm_key），"
            "本轮跳过 LLM，只出规则版。",
            file=sys.stderr,
        )
        return None, info
    return SemanticVerifier(llm, cache_dir=cache_dir), info


def _summary(report, *, out: Path, size: int, elapsed: float,
             verify_gate: bool) -> dict:
    stats = report.stats or {}
    llm = (stats.get("verification") or {}).get("llm") or {}
    top10 = []
    for index, meme in enumerate(report.memes[:10], start=1):
        signals = meme.signals or {}
        top10.append(
            {
                "rank": index,
                "canonical": meme.canonical,
                "score": round(float(meme.score), 1),
                "status": meme.status,
                "publishable": bool(
                    meme.status in ("hot", "rising")
                    and signals.get("evidence_rate_2x2") is True
                ),
            }
        )
    return {
        "ok": True,
        "out": str(out),
        "bytes": size,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(elapsed, 1),
        "schema_version": report.schema_version,
        "generated_at": report.generated_at,
        "memes": len(report.memes),
        "observing": len(report.observing),
        "top10": top10,
        "stats": {
            "videos": stats.get("videos"),
            "comments": stats.get("comments"),
            "danmaku": stats.get("danmaku"),
            "requests_total": stats.get("requests_total", stats.get("requests")),
            "current_families": stats.get("current_families"),
            "current_2x2": stats.get("current_2x2"),
            "observing_families": stats.get("observing_families"),
            "budget_exhausted": stats.get("budget_exhausted"),
        },
        "verification": {
            "configured": bool(llm.get("calls")) or bool(stats.get("verification")),
            "calls": int(llm.get("calls", 0) or 0),
            "ok": int((stats.get("verification") or {}).get("ok", 0) or 0),
            "rejected": int((stats.get("verification") or {}).get("rejected", 0) or 0),
            "attempted": int((stats.get("verification") or {}).get("attempted", 0) or 0),
            "gate": bool(verify_gate),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meme_info_shadow",
        description="跑一轮 meme_info 并把 HotMemeReport 原子写入 JSON（app 只读 shadow）。",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"报告输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--cache-dir", default=os.getenv("MEME_INFO_CACHE_DIR") or str(DEFAULT_CACHE_DIR),
                        help="HTTP/LLM 文件缓存目录（默认 /tmp/meme_info_cache）")
    parser.add_argument("--limit", type=int, default=20, help="报告保留条数（默认 20）")
    parser.add_argument("--comment-videos", type=int, default=45)
    parser.add_argument("--danmaku-videos", type=int, default=10)
    parser.add_argument("--max-requests", type=int, default=330)
    parser.add_argument("--expand-limit", type=int, default=20)
    parser.add_argument("--verify", action="store_true",
                        help="调用 LLM 语义审核（默认不调；无 key 自动降级）")
    parser.add_argument("--verify-gate", action="store_true",
                        help="按 LLM 结论剔除（隐含 --verify；仅 shadow 报告生效）")
    parser.add_argument("--now", default=None, help="ISO 时间或 Unix 秒；用于回放")
    args = parser.parse_args(argv)

    out = _assert_safe_out(Path(args.out))
    limit = max(1, min(int(args.limit), 100))
    verify = bool(args.verify or args.verify_gate)
    if args.verify_gate and not args.verify:
        print("提示：--verify-gate 隐含 --verify。", file=sys.stderr)

    started = time.time()
    now = _now(args.now)
    source = BiliSource(cache_dir=args.cache_dir, max_rps=2.0)
    query_config = QueryConfig(
        plan=CollectionPlan(
            comment_videos=max(1, args.comment_videos),
            danmaku_videos=max(1, args.danmaku_videos),
            max_requests=max(1, args.max_requests),
        ),
        limit=limit,
        expand_queries=True,
        expand_limit=max(0, args.expand_limit),
        verify=verify,
        verify_gate=bool(args.verify_gate),
        # gate 剔除后仍需覆盖 TopN；40 条足够覆盖 limit=20 + 拒绝缓冲。
        verify_limit=max(limit * 2, limit + 10),
    )
    verifier = None
    verify_info: dict = {"requested": verify, "configured": False}
    if verify:
        verifier, verify_info = _build_verifier(args.cache_dir)
        if verifier is None:
            query_config.verify = False
            query_config.verify_gate = False

    report = find_hot_memes(source, config=query_config, now=now, verifier=verifier)
    payload = report.to_dict()
    size = _write_json_atomic(out, payload)
    summary = _summary(
        report,
        out=out,
        size=size,
        elapsed=time.time() - started,
        verify_gate=bool(args.verify_gate),
    )
    summary["verify"] = verify_info
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
