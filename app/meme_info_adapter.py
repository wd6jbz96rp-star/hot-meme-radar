"""只读 shadow adapter：把 meme_info 的 HotMemeReport JSON 暴露给 app。

边界（docs/pm/BOARD.md §22）：

- **只读文件**：不 import ``meme_info``、不写 DB、不触发采集、不改 ``/api/top``；
- 路径来自 ``config.MEME_INFO_REPORT_PATH``（env ``HOTMEME_MEME_INFO_REPORT``）；
- 只返回精简字段，绝不把模块 3MB 原始报告（candidates/meta/usage 全量）透传给前端；
- 状态机：``ok | stale | missing | invalid``；``invalid`` 不抛异常、不 500；
- 按文件 ``(mtime_ns, size)`` 缓存解析结果，避免每请求重复读同一份大 JSON。
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
)
_LLM_KEYS = ("calls", "prompt_tokens", "completion_tokens", "total_tokens")

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
        "attempted": 0,
        "ok": 0,
        "rejected": 0,
        "errors": 0,
        "llm": {key: 0 for key in _LLM_KEYS},
        "model": "",
        "configured": False,
    }


def _trim_verification(stats: dict) -> dict:
    verification = stats.get("verification")
    result = _empty_verification()
    if not isinstance(verification, dict):
        return result
    for key in ("attempted", "ok", "rejected", "errors"):
        result[key] = _int(verification.get(key))
    llm = verification.get("llm")
    if isinstance(llm, dict):
        for key in _LLM_KEYS:
            result["llm"][key] = _int(llm.get(key))
    llm_config = verification.get("llm_config")
    if isinstance(llm_config, dict):
        result["model"] = _truncate(llm_config.get("model"), 60)
        result["configured"] = bool(llm_config.get("configured"))
    return result


def _trim_stats(stats: dict) -> dict:
    result: dict[str, Any] = {}
    for key in _STAT_KEYS:
        value = stats.get(key)
        if isinstance(value, bool) or isinstance(value, (int, float)):
            result[key] = value
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


def _validate(payload: Any) -> str:
    """返回空串=结构合法；否则返回 invalid 原因。"""
    if not isinstance(payload, dict):
        return "报告不是 JSON 对象"
    schema = payload.get("schema_version")
    if not isinstance(schema, str) or not schema.startswith(SCHEMA_PREFIX):
        return f"schema_version 非法：{_truncate(schema, 40)!r}（应形如 {SCHEMA_PREFIX}v1）"
    memes = payload.get("memes")
    if not isinstance(memes, list):
        return "memes 不是列表"
    for index, meme in enumerate(memes):
        where = f"memes[{index}]"
        if not isinstance(meme, dict):
            return f"{where} 不是对象"
        if not isinstance(meme.get("family_key"), str) or not meme["family_key"]:
            return f"{where}.family_key 缺失或非字符串"
        if not isinstance(meme.get("canonical"), str) or not meme["canonical"]:
            return f"{where}.canonical 缺失或非字符串"
        score = meme.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return f"{where}.score 不是数字"
        if not isinstance(meme.get("status"), str) or not meme["status"]:
            return f"{where}.status 缺失或非字符串"
    return ""


def load_report(path: str | Path | None = None,
                limit: int = 10,
                only: str = "all") -> dict:
    """读取报告并返回精简视图。

    ``status``：``missing``（文件不存在）→ ``invalid``（JSON/契约错误）→
    ``stale``（``generated_at`` 超过 ``MEME_INFO_MAX_AGE_SECONDS``）→ ``ok``。
    ``only=publishable`` 保留 ``status∈(hot,rising)`` 且
    （2×2 证据为 true 或 LLM is_meme/is_current 为 true）的条目；
    2×2 仍作为独立徽标；只做标记/筛选，不改变模块结论。
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

    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    trimmed = [
        _trim_meme(index + 1, meme)
        for index, meme in enumerate(payload.get("memes") or [])
    ]
    publishable_total = sum(1 for meme in trimmed if meme["publishable"])
    if only == "publishable":
        trimmed = [meme for meme in trimmed if meme["publishable"]]
    selected = trimmed[:bound_limit]
    return {
        "status": status,
        "generated_at": generated_at if isinstance(generated_at, str) else None,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "report_path": str(target),
        "count": len(selected),
        "total_memes": len(payload.get("memes") or []),
        "publishable_count": publishable_total,
        "memes": selected,
        "stats": _trim_stats(stats),
        "verification": _trim_verification(stats),
        "error": "",
    }
