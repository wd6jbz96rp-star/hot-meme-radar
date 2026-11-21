"""只读 shadow adapter：把 meme_info 的 HotMemeReport JSON 暴露给 app。

边界（docs/pm/BOARD.md §22）：

- **只读文件**：不 import ``meme_info``、不写 DB、不触发采集、不改 ``/api/top``；
- 路径来自 ``config.MEME_INFO_REPORT_PATH``（env ``HOTMEME_MEME_INFO_REPORT``）；
- 只返回精简字段，绝不把模块 3MB 原始报告（candidates/meta/usage 全量）透传给前端；
- 状态机：``ok | stale | missing | invalid``；``invalid`` 不抛异常、不 500；
- 按文件 ``(mtime_ns, size)`` 缓存解析结果，避免每请求重复读同一份大 JSON。

Phase E 起同时支持两代报告（``schema_version`` 前缀判定）：

- ``meme-info/v1``：旧单报告，行为保持不变（``status ∈ hot/rising`` 发布门）；
- ``meme-info/v2``：池化报告。``only=publishable`` 只返回 ``pool2``（新梗榜），
  ``only=all`` 返回 ``pool1`` 全量（模块 shadow，含 pool2 成员）；``publishable``
  完全由 pool2 成员资格决定，2×2 只做 ``badge_2x2`` 徽标。报告
  ``status=degraded`` 时不改池成员，仅通过 ``degraded`` / ``report_status``
  字段标记（``ok|stale|missing|invalid`` 新鲜度状态机不变）。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

SCHEMA_PREFIX = "meme-info/"
V2_PREFIX = "meme-info/v2"
PUBLISHABLE_STATUSES = ("hot", "rising")
ONLY_VALUES = ("all", "publishable")
MAX_LIMIT = 50

# 只透传这些 stats 标量；其余（discovery_audit/config/endpoint_counts…）留在文件里。
_STAT_KEYS = (
    "videos",
    "comments",
    "danmaku",
    "docs",
    "comment_evidence_ready",
    "families",
    "current_families",
    "current_2x2",
    "observing_families",
    "discovery_candidates",
    "requests_total",
    "requests",
    "collection_requests",
    "elapsed_total",
    "collection_elapsed",
    "report_elapsed",
    "budget_exhausted",
    # ---- v2（池化报告）----
    "comment_count",
    "danmaku_count",
    "degraded",
    "observations",
    "pool1",
    "pool2",
    "pool1_only",
    "pool1_out",
    "family_round_stats",
    "transitions",
    "heat_model",
    "window_minutes",
    "db_written",
    "sample_status",
)
_LLM_KEYS = ("calls", "prompt_tokens", "completion_tokens", "total_tokens")

#: stats 里的短文本标量（v2：heat 模型名 / 样本状态）
_STAT_TEXT_KEYS = ("heat_model", "sample_status")

_CACHE: dict[str, tuple[tuple[int, int], Any, str]] = {}
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """测试/热更新用：清空解析缓存。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def report_path() -> Path:
    return Path(config.MEME_INFO_REPORT_PATH)


def _max_age_seconds() -> float:
    try:
        return float(getattr(config, "MEME_INFO_MAX_AGE_SECONDS", 3600))
    except (TypeError, ValueError):
        return 3600.0


def _truncate(value: Any, length: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= length else text[: length - 1] + "…"


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _iso_seconds(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:  # 无时区按本地时区，和模块 iso() 口径一致
        parsed = parsed.astimezone()
    return int(parsed.timestamp())


def _load_json(path: Path) -> tuple[Any, str]:
    """读文件并按 (mtime_ns, size) 缓存；返回 (payload, error)。payload=None 表示无效。"""
    key = str(path)
    try:
        stat = path.stat()
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    signature = (stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == signature:
            return cached[1], cached[2]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            error = ""
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            payload, error = None, f"{type(exc).__name__}: {_truncate(exc, 160)}"
        _CACHE[key] = (signature, payload, error)
        return payload, error


def _blank(status: str, path: Path, *, error: str = "",
           generated_at: str | None = None, age_seconds: int | None = None,
           max_age: float = 3600.0) -> dict:
    return {
        "status": status,
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "report_path": str(path),
        "schema_version": "",
        "report_status": None,
        "degraded": False,
        "round": {"id": None, "window_start": None, "window_end": None},
        "window": {},
        "counts": {},
        "count": 0,
        "total_memes": 0,
        "publishable_count": 0,
        "memes": [],
        "stats": {},
        "verification": _empty_verification(),
        "error": error,
    }


def _empty_verification() -> dict:
    return {
        "configured": False,
        "calls": 0,
        "attempted": 0,
        "ok": 0,
        "rejected": 0,
        "errors": 0,
        "audited_families": 0,
        "llm": {key: 0 for key in _LLM_KEYS},
        "model": "",
    }


def _trim_verification(source: Any) -> dict:
    """兼容 v1（stats.verification）与 v2（顶层 verification）两种形状。"""
    result = _empty_verification()
    if not isinstance(source, dict):
        return result
    for key in ("attempted", "ok", "rejected", "errors", "calls", "audited_families"):
        result[key] = _int(source.get(key))
    llm = source.get("llm")
    if isinstance(llm, dict):
        for key in _LLM_KEYS:
            result["llm"][key] = _int(llm.get(key))
    llm_config = source.get("llm_config")
    model = source.get("model")
    if (not isinstance(model, str) or not model) and isinstance(llm_config, dict):
        model = llm_config.get("model")
    result["model"] = _truncate(model, 60)
    configured = source.get("configured")
    if isinstance(configured, bool):
        result["configured"] = configured
    elif isinstance(llm_config, dict):
        result["configured"] = bool(llm_config.get("configured"))
    return result


def _trim_stats(stats: dict) -> dict:
    result: dict[str, Any] = {}
    for key in _STAT_KEYS:
        value = stats.get(key)
        if isinstance(value, bool) or isinstance(value, (int, float)):
            result[key] = value
    for key in _STAT_TEXT_KEYS:
        value = stats.get(key)
        if isinstance(value, str) and value:
            result[key] = _truncate(value, 60)
    return result


def _trim_verified(verified: Any) -> dict | None:
    if not isinstance(verified, dict):
        return None
    result = {
        "is_meme": verified.get("is_meme"),
        "is_current": verified.get("is_current"),
        "heat": _num(verified.get("heat")),
        "confidence": _num(verified.get("confidence")),
        "reason": _truncate(verified.get("reason"), 200),
    }
    if verified.get("error"):
        result["error"] = _truncate(verified.get("error"), 120)
    return result


def _trim_evidence(meme: dict) -> list[dict]:
    raw = meme.get("evidence")
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw[:2]:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "quote": _truncate(item.get("quote"), 120),
                "ctime": _int(item.get("ctime")),
                "like": _int(item.get("like")),
                "bvid": _truncate(item.get("bvid"), 20),
            }
        )
    return items


def _trim_meme(rank: int, meme: dict) -> dict:
    signals = meme.get("signals") if isinstance(meme.get("signals"), dict) else {}
    meta = meme.get("meta") if isinstance(meme.get("meta"), list) else []
    usage = meme.get("usage") if isinstance(meme.get("usage"), list) else []
    rate_2x2 = signals.get("evidence_rate_2x2") is True
    status = str(meme.get("status") or "")
    verified = _trim_verified(meme.get("verified"))
    # 2026-11-15 产品决策：2×2 是证据徽标，不是唯一硬门槛。
    # 用户确认的真梗（灭建国/扎啤等）常只有 1 梗视频+1 使用；只按 2×2 会误杀，
    # 因此 hot/rising + LLM is_meme/is_current 也视为 publishable；2×2 仍单独展示。
    llm_ok = bool(verified and verified.get("is_meme") is True
                  and verified.get("is_current") is True)
    return {
        "rank": rank,
        "family_key": str(meme.get("family_key") or ""),
        "canonical": str(meme.get("canonical") or ""),
        "score": round(float(meme["score"]), 2),
        "status": status,
        "type": _truncate(meme.get("type"), 20),
        "meaning": _truncate(meme.get("meaning"), 200),
        "evidence_rate_2x2": rate_2x2,
        "publishable": status in PUBLISHABLE_STATUSES and (rate_2x2 or llm_ok),
        "verified": verified,
        "meta_count": len(meta) if meta else _int(signals.get("meta_videos")),
        "usage_count": len(usage) if usage else _int(signals.get("usage_docs")),
        "evidence": _trim_evidence(meme),
    }


def _trim_meme_v2(index: int, entry: dict) -> dict:
    """v2 条目：publishable 由报告 pool2 决定；2×2 只做徽标；不伪造 LLM 结论。"""
    raw_rank = entry.get("rank")
    heat_rank = (
        int(raw_rank)
        if isinstance(raw_rank, int) and not isinstance(raw_rank, bool) and raw_rank > 0
        else None
    )
    badge_2x2 = entry.get("badge_2x2") is True
    return {
        # 发布条目沿用报告 heat rank；池1候选（无 heat rank）沿用列表序号，前端编号连续
        "rank": heat_rank if heat_rank is not None else index,
        "family_key": str(entry.get("family_key") or ""),
        "canonical": str(entry.get("canonical") or ""),
        "score": round(float(entry["score"]), 2),
        "status": str(entry.get("status") or ""),
        "type": "",
        "meaning": "",
        # 旧前端徽标位：v2 只表示 2×2 证据，不再参与发布门
        "evidence_rate_2x2": badge_2x2,
        "publishable": entry.get("publishable") is True,
        "verified": None,  # 审核状态走 audit_state / audit_event，不伪造
        "meta_count": _int(entry.get("meta_count")),
        "usage_count": _int(entry.get("usage_count")),
        "evidence": _trim_evidence(entry),
        # ---- v2 新增（池/热度/可信度/审核）----
        "pool": str(entry.get("pool") or ""),
        "cred_score": _num(entry.get("cred_score")),
        "heat_score": _num(entry.get("heat_score")),
        "heat_rank": heat_rank,
        "explainer_only": entry.get("explainer_only") is True,
        "meta_only": entry.get("meta_only") is True,
        "audit_state": _truncate(entry.get("audit_state"), 40),
        "audit_adjustment": _num(entry.get("audit_adjustment")),
        "badge_2x2": badge_2x2,
        "evidence_video_count": _int(entry.get("evidence_video_count")),
        "count_total": _int(entry.get("count_total")),
        "comment_count": _int(entry.get("comment_count")),
        "danmaku_count": _int(entry.get("danmaku_count")),
        "video_count": _int(entry.get("video_count")),
        "first_seen_round": (
            _int(entry["first_seen_round"])
            if entry.get("first_seen_round") is not None
            else None
        ),
        "last_seen_round": (
            _int(entry["last_seen_round"])
            if entry.get("last_seen_round") is not None
            else None
        ),
    }


def _trim_round(value: Any) -> dict:
    result: dict[str, Any] = {"id": None, "window_start": None, "window_end": None}
    if not isinstance(value, dict):
        return result
    round_id = value.get("id")
    if isinstance(round_id, int) and not isinstance(round_id, bool):
        result["id"] = round_id
    for key in ("window_start", "window_end"):
        if isinstance(value.get(key), str):
            result[key] = _truncate(value.get(key), 40)
    return result


def _trim_window(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    minutes = value.get("minutes")
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
        return {"minutes": int(minutes)}
    return {}


def _trim_counts(value: Any) -> dict:
    result: dict[str, int] = {}
    if not isinstance(value, dict):
        return result
    for key in ("pool1", "pool2", "pool1_only", "pool1_out", "observations"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool):
            result[key] = number
    return result


def _validate(payload: Any) -> str:
    """返回空串=结构合法；否则返回 invalid 原因。"""
    if not isinstance(payload, dict):
        return "报告不是 JSON 对象"
    schema = payload.get("schema_version")
    if not isinstance(schema, str) or not schema.startswith(SCHEMA_PREFIX):
        return (f"schema_version 非法：{_truncate(schema, 40)!r}"
                f"（应形如 {SCHEMA_PREFIX}v1 / {V2_PREFIX}）")
    memes = payload.get("memes")
    if not isinstance(memes, list):
        return "memes 不是列表"
    problem = _validate_entries(memes, "memes")
    if problem:
        return problem
    if schema.startswith(V2_PREFIX):
        for key in ("pool1", "pool2"):
            entries = payload.get(key)
            if entries is None:
                continue
            if not isinstance(entries, list):
                return f"{key} 不是列表"
            problem = _validate_entries(entries, key)
            if problem:
                return problem
    return ""


def _validate_entries(entries: list, where: str) -> str:
    for index, meme in enumerate(entries):
        spot = f"{where}[{index}]"
        if not isinstance(meme, dict):
            return f"{spot} 不是对象"
        if not isinstance(meme.get("family_key"), str) or not meme["family_key"]:
            return f"{spot}.family_key 缺失或非字符串"
        if not isinstance(meme.get("canonical"), str) or not meme["canonical"]:
            return f"{spot}.canonical 缺失或非字符串"
        score = meme.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return f"{spot}.score 不是数字"
        if not isinstance(meme.get("status"), str) or not meme["status"]:
            return f"{spot}.status 缺失或非字符串"
    return ""


def _v1_view(payload: dict, *, target: Path, status: str, generated_at: Any,
             age_seconds: int, max_age: float, limit: int, only: str) -> dict:
    """v1 单报告：保持 2026-11-15 的发布门行为（2×2 或 LLM 通过）。"""
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    trimmed = [
        _trim_meme(index + 1, meme)
        for index, meme in enumerate(payload.get("memes") or [])
    ]
    publishable_total = sum(1 for meme in trimmed if meme["publishable"])
    if only == "publishable":
        trimmed = [meme for meme in trimmed if meme["publishable"]]
    selected = trimmed[:limit]
    return {
        "status": status,
        "generated_at": generated_at if isinstance(generated_at, str) else None,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "report_path": str(target),
        "schema_version": str(payload.get("schema_version") or ""),
        "report_status": None,
        "degraded": False,
        "round": {"id": None, "window_start": None, "window_end": None},
        "window": {},
        "counts": {},
        "count": len(selected),
        "total_memes": len(payload.get("memes") or []),
        "publishable_count": publishable_total,
        "memes": selected,
        "stats": _trim_stats(stats),
        "verification": _trim_verification(stats.get("verification")),
        "error": "",
    }


def _v2_view(payload: dict, *, target: Path, status: str, generated_at: Any,
             age_seconds: int, max_age: float, limit: int, only: str) -> dict:
    """v2 池化报告：only=publishable 只返回 pool2；only=all 返回 pool1 全量。"""
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    pool1 = payload.get("pool1") if isinstance(payload.get("pool1"), list) else None
    pool2 = payload.get("pool2") if isinstance(payload.get("pool2"), list) else None
    memes = payload.get("memes") if isinstance(payload.get("memes"), list) else []
    publish_entries = pool2 if pool2 is not None else memes
    all_entries = pool1 if pool1 is not None else publish_entries
    source = publish_entries if only == "publishable" else all_entries
    # publishable_count / total_memes 固定看「池2 / 池1 全量」，不受页大小影响
    publishable_count = sum(
        1 for entry in publish_entries if entry.get("publishable") is not False
    )
    total_memes = len(all_entries)
    trimmed = [_trim_meme_v2(index + 1, entry) for index, entry in enumerate(source)]
    selected = trimmed[:limit]
    report_status = str(payload.get("status") or "")
    verification = payload.get("verification")
    if not isinstance(verification, dict):
        verification = stats.get("verification")
    return {
        "status": status,
        "generated_at": generated_at if isinstance(generated_at, str) else None,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "report_path": str(target),
        "schema_version": str(payload.get("schema_version") or ""),
        "report_status": report_status or None,
        "degraded": report_status == "degraded",
        "round": _trim_round(payload.get("round")),
        "window": _trim_window(payload.get("window")),
        "counts": _trim_counts(payload.get("counts")),
        "count": len(selected),
        "total_memes": total_memes,
        "publishable_count": publishable_count,
        "memes": selected,
        "stats": _trim_stats(stats),
        "verification": _trim_verification(verification),
        "error": "",
    }


def load_report(path: str | Path | None = None,
                limit: int = 10,
                only: str = "all") -> dict:
    """读取报告（v1/v2 自动识别）并返回精简视图。

    ``status``（新鲜度状态机，两代报告一致）：``missing``（文件不存在）→
    ``invalid``（JSON/契约错误）→ ``stale``（``generated_at`` 超过
    ``MEME_INFO_MAX_AGE_SECONDS``）→ ``ok``。

    - ``only=publishable``：v1 = ``status∈(hot,rising)`` 且（2×2 或 LLM 通过）；
      v2 = 只返回 ``pool2``（新梗榜数据源）；
    - ``only=all``：v1 = 报告全量 memes；v2 = ``pool1`` 全量（模块 shadow）；
    - v2 报告 ``status=degraded`` 时保留条目、不改发布结论，仅用
      ``degraded=true`` / ``report_status="degraded"`` 标记（由 UI 提示）。
    """
    if only not in ONLY_VALUES:
        raise ValueError(f"only 仅支持 {'|'.join(ONLY_VALUES)}")
    try:
        bound_limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        bound_limit = 10
    target = Path(path) if path is not None else report_path()
    max_age = _max_age_seconds()

    if not target.is_file():
        return _blank("missing", target, error="报告文件不存在",
                      max_age=max_age)
    payload, error = _load_json(target)
    if error or payload is None:
        return _blank("invalid", target, error=error or "报告无法解析",
                      max_age=max_age)
    problem = _validate(payload)
    if problem:
        return _blank("invalid", target, error=problem, max_age=max_age)

    generated_at = payload.get("generated_at")
    generated_ts = _iso_seconds(generated_at)
    if generated_ts is None:
        # generated_at 坏掉时退回文件 mtime，至少还能判断新鲜度（并标注 invalid? 不：仍可用）
        generated_ts = int(target.stat().st_mtime)
        generated_at = None  # 原值不可解析：不把坏时间透传，新鲜度按文件 mtime 估
    age_seconds = max(0, int(time.time()) - generated_ts)
    status = "stale" if age_seconds > max_age else "ok"

    schema = str(payload.get("schema_version") or "")
    kwargs = {
        "target": target,
        "status": status,
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "max_age": max_age,
        "limit": bound_limit,
        "only": only,
    }
    if schema.startswith(V2_PREFIX):
        return _v2_view(payload, **kwargs)
    return _v1_view(payload, **kwargs)
