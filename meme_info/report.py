"""Phase E：把 Phase D 的池状态生成为报告 JSON v2（只读、无外部调用）。

边界：

- **只读**：不动 ``pool1`` / ``pool2`` / 迁移；不做采集、不调 LLM、不 import ``app/``；
- 报告只是池状态的投影：``publishable`` 完全由 ``pool2`` 成员资格决定，
  2×2 证据只做 ``badge_2x2`` 徽标，不参与发布门；
- ``status=degraded`` 时照实投影（pool2 保留、不新增 promote），由 adapter 标记降级；
- 原子写：先序列化 → 同目录 tmp → ``fsync`` → ``os.replace``，失败不碰旧报告。

报告 v2 顶层契约（稳定，供 app adapter / UI 只读）::

    {
      "schema_version": "meme-info/v2",
      "generated_at": "...",
      "status": "ok|degraded",
      "round": {"id": 29, "window_start": "...", "window_end": "..."},
      "window": {"minutes": 15},
      "counts": {"pool1": 12, "pool2": 3, ...},
      "stats": {"comment_count": 900, "danmaku_count": 500, "families": 20,
                "degraded": false, ...},
      "verification": {"configured": false, "calls": 0, ...},
      "memes": [pool2 条目，按 heat/rank 降序],
      "pool1": [全量池1条目（normal/provisional，含 pool2 成员），shadow 用],
      "pool2": [与 memes 相同的 pool2 条目],
    }

heat_score 公式（Phase E 可解释代理，无新增外部调用）::

    heat_score = 100 * clamp(
        0.40 * min(1, comment_count / 20)     # 15 分钟窗口去重评论
      + 0.30 * min(1, danmaku_count / 100)    # 15 分钟窗口去重弹幕
      + 0.15 * min(1, video_count / 5)        # 15 分钟窗口独立 bvid
      + 0.15 * clamp(score / 100, 0, 1)       # 池1 score（可信度/平滑/审核）
    , 0, 1)

    comment/danmaku/video 来自 ``family_round_stat``（本轮 15 分钟窗口计数），
    score 是 Phase D 的池1分数；四块贡献即条目里的 ``heat_components``。
    排序：heat_score desc → score desc → family_key asc（稳定、可复算）。
    只给 pool2 计算/展示排名（设计 §5.8）；pool1 内非 pool2 条目不参与热度排名。

cred_score 说明：``cred_base``（Phase D 可信度基数）由 Phase D 写入
``pool1.evidence_json.cred_base``（2026-11-22 PM 复核后落库，不改迁移）。
优先级：内存 ``PoolRoundResult`` / replay JSON 的 observations（精确值）→
``evidence_json.cred_base``；旧数据没有该字段时回退 ``null``（不伪造）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import as_jsonable, iso, now_ts

SCHEMA_VERSION = "meme-info/v2"
WINDOW_MINUTES = 15

#: 池1 的「仍在池内」状态；``out`` 是历史行，不进报告列表（只计数）。
POOL1_ACTIVE_STATUSES: tuple[str, ...] = ("normal", "provisional")
POOL_OUT = "out"
POOL_P1 = "p1"
POOL_P2 = "p2"

HEAT_MODEL = "phase-e/window-heat-v1"
#: heat 权重（和为 1）
HEAT_WEIGHTS: dict[str, float] = {
    "comment": 0.40,
    "danmaku": 0.30,
    "video": 0.15,
    "score": 0.15,
}
#: 饱和锚点：达到该 15 分钟窗口计数值即视为满分贡献（🟡 可调，PM 复核）
HEAT_ANCHORS: dict[str, float] = {
    "comment": 20.0,
    "danmaku": 100.0,
    "video": 5.0,
}

EVIDENCE_LIMIT = 5

#: build_report 里由本模块统一重算、不原样透传的 stats 键
_OVERRIDDEN_STAT_KEYS = frozenset(
    {
        "comment_count",
        "danmaku_count",
        "families",
        "degraded",
        "observations",
        "pool1",
        "pool2",
        "pool1_only",
        "pool1_out",
        "heat_model",
        "heat_weights",
        "heat_anchors",
        "window_minutes",
        "schema_version",
    }
)

_LLM_KEYS = ("calls", "prompt_tokens", "completion_tokens", "total_tokens")


# ---------------------------------------------------------------- 小工具
def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sat(value: float, anchor: float) -> float:
    if anchor <= 0:
        return 0.0
    return _clamp(value / anchor, 0.0, 1.0)


def _mapping(item: Any) -> dict[str, Any]:
    """dataclass / Mapping / JSON 对象统一转 dict；无法识别时返回空 dict。"""
    if isinstance(item, Mapping):
        return dict(item)
    try:
        value = as_jsonable(item)
    except Exception:  # noqa: BLE001 - 投影层不因单行形状异常整体失败
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _iso_value(value: Any) -> str | None:
    """epoch 秒 / datetime / ISO 字符串 → 本地时区 ISO（与模块 ``iso()`` 一致）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return iso(value.timestamp()) or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return iso(float(value)) or None
    if isinstance(value, str):
        return value or None
    return None


# ---------------------------------------------------------------- heat
def heat_components(
    *,
    comment_count: int,
    danmaku_count: int,
    video_count: int,
    score: float,
) -> dict[str, float]:
    """四项贡献（0–100 的绝对值），相加即 heat_score（四舍五入前）。"""
    raw = {
        "comment": 100.0 * HEAT_WEIGHTS["comment"] * _sat(comment_count, HEAT_ANCHORS["comment"]),
        "danmaku": 100.0 * HEAT_WEIGHTS["danmaku"] * _sat(danmaku_count, HEAT_ANCHORS["danmaku"]),
        "video": 100.0 * HEAT_WEIGHTS["video"] * _sat(video_count, HEAT_ANCHORS["video"]),
        "score": 100.0 * HEAT_WEIGHTS["score"] * _clamp(_num(score) / 100.0, 0.0, 1.0),
    }
    return {key: round(value, 4) for key, value in raw.items()}


def compute_heat_score(
    *,
    comment_count: int,
    danmaku_count: int,
    video_count: int,
    score: float,
) -> float:
    """Phase E 热度代理（0–100，见模块 docstring 公式）。"""
    components = heat_components(
        comment_count=comment_count,
        danmaku_count=danmaku_count,
        video_count=video_count,
        score=score,
    )
    return round(_clamp(sum(components.values()), 0.0, 100.0), 2)


# ---------------------------------------------------------------- verification
def llm_configured() -> bool:
    """LLM 审核是否可用：``MEME_INFO_VERIFY=1`` 且能读到 key（只读 bool，不打印 key）。

    当前线上 worker 为 ``MEME_INFO_VERIFY=0``，因此缺省 ``configured=false``；
    报告只聚合 audit_event，不伪造调用次数。
    """
    enabled = os.getenv("MEME_INFO_VERIFY", "0").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        return False
    try:
        from .verify import LlmConfig

        return bool(LlmConfig.from_env().configured)
    except Exception:  # noqa: BLE001 - 配置不可用一律按未配置
        return False


def empty_verification(*, configured: bool | None = None) -> dict[str, Any]:
    return {
        "configured": llm_configured() if configured is None else bool(configured),
        "calls": 0,
        "attempted": 0,
        "ok": 0,
        "rejected": 0,
        "errors": 0,
        "llm": {key: 0 for key in _LLM_KEYS},
        "model": "",
        "audited_families": 0,
    }


def normalize_verification(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """把外部聚合结果补齐成稳定形状（缺键补 0，不推断）。"""
    result = empty_verification()
    if not isinstance(value, Mapping):
        return result
    for key in ("calls", "attempted", "ok", "rejected", "errors", "audited_families"):
        result[key] = _int(value.get(key))
    result["configured"] = bool(value.get("configured", result["configured"]))
    result["model"] = str(value.get("model") or "")
    llm = value.get("llm")
    if isinstance(llm, Mapping):
        for key in _LLM_KEYS:
            result["llm"][key] = _int(llm.get(key))
    return result


# ---------------------------------------------------------------- 条目投影
_COUNT_KEYS = ("comment_count", "danmaku_count", "video_count", "meta_count", "count_total")


def _counts_index(*collections: Sequence[Any]) -> dict[str, dict[str, int]]:
    """把 ``FamilyRoundStat`` / ``FamilyObservation`` 序列合并成 family_key → 计数。"""
    result: dict[str, dict[str, int]] = {}
    for rows in collections:
        for row in rows or ():
            data = _mapping(row)
            key = str(data.get("family_key") or "")
            if not key:
                continue
            counts = result.setdefault(key, {})
            for field in _COUNT_KEYS:
                if field in data:
                    counts[field] = _int(data.get(field))
            if "usage_count" in data:
                counts["usage_count"] = _int(data.get("usage_count"))
    for counts in result.values():
        if "count_total" not in counts:
            counts["count_total"] = (
                counts.get("comment_count", 0)
                + counts.get("danmaku_count", 0)
                + counts.get("video_count", 0)
            )
        if "usage_count" not in counts:
            counts["usage_count"] = counts.get("comment_count", 0) + counts.get("danmaku_count", 0)
    return result


def _cred_index(observations: Sequence[Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in observations or ():
        data = _mapping(row)
        key = str(data.get("family_key") or "")
        if key and data.get("cred_base") is not None:
            result[key] = round(_num(data.get("cred_base")), 2)
    return result


def _evidence_parts(evidence_json: Any) -> tuple[list[dict[str, Any]], bool, int]:
    """返回 (前 5 条证据, badge_2x2, evidence_video_count)，兼容 dict 包装 / 裸 list。"""
    wrapped = evidence_json if isinstance(evidence_json, Mapping) else {}
    if isinstance(evidence_json, Mapping):
        raw_items = evidence_json.get("items")
    elif isinstance(evidence_json, list):
        raw_items = evidence_json
    else:
        raw_items = None
    items: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, Mapping):
                items.append(dict(item))
            if len(items) >= EVIDENCE_LIMIT:
                break
    return (
        items,
        bool(wrapped.get("badge_2x2")),
        _int(wrapped.get("evidence_video_count")),
    )


def _evidence_cred_base(evidence_json: Any) -> float | None:
    """从 ``pool1.evidence_json`` 读 Phase D 落库的 cred_base（旧行可能没有→None）。"""
    if not isinstance(evidence_json, Mapping):
        return None
    value = evidence_json.get("cred_base")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 2)


def _base_entry(pool1_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "family_key": str(pool1_row.get("family_key") or ""),
        "canonical": str(pool1_row.get("canonical") or pool1_row.get("family_key") or ""),
        "aliases": [
            str(alias)
            for alias in (pool1_row.get("aliases") or [])
            if isinstance(alias, (str, int, float)) and str(alias)
        ],
    }


def _entry(
    pool1_row: Mapping[str, Any],
    *,
    pool2_row: Mapping[str, Any] | None,
    counts: Mapping[str, int],
    cred_score: float | None,
    status: str,
) -> dict[str, Any]:
    family_key = str(pool1_row.get("family_key") or "")
    comment_count = _int(counts.get("comment_count"))
    danmaku_count = _int(counts.get("danmaku_count"))
    video_count = _int(counts.get("video_count"))
    meta_count = _int(counts.get("meta_count"))
    count_total = counts.get("count_total")
    if count_total is None:
        count_total = _int(pool1_row.get("count_now"))
    else:
        count_total = _int(count_total)
    usage_count = counts.get("usage_count")
    if usage_count is None:
        usage_count = comment_count + danmaku_count
    else:
        usage_count = _int(usage_count)
    score = round(_num(pool1_row.get("score")), 2)
    in_pool2 = pool2_row is not None
    evidence, badge_2x2, evidence_video_count = _evidence_parts(
        pool1_row.get("evidence_json")
    )
    if cred_score is None:
        # DB 重建路径没有 observations：从 Phase D 落库的 evidence_json 取 cred_base
        cred_score = _evidence_cred_base(pool1_row.get("evidence_json"))
    explainer_only = bool(
        pool1_row.get("explainer_only")
        or (pool2_row or {}).get("explainer_only")
    )
    entry = {
        **_base_entry(pool1_row),
        "status": status,
        "pool": POOL_P2 if in_pool2 else (POOL_OUT if status == POOL_OUT else POOL_P1),
        # publishable 只由 pool2 成员资格决定；2×2 只是徽标（§5.8.1）
        "publishable": bool(in_pool2),
        "score": score,
        "cred_score": cred_score,
        "heat_score": None,
        "rank": None,
        "heat_components": None,
        "count_total": count_total,
        "comment_count": comment_count,
        "danmaku_count": danmaku_count,
        "video_count": video_count,
        "usage_count": usage_count,
        "meta_count": meta_count,
        "explainer_only": explainer_only,
        "meta_only": bool(pool1_row.get("meta_only")),
        "audit_state": str(pool1_row.get("audit_state") or "none"),
        "audit_adjustment": round(_num(pool1_row.get("audit_adjustment")), 2),
        "badge_2x2": badge_2x2,
        "evidence_video_count": evidence_video_count,
        "evidence": evidence,
        "first_seen_round": (
            _int(pool1_row.get("first_seen_round"))
            if pool1_row.get("first_seen_round") is not None
            else None
        ),
        "last_seen_round": (
            _int(pool1_row.get("last_seen_round"))
            if pool1_row.get("last_seen_round") is not None
            else None
        ),
        "reason": str((pool2_row or {}).get("reason") or ""),
        "family_key_ok": bool(family_key),  # 内部占位，构建后删除
    }
    return entry


def _fill_heat(entry: dict[str, Any]) -> None:
    components = heat_components(
        comment_count=_int(entry.get("comment_count")),
        danmaku_count=_int(entry.get("danmaku_count")),
        video_count=_int(entry.get("video_count")),
        score=_num(entry.get("score")),
    )
    entry["heat_components"] = components
    entry["heat_score"] = round(_clamp(sum(components.values()), 0.0, 100.0), 2)


def _drop_internal(entry: dict[str, Any]) -> dict[str, Any]:
    entry.pop("family_key_ok", None)
    return entry


# ---------------------------------------------------------------- 报告组装
def build_report(
    *,
    pool1: Sequence[Any] = (),
    pool2: Sequence[Any] = (),
    counts_rows: Sequence[Any] = (),
    observations: Sequence[Any] = (),
    round_id: int | None = None,
    window_start: Any = None,
    window_end: Any = None,
    degraded: bool = False,
    round_status: str = "",
    stats: Mapping[str, Any] | None = None,
    verification: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """从池状态（Phase D 结果 / DB 读取结果）构建报告 v2。

    只投影、不改状态；``degraded`` 轮保留 pool2 成员资格，不新增 promote。
    """
    now = int(now if now is not None else now_ts())
    if not generated_at:
        generated_at = iso(now)
    stats_in = dict(stats or {})
    status_is_degraded = (
        bool(degraded)
        or bool(stats_in.get("degraded"))
        or str(round_status or "").lower() not in ("", "ok")
    )
    report_status = "degraded" if status_is_degraded else "ok"

    # ---- 索引
    pool1_rows: list[dict[str, Any]] = [_mapping(row) for row in pool1 or ()]
    pool2_rows: list[dict[str, Any]] = [_mapping(row) for row in pool2 or ()]
    counts_by_key = _counts_index(observations, counts_rows)
    cred_by_key = _cred_index(observations)
    pool1_by_key: dict[str, dict[str, Any]] = {}
    for row in pool1_rows:
        key = str(row.get("family_key") or "")
        if key:
            pool1_by_key[key] = row
    pool2_by_key: dict[str, dict[str, Any]] = {}
    for row in pool2_rows:
        key = str(row.get("family_key") or "")
        if key:
            pool2_by_key[key] = row

    # ---- 池2 条目（发布/新梗榜数据源）
    pool2_entries: list[dict[str, Any]] = []
    orphan_pool2 = 0
    for key, pool2_row in pool2_by_key.items():
        pool1_row = pool1_by_key.get(key)
        if pool1_row is None:
            orphan_pool2 += 1
            continue
        status = str(pool1_row.get("status") or "normal")
        if status == POOL_OUT:
            continue
        entry = _entry(
            pool1_row,
            pool2_row=pool2_row,
            counts=counts_by_key.get(key, {}),
            cred_score=cred_by_key.get(key),
            status=status,
        )
        _fill_heat(entry)
        pool2_entries.append(entry)
    pool2_entries.sort(key=lambda item: (-item["heat_score"], -item["score"], item["family_key"]))
    for index, entry in enumerate(pool2_entries):
        entry["rank"] = index + 1

    # ---- 池1 条目（normal/provisional 全量；含池2成员，pool 字段区分）
    active_rows = [
        row
        for row in pool1_rows
        if str(row.get("status") or "") in POOL1_ACTIVE_STATUSES
    ]
    pool1_only_entries = [
        _drop_internal(
            _entry(
                row,
                pool2_row=None,
                counts=counts_by_key.get(str(row.get("family_key") or ""), {}),
                cred_score=cred_by_key.get(str(row.get("family_key") or "")),
                status=str(row.get("status") or "normal"),
            )
        )
        for row in active_rows
        if str(row.get("family_key") or "") not in pool2_by_key
    ]
    pool1_only_entries.sort(
        key=lambda item: (
            -item["score"],
            0 if item["status"] == "normal" else 1,
            item["family_key"],
        )
    )
    out_rows = [
        row for row in pool1_rows if str(row.get("status") or "") == POOL_OUT
    ]
    # 先显示已发布（按 heat rank），再显示池1候选（按 score）
    pool1_entries = [_drop_internal(entry) for entry in pool2_entries] + pool1_only_entries

    # ---- 统计
    counts_comment = sum(_int(item.get("comment_count")) for item in counts_by_key.values())
    counts_danmaku = sum(_int(item.get("danmaku_count")) for item in counts_by_key.values())
    stats_out: dict[str, Any] = {}
    for key, value in stats_in.items():
        if key in _OVERRIDDEN_STAT_KEYS:
            continue
        if isinstance(value, (bool, int, float, str)):
            stats_out[key] = value
    families = _int(stats_in.get("families"), -1)
    if families < 0:
        families = len(observations or ()) or len(active_rows)
    stats_out.update(
        {
            "comment_count": _int(stats_in.get("comment_count"), counts_comment),
            "danmaku_count": _int(stats_in.get("danmaku_count"), counts_danmaku),
            "families": families,
            "degraded": status_is_degraded,
            "observations": len(observations or ()),
            "pool1": len(pool1_entries),
            "pool2": len(pool2_entries),
            "pool1_only": len(pool1_only_entries),
            "pool1_out": len(out_rows),
            "heat_model": HEAT_MODEL,
            "heat_weights": dict(HEAT_WEIGHTS),
            "heat_anchors": dict(HEAT_ANCHORS),
            "window_minutes": WINDOW_MINUTES,
        }
    )

    counts_out = {
        "pool1": len(pool1_entries),
        "pool2": len(pool2_entries),
        "pool1_only": len(pool1_only_entries),
        "pool1_out": len(out_rows),
        "observations": len(observations or ()),
    }

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": report_status,
        "round": {
            "id": _int(round_id) if round_id is not None else None,
            "window_start": _iso_value(window_start),
            "window_end": _iso_value(window_end),
        },
        "window": {"minutes": WINDOW_MINUTES},
        "counts": counts_out,
        "stats": stats_out,
        "verification": normalize_verification(verification),
        "memes": pool2_entries,
        "pool1": pool1_entries,
        "pool2": pool2_entries,
    }
    if orphan_pool2:
        report["stats"]["orphan_pool2"] = orphan_pool2
    return report


# ---------------------------------------------------------------- 输入适配器
def report_from_result(
    result: Any,
    *,
    generated_at: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """从内存 ``PoolRoundResult`` 生成报告（含 cred_base 与精确窗口计数）。"""
    status = str(getattr(result, "status", "ok") or "ok")
    stats = getattr(result, "stats", None)
    return build_report(
        pool1=getattr(result, "pool1", ()) or (),
        pool2=getattr(result, "pool2", ()) or (),
        counts_rows=getattr(result, "stats_rows", ()) or (),
        observations=getattr(result, "observations", ()) or (),
        round_id=getattr(result, "round_id", None),
        window_start=getattr(result, "window_start", None),
        window_end=getattr(result, "window_end", None),
        degraded=bool(getattr(result, "degraded", False)) or status.lower() != "ok",
        round_status=status,
        stats=stats if isinstance(stats, Mapping) else {},
        generated_at=generated_at,
        now=now,
    )


def report_from_mapping(
    payload: Any,
    *,
    generated_at: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """从 Phase D ``PoolRoundResult.to_dict()`` 的 JSON replay 生成报告（--no-db）。"""
    data = _mapping(payload)
    if not data:
        raise ValueError("pool JSON 顶层必须是对象")
    status = str(data.get("status") or "ok")
    return build_report(
        pool1=data.get("pool1") or (),
        pool2=data.get("pool2") or (),
        counts_rows=data.get("stats_rows") or (),
        observations=data.get("observations") or (),
        round_id=data.get("round_id"),
        window_start=data.get("window_start"),
        window_end=data.get("window_end"),
        degraded=bool(data.get("degraded")) or status.lower() != "ok",
        round_status=status,
        stats=data.get("stats") if isinstance(data.get("stats"), Mapping) else {},
        generated_at=generated_at or data.get("generated_at") or None,
        now=now,
    )


# ---------------------------------------------------------------- Postgres 读取
def _state_module() -> Any:
    from . import state

    return state


def _db_verification(conn: Any) -> dict[str, Any]:
    """从 ``audit_event`` / ``pool1.audit_state`` 聚合审核统计（不伪造调用）。"""
    totals: dict[str, int] = {}
    llm_calls = 0
    attempted = 0
    passed = 0
    rejected = 0
    for kind, verdict, number in conn.execute(
        "SELECT kind, verdict, count(*) FROM audit_event GROUP BY kind, verdict"
    ).fetchall():
        count = _int(number)
        totals[f"{kind}:{verdict}"] = count
        attempted += count
        if verdict == "pass":
            passed += count
        elif verdict == "fail":
            rejected += count
        if kind == "llm":
            llm_calls += count
    model_row = conn.execute(
        "SELECT model FROM audit_event WHERE kind = 'llm' "
        "AND model IS NOT NULL AND model <> '' "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    audited = conn.execute(
        "SELECT count(*) FROM pool1 WHERE audit_state IS NOT NULL AND audit_state <> 'none'"
    ).fetchone()
    result = empty_verification()
    result.update(
        {
            "calls": llm_calls,
            "attempted": attempted,
            "ok": passed,
            "rejected": rejected,
            "errors": 0,
            "model": str(model_row[0]) if model_row else "",
            "audited_families": _int(audited[0]) if audited else 0,
        }
    )
    result["llm"]["calls"] = llm_calls
    return result


def _round_row(conn: Any, round_id: int | None) -> tuple[int, dict[str, Any]]:
    sql = (
        "SELECT id, started_at, window_start, window_end, status, "
        "comments_sampled, danmaku_sampled, stats, degraded_reason "
        "FROM round {where} ORDER BY started_at DESC, id DESC LIMIT 1"
    )
    if round_id is None:
        row = conn.execute(sql.format(where="")).fetchone()
        if row is None:
            raise ValueError("数据库没有 round，无法生成报告；请先跑 Phase B/C/D")
    else:
        row = conn.execute(sql.format(where="WHERE id = %s"), (_int(round_id),)).fetchone()
        if row is None:
            raise ValueError(f"round id={round_id} 不存在")
    round_info = {
        "id": _int(row[0]),
        "window_start": row[2],
        "window_end": row[3],
        "status": str(row[4] or ""),
        "comments_sampled": _int(row[5]),
        "danmaku_sampled": _int(row[6]),
        "stats": row[7] if isinstance(row[7], Mapping) else {},
        "degraded_reason": str(row[8] or ""),
    }
    return round_info["id"], round_info


def _db_stat_rows(conn: Any, round_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT family_key, comment_count, danmaku_count, video_count, meta_count, "
        "count_total, ratio, score_before, score_after, delta, reason "
        "FROM family_round_stat WHERE round_id = %s",
        (round_id,),
    ).fetchall()
    return [
        {
            "family_key": str(row[0]),
            "comment_count": _int(row[1]),
            "danmaku_count": _int(row[2]),
            "video_count": _int(row[3]),
            "meta_count": _int(row[4]),
            "count_total": _int(row[5]),
            "ratio": row[6],
            "score_before": row[7],
            "score_after": row[8],
            "delta": row[9],
            "reason": str(row[10] or ""),
        }
        for row in rows
    ]


def report_from_db(
    conn: Any = None,
    *,
    round_id: int | None = None,
    url: str | None = None,
    now: int | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """从 Postgres 读 latest（或指定）round + 池状态，生成报告 v2（只读）。

    - ``conn`` 传入时复用调用方事务（测试回滚用）；
    - 缺省打开只读连接，不写任何表。
    """
    from .pools import load_pool_state

    def build(connection: Any) -> dict[str, Any]:
        resolved_round, round_info = _round_row(connection, round_id)
        pool1, pool2 = load_pool_state(connection)
        stat_rows = _db_stat_rows(connection, resolved_round)
        stats = {
            # round 计数为 0 时退回 family_round_stat 汇总（Phase C 尚未把样本量写 round）
            "comment_count": round_info["comments_sampled"] or None,
            "danmaku_count": round_info["danmaku_sampled"] or None,
            "families": len(stat_rows) if stat_rows else None,
        }
        if round_info["degraded_reason"]:
            stats["degraded_reason"] = round_info["degraded_reason"]
        return build_report(
            pool1=list(pool1.values()),
            pool2=list(pool2.values()),
            counts_rows=stat_rows,
            round_id=resolved_round,
            window_start=round_info["window_start"],
            window_end=round_info["window_end"],
            degraded=round_info["status"].lower() not in ("ok", ""),
            round_status=round_info["status"],
            stats=stats,
            verification=_db_verification(connection),
            generated_at=generated_at,
            now=now,
        )

    if conn is not None:
        return build(conn)
    state = _state_module()
    with state.connection(url, autocommit=True) as connection:
        return build(connection)


# ---------------------------------------------------------------- 原子写
def write_report(report: Mapping[str, Any], path: str | Path) -> int:
    """原子写报告：先序列化 → 同目录 tmp → fsync → ``os.replace``。

    返回写入字节数。序列化/写 tmp 失败时不触碰旧报告，也不留 tmp 文件。
    """
    target = Path(path)
    # 1) 先序列化：任何序列化错误都发生在磁盘写入之前（不默认 str 兜底，
    #    避免把不可序列化对象悄悄写坏报告；契约字段都应是 JSON 原生类型）
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    data = text.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return len(data)


def read_report(path: str | Path) -> dict[str, Any]:
    """读回报告 JSON（CLI/测试用；adapter 不走这里，保持零依赖边界）。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("报告顶层必须是对象")
    return payload
