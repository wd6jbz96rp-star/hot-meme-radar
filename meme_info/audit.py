"""Phase F：审核（可选）——LLM 审核包装、人工审核、audit_event 与 pool1 分数替换。

设计口径（``docs/pm/REARCHITECTURE.md`` §3.8 / §5.9 / §5.9.1 / §5.9.2）：

- **默认暂停**：只有 ``MEME_INFO_VERIFY=1`` 且能读到 LLM key 时才调用
  :class:`~meme_info.verify.SemanticVerifier`；缺省 ``run_audit_round`` 直接返回
  ``enabled=false``，不产生任何写库/调用。
- **加减分（最新替换，不累加）**::

      LLM   pass +8    fail -15   unclear 0
      Human pass +15   fail -25   need_more 0    merge 合并后重算

  ``audit_event`` 保留流水（``UNIQUE(family_key, evidence_hash, kind)``）；
  ``pool1.audit_adjustment`` 只存**最新一次**决定性结论的分数变化，分数按
  ``score - old_adjustment + new_adjustment`` 替换（因此 +8 → -25 → +15 的
  结果是 ``score + 15``，不是 ``score - 2``）。人工结论覆盖 LLM 结论。
- **同一 evidence_hash 不重复审核**：证据哈希由 family 身份（canonical/aliases）
  与证据样本（doc_id/bvid/quote/2×2 徽标）决定；证据不变时 LLM 不重复调用
  （已存在同 kind 事件即跳过）。证据变化后 ``audit_hash != current_hash``，
  重新进入队列。
- **人工覆盖**：同一 evidence_hash 上已有人工结论时 LLM 跳过；人工可对同一
  evidence_hash 重新结论（upsert 替换上一版，最新替换语义）。
- **池迁移**：写入后立即用 P1/P2 阈值（40/32、65/55）同步：
  ``normal + score>=65 + usage>0`` 进池2；``score<55`` 出池2；``score<32`` out；
  审核 ``fail`` 一律撤出池2（模式 B：否决 + 加分）。explainer-only 沿用 Phase D
  规则（不因审核低分立即淘汰、无使用不新增进池2）。迁移写 ``pool_transition``。
- **边界**：不改 ``pools.py`` / worker / 公开 app。审核只经
  :func:`meme_info.state.transaction` 读写 Postgres；测试可在调用方事务内执行并回滚。

口径（PM 2026-11-22 复核，Phase D 已统一）：``audit_adjustment`` 是**一次性事件**。
Phase D 每轮分数公式不再加 ``audit_adjustment``，只透传「当前已应用的最新审核分」
（``pool1.audit_adjustment`` 仅用于审核时替换旧结论）；本模块审核时按
``score = score - old_adjustment + new_adjustment`` 一次性应用，不重复累加历史，
也不在后续轮次再次加分。``compute_audit_update`` 的替换公式与 Phase D 口径一致，
本 Phase 不改 ``pools.py``。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import Pool1Row, as_jsonable, iso, now_ts
from .text import normalize_surface

# ---------------------------------------------------------------- 常量
AUDIT_SCHEMA_VERSION = "meme-info/audit-v1"
EVIDENCE_HASH_VERSION = "ev1"
MERGE_PROMPT_VERSION = "meme-info/audit-merge-v1"

AUDIT_KINDS: tuple[str, ...] = ("llm", "human")
LLM_VERDICTS: tuple[str, ...] = ("pass", "fail", "unclear")
HUMAN_VERDICTS: tuple[str, ...] = ("pass", "fail", "unclear", "merge")

#: LLM 加减分（§3.8）
LLM_DELTAS: dict[str, float] = {"pass": 8.0, "fail": -15.0, "unclear": 0.0}
#: 人工加减分（§3.8）；merge 的调整由「合并后重算」决定，事件 delta 记 0
HUMAN_DELTAS: dict[str, float] = {
    "pass": 15.0,
    "fail": -25.0,
    "unclear": 0.0,
    "merge": 0.0,
}
DELTAS: dict[str, dict[str, float]] = {"llm": LLM_DELTAS, "human": HUMAN_DELTAS}

#: Admin 操作 → (kind, verdict, audit_state)
ACTION_MAP: dict[str, tuple[str, str, str]] = {
    "approve": ("human", "pass", "human_pass"),
    "reject": ("human", "fail", "human_fail"),
    "merge": ("human", "merge", "human_merge"),
    "need_more": ("human", "unclear", "human_need_more"),
}
VERDICT_STATE: dict[tuple[str, str], str] = {
    ("llm", "pass"): "llm_pass",
    ("llm", "fail"): "llm_fail",
    ("llm", "unclear"): "llm_unclear",
    ("human", "pass"): "human_pass",
    ("human", "fail"): "human_fail",
    ("human", "unclear"): "human_need_more",
    ("human", "merge"): "human_merge",
}
#: 视为「人工已明确结论」的状态前缀（人工优先于 LLM）
HUMAN_STATES: tuple[str, ...] = tuple(
    state for (kind, _), state in VERDICT_STATE.items() if kind == "human"
)

P1_IN = 40.0
P1_OUT = 32.0
P2_IN = 65.0
P2_OUT = 55.0

#: 审核队列优先级（§5.9.2；数字越小越优先）
PRIORITY_NEAR_P2 = 1
PRIORITY_LLM_STRONG = 2
PRIORITY_EXPLAINER = 3
PRIORITY_LONG_UNREVIEWED = 4

#: 队列默认上限（避免每轮/每次操作全量审核）
DEFAULT_QUEUE_LIMIT = 20
DEFAULT_RUN_LIMIT = 5
#: 长期未复审：池2 条目超过该天数进入第 4 优先级
DEFAULT_LONG_REVIEW_DAYS = 14
#: 「接近池2」分数区间（任务书 50–70；§5.9.2 原文 55–70）
NEAR_P2_LOW = 50.0
NEAR_P2_HIGH = 70.0
#: 证据「强」定义：2×2 徽标 / 独立证据视频数 >=2 / 独立证据文档数 >=2 / 元话语
STRONG_EVIDENCE_VIDEOS = 2
STRONG_EVIDENCE_DOCS = 2


# ---------------------------------------------------------------- 异常
class AuditError(RuntimeError):
    """审核错误基类。"""


class AuditConfigError(AuditError):
    """缺少数据库连接串 / LLM 配置。"""


class AuditValidationError(AuditError):
    """参数不合法（kind/verdict/operator/reason/merge 目标等）。"""


class AuditNotFoundError(AuditError):
    """pool1 中没有该 family。"""


class AuditStaleEvidenceError(AuditError):
    """页面/调用方携带的 evidence_hash 已过期（证据已变化），拒绝写库。"""


# ---------------------------------------------------------------- 配置 / 开关
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_truthy(name: str, default: str = "0") -> bool:
    return _env(name, default).strip().lower() in ("1", "true", "yes", "on")


def llm_enabled() -> bool:
    """LLM 审核是否开启：``MEME_INFO_VERIFY=1`` 且能读到 key（不打印 key）。"""
    if not _env_truthy("MEME_INFO_VERIFY"):
        return False
    try:
        from .verify import LlmConfig

        return bool(LlmConfig.from_env().configured)
    except Exception:  # noqa: BLE001 - 配置不可用一律按关闭处理
        return False


def default_audit_url(explicit: str | None = None) -> str:
    """审核连接串：显式 > ``ADMIN_DATABASE_URL`` > ``DATABASE_URL``。"""
    from . import state

    try:
        return state.admin_database_url(explicit)
    except state.StateConfigError as exc:  # pragma: no cover - 测试会跳过
        raise AuditConfigError(str(exc)) from exc


def _state_module() -> Any:
    from . import state

    return state


# ---------------------------------------------------------------- 纯工具
def _clamp_score(value: float) -> float:
    return float(min(100.0, max(0.0, float(value))))


def _round3(value: float) -> float:
    return round(float(value), 3)


def _get(row: Any, name: str, default: Any = None) -> Any:
    """兼容 mapping / dataclass 行。"""
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _evidence_items(row: Any) -> list[dict[str, Any]]:
    payload = _get(row, "evidence_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if isinstance(payload, Mapping):
        items = payload.get("items")
    elif isinstance(payload, list):
        items = payload
    else:
        items = None
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, Mapping)]


def _evidence_flag(row: Any, name: str, default: Any = 0) -> Any:
    payload = _get(row, "evidence_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if isinstance(payload, Mapping):
        return payload.get(name, default)
    return default


def evidence_is_strong(row: Any) -> bool:
    """证据是否足够强（人工/LLM 不确定时仍值得复审的判据，§5.9.2 第 2 项）。"""
    try:
        badge_docs = int(_evidence_flag(row, "badge_docs", 0) or 0)
        badge_videos = int(_evidence_flag(row, "badge_videos", 0) or 0)
        evidence_videos = int(_evidence_flag(row, "evidence_video_count", 0) or 0)
    except (TypeError, ValueError):
        badge_docs = badge_videos = evidence_videos = 0
    return bool(
        _evidence_flag(row, "badge_2x2", False)
        or badge_docs >= STRONG_EVIDENCE_DOCS
        or badge_videos >= STRONG_EVIDENCE_VIDEOS
        or evidence_videos >= STRONG_EVIDENCE_VIDEOS
        or bool(_get(row, "meta_only", False))
        or bool(_get(row, "explainer_only", False))
    )


def family_evidence_hash(row: Any) -> str:
    """按 family 身份 + 证据样本算稳定哈希（证据变化 → 新哈希 → 可重新审核）。

    只包含「审核所需证据」（canonical/aliases/证据 doc 的 doc_id+bvid+quote /
    2×2 徽标），不包含每轮都会变的计数/分数，避免每轮都触发全量 LLM 调用。
    """
    canonical = normalize_surface(str(_get(row, "canonical", "") or ""))
    aliases = sorted(
        {
            normalize_surface(str(alias))
            for alias in (_get(row, "aliases", []) or [])
            if str(alias or "").strip()
        }
    )
    docs: list[str] = []
    for item in _evidence_items(row):
        doc_id = str(item.get("doc_id") or item.get("comment_id") or "")
        bvid = str(item.get("bvid") or "")
        quote = str(item.get("quote") or "")[:200]
        docs.append(f"{doc_id}\x1f{bvid}\x1f{quote}")
    payload = {
        "v": EVIDENCE_HASH_VERSION,
        "c": canonical,
        "a": aliases,
        "d": sorted(set(docs)),
        "x2": bool(_evidence_flag(row, "badge_2x2", False)),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def verdict_adjustment(kind: str, verdict: str) -> float:
    """加减分表（未知组合直接报错，避免静默 0）。"""
    if kind not in DELTAS:
        raise AuditValidationError(f"kind 非法：{kind}")
    table = DELTAS[kind]
    if verdict not in table:
        raise AuditValidationError(f"{kind} verdict 非法：{verdict}")
    return float(table[verdict])


def audit_state_of(kind: str, verdict: str) -> str:
    try:
        return VERDICT_STATE[(kind, verdict)]
    except KeyError as exc:
        raise AuditValidationError(f"不支持的审核结论：{kind}/{verdict}") from exc


def verdict_label(state: str) -> str:
    """把 ``llm_pass`` / ``human_need_more`` 转成展示文案（无伪造成分）。"""
    labels = {
        "none": "未审核",
        "llm_pass": "LLM 通过",
        "llm_fail": "LLM 否决",
        "llm_unclear": "LLM 不确定",
        "human_pass": "人工通过",
        "human_fail": "人工否决",
        "human_need_more": "需更多证据",
        "human_merge": "人工合并",
    }
    return labels.get(state, state or "未审核")


# ---------------------------------------------------------------- 纯计算：审核后状态
@dataclass
class AuditUpdate:
    """一次审核结论对 pool1/pool2 的纯计算结果（不碰数据库）。"""

    family_key: str
    kind: str
    verdict: str
    delta: float
    old_adjustment: float
    new_adjustment: float
    score_before: float
    score_after: float
    status_before: str
    status_after: str
    explainer_only: bool = False
    usage: int = 0
    pool2_action: str = "none"  # keep / insert / remove
    transitions: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return as_jsonable(self)


def compute_audit_update(
    row: Any,
    *,
    kind: str,
    verdict: str,
    in_pool2: bool = False,
    usage: int | None = None,
) -> AuditUpdate:
    """最新替换语义：``score = clamp(score - old_adjustment + delta)``，再同步池。"""
    if kind not in DELTAS:
        raise AuditValidationError(f"kind 非法：{kind}")
    if verdict not in DELTAS[kind]:
        raise AuditValidationError(f"{kind} verdict 非法：{verdict}")

    delta = verdict_adjustment(kind, verdict)
    old_adjustment = float(_get(row, "audit_adjustment", 0.0) or 0.0)
    score_before = float(_get(row, "score", 0.0) or 0.0)
    score_after = _clamp_score(score_before - old_adjustment + delta)
    status_before = str(_get(row, "status", "normal") or "normal")
    status_after = status_before
    explainer_only = bool(_get(row, "explainer_only", False))
    usage_count = int(
        (usage if usage is not None else int(_get(row, "count_now", 0) or 0)) or 0
    )
    transitions: list[dict[str, Any]] = []
    reasons: list[str] = [f"audit:{kind}:{verdict}:{delta:+.0f}"]

    if verdict == "fail":
        # 模式 B：fail = 否决，explainer-only 例外失效
        explainer_only = False

    def _active(state: str) -> bool:
        return state in ("normal", "provisional")

    # ---- 池1迁移（滞回：32–40 / normal 与 out 之间）
    if _active(status_before):
        if score_after < P1_OUT:
            status_after = "out"
            reasons.append("p1:out_low_score")
            transitions.append(
                {
                    "family_key": _get(row, "family_key", ""),
                    "from_pool": "pool1",
                    "to_pool": None,
                    "reason": "p1:audit_out",
                    "score": score_after,
                }
            )
        elif status_before == "provisional" and (
            usage_count > 0 or (kind == "human" and verdict == "pass")
        ):
            # 人工通过视为确认；有本轮使用则按 Phase D 转 normal
            status_after = "normal"
            reasons.append("p1:provisional_to_normal")
    elif status_before == "out" and score_after >= P1_IN:
        status_after = "normal"
        reasons.append("p1:reentry")
        transitions.append(
            {
                "family_key": _get(row, "family_key", ""),
                "from_pool": None,
                "to_pool": "pool1",
                "reason": "p1:audit_reentry",
                "score": score_after,
            }
        )

    # ---- 池2同步
    pool2_action = "keep"
    if status_after != "normal":
        pool2_action = "remove" if in_pool2 else "none"
        if pool2_action == "remove":
            reasons.append("p2:out_status")
    elif verdict == "fail":
        pool2_action = "remove" if in_pool2 else "none"
        if pool2_action == "remove":
            reasons.append("p2:out_audit_fail")
        # fail 后即使分数仍 >=55 也不在池2；explainer 标记已清空
        explainer_only = False
    elif explainer_only:
        # explainer-only 沿用 Phase D：不因 P2_out 立即淘汰、无使用不新增进池2
        pool2_action = "keep"
        if in_pool2:
            reasons.append("p2:explainer_keep")
    elif score_after < P2_OUT:
        pool2_action = "remove" if in_pool2 else "none"
        if pool2_action == "remove":
            reasons.append("p2:out_low_score")
    elif score_after >= P2_IN and usage_count > 0:
        if in_pool2:
            pool2_action = "keep"
        else:
            pool2_action = "insert"
            reasons.append("p2:in_audit")
    else:
        pool2_action = "keep"

    return AuditUpdate(
        family_key=str(_get(row, "family_key", "") or ""),
        kind=kind,
        verdict=verdict,
        delta=delta,
        old_adjustment=old_adjustment,
        new_adjustment=delta,
        score_before=_round3(score_before),
        score_after=_round3(score_after),
        status_before=status_before,
        status_after=status_after,
        explainer_only=explainer_only,
        usage=usage_count,
        pool2_action=pool2_action,
        transitions=transitions,
        reasons=reasons,
    )


# ---------------------------------------------------------------- 审核队列（纯排序）
@dataclass
class AuditCandidate:
    """队列条目（Admin 页面 / CLI list 共用）。"""

    family_key: str
    canonical: str
    score: float
    status: str
    pool: str                       # pool1 / pool2
    evidence_hash: str
    audit_state: str
    audit_hash: str
    explainer_only: bool
    meta_only: bool
    priority: int
    priority_reason: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    evidence_strong: bool = False
    latest_event: dict[str, Any] | None = None
    count_now: int = 0
    age_days: float | None = None
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return as_jsonable(self)


def _event_ts(event: Mapping[str, Any] | None) -> float | None:
    if not event:
        return None
    if isinstance(event.get("ts"), (int, float)):
        return float(event["ts"])
    created = event.get("created_at")
    if not created:
        return None
    text = str(created)
    try:
        from datetime import datetime

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def rank_audit_queue(
    rows: Sequence[Any],
    *,
    pool2_keys: Iterable[str] = (),
    latest_events: Mapping[str, Mapping[str, Any]] | None = None,
    now: int | None = None,
    limit: int | None = None,
    long_review_days: int = DEFAULT_LONG_REVIEW_DAYS,
) -> list[AuditCandidate]:
    """按 §5.9.2 优先级排序（纯函数，便于无库测试）。

    1. score 50–70 接近池2；
    2. LLM unclear/fail 且证据强；
    3. explainer-only / 元话语待确认；
    4. 长期未复审的池2条目。

    过滤：已有人工结论且证据未变化的条目；无任何证据样本（items/徽标/元话语）的
    条目；同一证据已 LLM pass 的条目。
    """
    now = int(now if now is not None else now_ts())
    pool2 = {str(key) for key in pool2_keys}
    events = dict(latest_events or {})
    result: list[AuditCandidate] = []

    for row in rows:
        key = str(_get(row, "family_key", "") or "")
        if not key:
            continue
        status = str(_get(row, "status", "normal") or "normal")
        if status not in ("normal", "provisional"):
            continue
        state = str(_get(row, "audit_state", "none") or "none")
        row_hash = str(_get(row, "audit_hash", "") or "")
        current_hash = family_evidence_hash(row)
        in_pool2 = key in pool2
        items = _evidence_items(row)
        strong = evidence_is_strong(row)
        # 证据过期才允许重新审核：没有证据样本且不像元话语 → 不进队列
        if not items and not strong:
            continue
        # 人工已对当前证据给出结论 → 不再打扰（人工优先）
        if state.startswith("human_") and row_hash == current_hash:
            continue

        event = events.get(key)
        age_days: float | None = None
        event_ts = _event_ts(event)
        if event_ts is not None:
            age_days = max(0.0, (now - event_ts) / 86400.0)
        elif in_pool2:
            age_days = None  # 从未审核过的池2条目：算「长期未复审」

        score = float(_get(row, "score", 0.0) or 0.0)
        llm_uncertain = state in ("llm_unclear", "llm_fail")
        already_llm_current = state.startswith("llm_") and row_hash == current_hash
        explainer_flag = bool(
            _get(row, "explainer_only", False) or _get(row, "meta_only", False)
        )

        priority: int
        reason: str
        if llm_uncertain and strong:
            priority = PRIORITY_LLM_STRONG
            reason = f"llm:{state.split('_', 1)[-1]}+strong_evidence"
        elif NEAR_P2_LOW <= score <= NEAR_P2_HIGH:
            if state == "llm_pass" and row_hash == current_hash:
                continue
            priority = PRIORITY_NEAR_P2
            reason = f"near_p2:score={score:.1f}"
        elif explainer_flag and state != "human_pass":
            priority = PRIORITY_EXPLAINER
            reason = "explainer_only_pending"
        elif in_pool2 and (age_days is None or age_days >= long_review_days):
            priority = PRIORITY_LONG_UNREVIEWED
            reason = (
                "pool2_never_audited"
                if age_days is None
                else f"pool2_unreviewed:{age_days:.1f}d"
            )
        else:
            continue

        # 同优先级内排序权重：分数（近池2/explainer）或未复审时长
        if priority == PRIORITY_LLM_STRONG:
            weight = score + (10.0 if state == "llm_fail" else 0.0)
        elif priority == PRIORITY_LONG_UNREVIEWED:
            weight = float(age_days if age_days is not None else 10**6)
        else:
            weight = score
        result.append(
            AuditCandidate(
                family_key=key,
                canonical=str(_get(row, "canonical", "") or ""),
                score=_round3(score),
                status=status,
                pool="pool2" if in_pool2 else "pool1",
                evidence_hash=current_hash,
                audit_state=state,
                audit_hash=row_hash,
                explainer_only=bool(_get(row, "explainer_only", False)),
                meta_only=bool(_get(row, "meta_only", False)),
                priority=priority,
                priority_reason=reason,
                evidence=items[:5],
                evidence_strong=strong,
                latest_event=dict(event) if event else None,
                count_now=int(_get(row, "count_now", 0) or 0),
                age_days=round(age_days, 2) if age_days is not None else None,
                aliases=[
                    str(alias) for alias in (_get(row, "aliases", []) or []) if alias
                ],
            )
        )
        # 排序权重暂存到私有属性以外的字段：用列表元组排序
        result[-1]._weight = weight  # type: ignore[attr-defined]

    result.sort(
        key=lambda item: (
            item.priority,
            -float(getattr(item, "_weight", 0.0)),
            item.family_key,
        )
    )
    if limit is None:
        return result
    limit = max(0, int(limit))
    return result[:limit] if limit else []


# ---------------------------------------------------------------- Postgres 行读取
_POOL1_AUDIT_COLUMNS = (
    "family_key, canonical, aliases_json, status, score, count_prev, count_now, "
    "zero_streak, audit_state, audit_hash, audit_adjustment, meta_only, "
    "explainer_only, first_seen_round, last_seen_round, evidence_json, updated_at"
)

_AUDIT_EVENT_COLUMNS = (
    "family_key, id, kind, verdict, delta, confidence, reason, operator, model, "
    "prompt_version, evidence_hash, created_at"
)


def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row_from_record(record: Any) -> Pool1Row:
    """tuple 行 → :class:`Pool1Row`（列顺序同 ``_POOL1_AUDIT_COLUMNS``）。"""
    return Pool1Row(
        family_key=str(record[0]),
        canonical=str(record[1] or ""),
        aliases=[
            str(item) for item in _json_load(record[2], []) if item
        ],
        status=str(record[3] or "normal"),
        score=float(record[4] or 0.0),
        count_prev=int(record[5] or 0),
        count_now=int(record[6] or 0),
        zero_streak=int(record[7] or 0),
        audit_state=str(record[8] or "none"),
        audit_hash=str(record[9] or ""),
        audit_adjustment=float(record[10] or 0.0),
        meta_only=bool(record[11]),
        explainer_only=bool(record[12]),
        first_seen_round=int(record[13]) if record[13] is not None else None,
        last_seen_round=int(record[14]) if record[14] is not None else None,
        evidence_json=_json_load(record[15], {}),
        updated_at=iso(record[16].timestamp()) if record[16] is not None else "",
    )


def load_pool1_row(conn: Any, family_key: str, *, for_update: bool = False) -> Pool1Row | None:
    sql = f"SELECT {_POOL1_AUDIT_COLUMNS} FROM pool1 WHERE family_key = %s"
    if for_update:
        sql += " FOR UPDATE"
    record = conn.execute(sql, (family_key,)).fetchone()
    return _row_from_record(record) if record is not None else None


def _resolve_round_ref(conn: Any, candidate: int | None) -> int | None:
    """round 外键安全化：老 round 被 90 天保留期清理后 pool1 仍可能引用它。"""
    if candidate is None:
        return None
    try:
        value = int(candidate)
    except (TypeError, ValueError):
        return None
    record = conn.execute("SELECT id FROM round WHERE id = %s", (value,)).fetchone()
    return int(record[0]) if record is not None else None


def _pool1_active_rows(conn: Any) -> list[Pool1Row]:
    sql = (
        f"SELECT {_POOL1_AUDIT_COLUMNS} FROM pool1 "
        "WHERE status IN ('normal', 'provisional') ORDER BY score DESC, family_key"
    )
    return [_row_from_record(record) for record in conn.execute(sql).fetchall()]


def load_pool2_keys(conn: Any) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT family_key FROM pool2").fetchall()}


def latest_audit_events(
    conn: Any, family_keys: Iterable[str] | None = None
) -> dict[str, dict[str, Any]]:
    """每个 family 的最新一条 audit_event（按 created_at,id 倒序）。"""
    result: dict[str, dict[str, Any]] = {}
    keys = [str(key) for key in (family_keys or [])]
    where = "WHERE family_key = ANY(%s)" if keys else ""
    if keys:
        rows = conn.execute(
            "SELECT DISTINCT ON (family_key) "
            + _AUDIT_EVENT_COLUMNS
            + " FROM audit_event "
            + where
            + " ORDER BY family_key, created_at DESC, id DESC",
            (keys,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT ON (family_key) "
            + _AUDIT_EVENT_COLUMNS
            + " FROM audit_event ORDER BY family_key, created_at DESC, id DESC"
        ).fetchall()
    for record in rows:
        event = _event_from_record(record)
        result[event["family_key"]] = event
    return result


def _event_from_record(record: Any) -> dict[str, Any]:
    created = record[11]
    return {
        "family_key": str(record[0]),
        "id": int(record[1]),
        "kind": str(record[2] or ""),
        "verdict": str(record[3] or ""),
        "delta": float(record[4] or 0.0),
        "confidence": float(record[5]) if record[5] is not None else None,
        "reason": str(record[6] or ""),
        "operator": str(record[7] or ""),
        "model": str(record[8] or ""),
        "prompt_version": str(record[9] or ""),
        "evidence_hash": str(record[10] or ""),
        "created_at": iso(created.timestamp()) if created is not None else "",
        "ts": created.timestamp() if created is not None else None,
    }


def list_audit_events(
    conn: Any, family_key: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT "
        + _AUDIT_EVENT_COLUMNS
        + " FROM audit_event WHERE family_key = %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (family_key, int(limit)),
    ).fetchall()
    return [_event_from_record(record) for record in rows]


def build_audit_queue(
    conn: Any, *, limit: int = DEFAULT_QUEUE_LIMIT, now: int | None = None
) -> list[AuditCandidate]:
    """从 Postgres 读活跃 pool1 + pool2 + 最新审核事件，返回排序后的队列。"""
    limit = int(limit)
    if limit <= 0:
        return []
    rows = _pool1_active_rows(conn)
    if not rows:
        return []
    pool2_keys = load_pool2_keys(conn)
    events = latest_audit_events(conn, [row.family_key for row in rows])
    return rank_audit_queue(
        rows,
        pool2_keys=pool2_keys,
        latest_events=events,
        now=now,
        limit=limit,
    )


# ---------------------------------------------------------------- 数据库写入
def _transition_row(
    *,
    family_key: str,
    round_id: int | None,
    from_pool: str | None,
    to_pool: str | None,
    reason: str,
    score: float,
) -> tuple[Any, ...]:
    return (family_key, round_id, from_pool, to_pool, reason, float(score))


def _insert_transitions(conn: Any, transitions: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for item in transitions:
        conn.execute(
            "INSERT INTO pool_transition "
            "(family_key, round_id, from_pool, to_pool, reason, score) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            _transition_row(
                family_key=str(item.get("family_key") or ""),
                round_id=item.get("round_id"),
                from_pool=item.get("from_pool"),
                to_pool=item.get("to_pool"),
                reason=str(item.get("reason") or ""),
                score=float(item.get("score") or 0.0),
            ),
        )
        count += 1
    return count


def _sync_pool2(
    conn: Any,
    *,
    row: Any,
    update: AuditUpdate,
    in_pool2: bool,
    round_id: int | None,
) -> tuple[bool, list[dict[str, Any]]]:
    """按 update.pool2_action 同步 pool2，返回 (是否在池2, 迁移记录)。"""
    key = update.family_key
    transitions: list[dict[str, Any]] = []
    if update.pool2_action == "remove" and in_pool2:
        conn.execute("DELETE FROM pool2 WHERE family_key = %s", (key,))
        transitions.append(
            {
                "family_key": key,
                "round_id": round_id,
                "from_pool": "pool2",
                "to_pool": "pool1",
                "reason": f"p2:out_audit_{update.verdict}",
                "score": update.score_after,
            }
        )
        return False, transitions
    if update.pool2_action == "insert" and not in_pool2:
        conn.execute(
            "INSERT INTO pool2 (family_key, round_id, reason, explainer_only) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (family_key) DO UPDATE SET "
            "reason = EXCLUDED.reason, explainer_only = EXCLUDED.explainer_only",
            (key, round_id, f"p2:in_audit_{update.verdict}", update.explainer_only),
        )
        transitions.append(
            {
                "family_key": key,
                "round_id": round_id,
                "from_pool": "pool1",
                "to_pool": "pool2",
                "reason": f"p2:in_audit_{update.verdict}",
                "score": update.score_after,
            }
        )
        return True, transitions
    if in_pool2 and update.explainer_only:
        conn.execute(
            "UPDATE pool2 SET explainer_only = true WHERE family_key = %s", (key,)
        )
    return in_pool2, transitions


def _apply_update(
    conn: Any,
    row: Pool1Row,
    update: AuditUpdate,
    *,
    in_pool2: bool,
    round_id: int | None,
    event_id: int | None = None,
) -> dict[str, Any]:
    """把纯计算结果写回 pool1 + pool2 + pool_transition。"""
    raw = json.dumps(update.transitions, ensure_ascii=False)
    transitions = json.loads(raw)
    p1_transitions = [item for item in transitions if item.get("from_pool") != "pool2"]
    for item in p1_transitions:
        item["round_id"] = round_id
        item["family_key"] = update.family_key
    conn.execute(
        "UPDATE pool1 SET score = %s, status = %s, explainer_only = %s, "
        "audit_state = %s, audit_hash = %s, audit_adjustment = %s, updated_at = now() "
        "WHERE family_key = %s",
        (
            float(update.score_after),
            update.status_after,
            bool(update.explainer_only),
            audit_state_of(update.kind, update.verdict),
            str(_get(row, "audit_hash", "") or ""),
            float(update.new_adjustment),
            update.family_key,
        ),
    )
    in_pool2_after, p2_transitions = _sync_pool2(
        conn,
        row=row,
        update=update,
        in_pool2=in_pool2,
        round_id=round_id,
    )
    transition_count = _insert_transitions(conn, p1_transitions + p2_transitions)
    return {
        "pool2_before": in_pool2,
        "pool2_after": in_pool2_after,
        "event_id": event_id,
        "transitions": p1_transitions + p2_transitions,
        "transition_count": transition_count,
    }


def _validate_request(
    kind: str, verdict: str, *, operator: str, reason: str
) -> None:
    if kind not in AUDIT_KINDS:
        raise AuditValidationError(f"kind 仅支持 llm|human：{kind}")
    if kind == "llm":
        if verdict not in LLM_VERDICTS:
            raise AuditValidationError(f"LLM verdict 仅支持 pass|fail|unclear：{verdict}")
        return
    if verdict not in HUMAN_VERDICTS:
        raise AuditValidationError(
            f"人工 verdict 仅支持 pass|fail|unclear|merge：{verdict}"
        )
    if not str(operator or "").strip():
        raise AuditValidationError("人工审核必须填写 operator")
    if verdict in ("fail", "merge") and not str(reason or "").strip():
        raise AuditValidationError(f"人工 {verdict} 必须填写 reason")


def _write_audit_event(
    conn: Any,
    *,
    family_key: str,
    round_id: int | None,
    kind: str,
    verdict: str,
    delta: float,
    confidence: float | None,
    reason: str,
    operator: str,
    model: str,
    prompt_version: str,
    evidence_hash: str,
) -> int:
    row = conn.execute(
        "INSERT INTO audit_event (family_key, round_id, kind, verdict, delta, "
        "confidence, reason, operator, model, prompt_version, evidence_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (family_key, evidence_hash, kind) DO UPDATE SET "
        "verdict = EXCLUDED.verdict, delta = EXCLUDED.delta, "
        "confidence = EXCLUDED.confidence, reason = EXCLUDED.reason, "
        "operator = EXCLUDED.operator, model = EXCLUDED.model, "
        "prompt_version = EXCLUDED.prompt_version, round_id = EXCLUDED.round_id, "
        "created_at = now() "
        "RETURNING id",
        (
            family_key,
            round_id,
            kind,
            verdict,
            float(delta),
            confidence,
            str(reason or "")[:500],
            str(operator or "")[:120],
            str(model or "")[:120],
            str(prompt_version or "")[:120],
            evidence_hash,
        ),
    ).fetchone()
    return int(row[0])


def record_audit(
    conn: Any = None,
    *,
    url: str | None = None,
    family_key: str,
    kind: str,
    verdict: str,
    reason: str = "",
    operator: str = "",
    model: str = "",
    prompt_version: str = "",
    confidence: float | None = None,
    evidence_hash: str | None = None,
    round_id: int | None = None,
    merge_into: str | None = None,
    allow_stale_evidence: bool = False,
) -> dict[str, Any]:
    """记录一次审核结论并立即更新 pool1 分数 / 审计字段 / 池成员。

    - 同一 ``(family_key, evidence_hash, kind)`` 唯一：LLM 对同一证据重复调用
      直接 ``skipped``（不重复加分）；人工可重新结论（upsert 替换）。
    - 人工结论优先：同一 evidence_hash 已有人工结论时 LLM 跳过。
    - ``merge``：把 ``family_key`` 合并进 ``merge_into``（family_key 或 canonical），
      合并后重算目标分数，源条目置 out。
    - ``conn`` 传入时复用调用方事务（测试回滚）；否则自建事务并提交。
    """
    _validate_request(kind, verdict, operator=operator, reason=reason)
    if conn is not None:
        return _record_audit_with_conn(
            conn,
            family_key=family_key,
            kind=kind,
            verdict=verdict,
            reason=reason,
            operator=operator,
            model=model,
            prompt_version=prompt_version,
            confidence=confidence,
            evidence_hash=evidence_hash,
            round_id=round_id,
            merge_into=merge_into,
            allow_stale_evidence=allow_stale_evidence,
        )
    state = _state_module()
    with state.transaction(default_audit_url(url)) as own_conn:
        return _record_audit_with_conn(
            own_conn,
            family_key=family_key,
            kind=kind,
            verdict=verdict,
            reason=reason,
            operator=operator,
            model=model,
            prompt_version=prompt_version,
            confidence=confidence,
            evidence_hash=evidence_hash,
            round_id=round_id,
            merge_into=merge_into,
            allow_stale_evidence=allow_stale_evidence,
        )


def _record_audit_with_conn(
    conn: Any,
    *,
    family_key: str,
    kind: str,
    verdict: str,
    reason: str,
    operator: str,
    model: str,
    prompt_version: str,
    confidence: float | None,
    evidence_hash: str | None,
    round_id: int | None,
    merge_into: str | None,
    allow_stale_evidence: bool,
) -> dict[str, Any]:
    row = load_pool1_row(conn, family_key, for_update=True)
    if row is None:
        raise AuditNotFoundError(f"pool1 中不存在 family_key={family_key}")

    current_hash = family_evidence_hash(row)
    if evidence_hash and evidence_hash != current_hash and not allow_stale_evidence:
        raise AuditStaleEvidenceError(
            "证据已变化（evidence_hash 过期），请刷新后重新审核"
        )
    use_hash = str(evidence_hash or current_hash)
    if not use_hash:
        raise AuditValidationError("evidence_hash 为空，无法记录审核")
    round_ref = _resolve_round_ref(
        conn, int(round_id) if round_id is not None else row.last_seen_round
    )

    if kind == "human" and verdict == "merge":
        return _record_merge(
            conn,
            row,
            source_hash=use_hash,
            merge_into=merge_into,
            operator=operator,
            reason=reason,
            confidence=confidence,
            round_id=round_ref,
        )

    existing_event = conn.execute(
        "SELECT id, verdict, delta FROM audit_event "
        "WHERE family_key = %s AND evidence_hash = %s AND kind = %s",
        (family_key, use_hash, kind),
    ).fetchone()

    if kind == "llm":
        # 人工结论优先：同一证据上已有人工结论时 LLM 不再覆盖
        if row.audit_state.startswith("human_") and row.audit_hash == use_hash:
            return {
                "ok": True,
                "skipped": True,
                "skip_reason": "human_override",
                "family_key": family_key,
                "kind": kind,
                "verdict": verdict,
                "evidence_hash": use_hash,
                "audit_state": row.audit_state,
                "audit_adjustment": row.audit_adjustment,
                "score": row.score,
            }
        if existing_event is not None:
            return {
                "ok": True,
                "skipped": True,
                "skip_reason": "duplicate_evidence",
                "family_key": family_key,
                "kind": kind,
                "verdict": verdict,
                "evidence_hash": use_hash,
                "audit_state": row.audit_state,
                "audit_adjustment": row.audit_adjustment,
                "score": row.score,
            }

    in_pool2 = row.family_key in load_pool2_keys(conn)
    update = compute_audit_update(
        row, kind=kind, verdict=verdict, in_pool2=in_pool2
    )
    state_name = audit_state_of(kind, verdict)
    event_id = _write_audit_event(
        conn,
        family_key=family_key,
        round_id=round_ref,
        kind=kind,
        verdict=verdict,
        delta=update.new_adjustment,
        confidence=confidence,
        reason=reason,
        operator=operator,
        model=model,
        prompt_version=prompt_version or f"{AUDIT_SCHEMA_VERSION}/{kind}",
        evidence_hash=use_hash,
    )
    # audit_hash 记录最新一次审核所用的证据哈希
    row.audit_hash = use_hash
    applied = _apply_update(
        conn,
        row,
        update,
        in_pool2=in_pool2,
        round_id=round_ref,
        event_id=event_id,
    )
    return {
        "ok": True,
        "skipped": False,
        "family_key": family_key,
        "kind": kind,
        "verdict": verdict,
        "delta": update.new_adjustment,
        "old_adjustment": update.old_adjustment,
        "audit_state": state_name,
        "evidence_hash": use_hash,
        "round_id": round_ref,
        "score_before": update.score_before,
        "score_after": update.score_after,
        "status_before": update.status_before,
        "status_after": update.status_after,
        "explainer_only": update.explainer_only,
        "pool2_before": applied["pool2_before"],
        "pool2_after": applied["pool2_after"],
        "transitions": applied["transitions"],
        "reasons": update.reasons,
        "event_id": event_id,
        "operator": operator,
    }


# ---------------------------------------------------------------- merge
def _resolve_merge_target(conn: Any, merge_into: str) -> Pool1Row | None:
    text = str(merge_into or "").strip()
    if not text:
        return None
    row = load_pool1_row(conn, text, for_update=True)
    if row is not None:
        return row
    record = conn.execute(
        f"SELECT {_POOL1_AUDIT_COLUMNS} FROM pool1 WHERE canonical = %s "
        "ORDER BY score DESC LIMIT 1 FOR UPDATE",
        (text,),
    ).fetchone()
    if record is not None:
        return _row_from_record(record)
    norm = normalize_surface(text)
    if not norm:
        return None
    for record in conn.execute(
        f"SELECT {_POOL1_AUDIT_COLUMNS} FROM pool1 ORDER BY score DESC"
    ).fetchall():
        candidate = _row_from_record(record)
        surfaces = [candidate.canonical, *candidate.aliases]
        if any(normalize_surface(surface) == norm for surface in surfaces if surface):
            return candidate
    return None


def _merged_evidence(
    target: Pool1Row, source: Pool1Row
) -> dict[str, Any]:
    def _payload(row: Pool1Row) -> dict[str, Any]:
        value = row.evidence_json if isinstance(row.evidence_json, Mapping) else {}
        return dict(value)

    target_payload = _payload(target)
    source_payload = _payload(source)
    merged_items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for payload in (target_payload, source_payload):
        for item in payload.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            key = (
                str(item.get("doc_id") or ""),
                str(item.get("bvid") or ""),
                str(item.get("quote") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged_items.append(dict(item))
    merged_items = merged_items[:8]

    def _num(payload: Mapping[str, Any], name: str) -> float:
        try:
            return float(payload.get(name) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    merged_from = list(target_payload.get("merged_from") or [])
    merged_from.append(source.family_key)
    return {
        **target_payload,
        "items": merged_items,
        "badge_2x2": bool(target_payload.get("badge_2x2"))
        or bool(source_payload.get("badge_2x2")),
        "badge_docs": max(
            int(_num(target_payload, "badge_docs")),
            int(_num(source_payload, "badge_docs")),
        ),
        "badge_videos": max(
            int(_num(target_payload, "badge_videos")),
            int(_num(source_payload, "badge_videos")),
        ),
        "evidence_video_count": max(
            int(_num(target_payload, "evidence_video_count")),
            int(_num(source_payload, "evidence_video_count")),
        ),
        "cred_base": max(
            _num(target_payload, "cred_base"), _num(source_payload, "cred_base")
        ),
        "merged_from": merged_from[-10:],
    }


def _record_merge(
    conn: Any,
    source: Pool1Row,
    *,
    source_hash: str,
    merge_into: str | None,
    operator: str,
    reason: str,
    confidence: float | None,
    round_id: int | None,
) -> dict[str, Any]:
    if not str(merge_into or "").strip():
        raise AuditValidationError("merge 必须指定 merge_into（目标 family_key/canonical）")
    target = _resolve_merge_target(conn, str(merge_into))
    if target is None:
        raise AuditNotFoundError(f"merge 目标不存在：{merge_into}")
    if target.family_key == source.family_key:
        raise AuditValidationError("merge 目标不能是自身")

    source_pool2 = source.family_key in load_pool2_keys(conn)
    target_pool2 = target.family_key in load_pool2_keys(conn)

    # ---- 合并别名 / 证据
    aliases: list[str] = list(target.aliases)
    for surface in [source.canonical, *source.aliases]:
        text = str(surface or "").strip()
        if text and text not in aliases and normalize_surface(text) != normalize_surface(
            target.canonical
        ):
            aliases.append(text)
    merged_evidence = _merged_evidence(target, source)

    # ---- 合并后重算：按本轮使用量加权（缺省各 1，避免除零）
    weight_target = max(int(target.count_now or 0), 0) + 1
    weight_source = max(int(source.count_now or 0), 0) + 1
    raw_score = (
        target.score * weight_target + source.score * weight_source
    ) / (weight_target + weight_source)
    target_score = _clamp_score(raw_score)
    target_status = target.status
    if target_status in ("normal", "provisional"):
        if target_score < P1_OUT:
            target_status = "out"
        elif target_status == "provisional" and (
            target.count_now > 0 or source.count_now > 0
        ):
            target_status = "normal"
    elif target_status == "out" and target_score >= P1_IN:
        target_status = "normal"
    target_explainer = bool(target.explainer_only or source.explainer_only)
    if target_status != "normal":
        target_explainer = False

    conn.execute(
        "UPDATE pool1 SET aliases_json = %s::jsonb, evidence_json = %s::jsonb, "
        "score = %s, status = %s, explainer_only = %s, updated_at = now() "
        "WHERE family_key = %s",
        (
            json.dumps(aliases[:32], ensure_ascii=False),
            json.dumps(merged_evidence, ensure_ascii=False),
            float(target_score),
            target_status,
            target_explainer,
            target.family_key,
        ),
    )
    transitions: list[dict[str, Any]] = []
    # 目标侧池迁移（分数变化）
    if target_status == "normal" and not target_explainer:
        if target_pool2 and target_score < P2_OUT:
            conn.execute(
                "DELETE FROM pool2 WHERE family_key = %s", (target.family_key,)
            )
            transitions.append(
                {
                    "family_key": target.family_key,
                    "round_id": round_id,
                    "from_pool": "pool2",
                    "to_pool": "pool1",
                    "reason": "p2:out_low_score_merge",
                    "score": target_score,
                }
            )
        elif (
            not target_pool2
            and target_score >= P2_IN
            and int(target.count_now or 0) + int(source.count_now or 0) > 0
        ):
            conn.execute(
                "INSERT INTO pool2 (family_key, round_id, reason, explainer_only) "
                "VALUES (%s, %s, 'p2:in_merge', %s) "
                "ON CONFLICT (family_key) DO UPDATE SET reason = EXCLUDED.reason",
                (target.family_key, round_id, target_explainer),
            )
            transitions.append(
                {
                    "family_key": target.family_key,
                    "round_id": round_id,
                    "from_pool": "pool1",
                    "to_pool": "pool2",
                    "reason": "p2:in_merge",
                    "score": target_score,
                }
            )

    # ---- 源条目：合并后撤出（保留行，audit_event 外键不断）
    source_score = _clamp_score(float(source.score) - float(source.audit_adjustment or 0.0))
    source_evidence = (
        dict(source.evidence_json) if isinstance(source.evidence_json, Mapping) else {}
    )
    source_evidence["merged_into"] = target.family_key
    conn.execute(
        "UPDATE pool1 SET status = 'out', score = %s, explainer_only = false, "
        "audit_state = 'human_merge', audit_hash = %s, audit_adjustment = 0, "
        "evidence_json = %s::jsonb, updated_at = now() WHERE family_key = %s",
        (
            float(source_score),
            source_hash,
            json.dumps(source_evidence, ensure_ascii=False),
            source.family_key,
        ),
    )
    if source_pool2:
        conn.execute("DELETE FROM pool2 WHERE family_key = %s", (source.family_key,))
        transitions.append(
            {
                "family_key": source.family_key,
                "round_id": round_id,
                "from_pool": "pool2",
                "to_pool": "pool1",
                "reason": "p2:out_merge",
                "score": source_score,
            }
        )
    transitions.append(
        {
            "family_key": source.family_key,
            "round_id": round_id,
            "from_pool": "pool1",
            "to_pool": None,
            "reason": "p1:merge",
            "score": source_score,
        }
    )

    # ---- family_alias 迁移到目标（避免 norm_alias 唯一冲突：先插后删）
    alias_rows = conn.execute(
        "SELECT alias, norm_alias FROM family_alias WHERE family_key = %s",
        (source.family_key,),
    ).fetchall()
    for alias, norm_alias in alias_rows:
        conn.execute(
            "INSERT INTO family_alias (family_key, alias, norm_alias, source, confidence) "
            "VALUES (%s, %s, %s, 'manual', 1.0) "
            "ON CONFLICT (norm_alias) DO UPDATE SET family_key = EXCLUDED.family_key, "
            "source = 'manual', confidence = 1.0",
            (target.family_key, alias, norm_alias),
        )
    conn.execute(
        "DELETE FROM family_alias WHERE family_key = %s", (source.family_key,)
    )

    event_id = _write_audit_event(
        conn,
        family_key=source.family_key,
        round_id=round_id,
        kind="human",
        verdict="merge",
        delta=0.0,
        confidence=confidence,
        reason=reason,
        operator=operator,
        model="",
        prompt_version=MERGE_PROMPT_VERSION,
        evidence_hash=source_hash,
    )
    transition_count = _insert_transitions(conn, transitions)
    return {
        "ok": True,
        "skipped": False,
        "kind": "human",
        "verdict": "merge",
        "family_key": source.family_key,
        "merged_into": target.family_key,
        "evidence_hash": source_hash,
        "delta": 0.0,
        "audit_state": "human_merge",
        "source_score_before": _round3(source.score),
        "source_score_after": _round3(source_score),
        "target_score_before": _round3(target.score),
        "target_score_after": _round3(target_score),
        "target_status": target_status,
        "target_pool2": target.family_key in load_pool2_keys(conn),
        "transitions": transitions,
        "transition_count": transition_count,
        "event_id": event_id,
        "operator": operator,
        "reason": reason,
    }


# ---------------------------------------------------------------- LLM 审核
def _verifier_verdict(verification: Any) -> tuple[str, str]:
    """把 SemanticVerifier 的 Verification（或 dict）映射成审核三态（不伪造结论）。"""
    if isinstance(verification, Mapping):
        error = str(verification.get("error") or "")
        is_meme = bool(verification.get("is_meme", False))
        is_current = bool(verification.get("is_current", False))
        confidence_value = verification.get("confidence", 0.0)
    else:
        error = str(getattr(verification, "error", "") or "")
        is_meme = bool(getattr(verification, "is_meme", False))
        is_current = bool(getattr(verification, "is_current", False))
        confidence_value = getattr(verification, "confidence", 0.0)
    if error:
        return "error", error
    try:
        confidence = float(confidence_value or 0.0)
    except (TypeError, ValueError):
        return "error", "invalid_verification"
    if not is_meme or not is_current:
        return "fail", ""
    if confidence >= 0.5:
        return "pass", ""
    return "unclear", ""


def _meme_from_pool1(row: Pool1Row) -> Any:
    from .models import Evidence, Meme

    evidence = [
        Evidence(
            comment_id=str(item.get("doc_id") or item.get("comment_id") or ""),
            quote=str(item.get("quote") or ""),
            ctime=int(item.get("ctime") or 0),
            like=int(item.get("like") or 0),
            bvid=str(item.get("bvid") or ""),
            kind=str(item.get("kind") or "comment"),
            video_title=str(item.get("video_title") or ""),
            zone=str(item.get("zone") or ""),
        )
        for item in _evidence_items(row)
    ]
    return Meme(
        family_key=row.family_key,
        canonical=row.canonical,
        norm=normalize_surface(row.canonical),
        members=[row.canonical, *row.aliases],
        evidence=evidence,
        known=bool(row.known or row.audit_state == "human_pass"),
        signals={
            "usage_docs": int(row.count_now or 0),
            "audit_state": row.audit_state,
            "explainer_only": row.explainer_only,
        },
    )


def run_audit_round(
    conn: Any = None,
    *,
    url: str | None = None,
    limit: int = DEFAULT_RUN_LIMIT,
    verifier: Any = None,
    now: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """LLM 审核一轮：队列 → verifier → audit_event/pool1（可选写库）。

    - 默认关闭（``MEME_INFO_VERIFY!=1``）：返回 ``enabled=false``，0 调用 0 写入；
    - ``verifier`` 可注入 fake（测试/影子运行），不做真实网络调用；
    - ``dry_run=True`` 只返回计划，不写库；
    - 队列上限 ``limit``（默认 5）控制单轮 LLM 调用次数。
    """
    state = _state_module()
    now_ts_value = int(now if now is not None else now_ts())
    limit = max(0, int(limit))

    def _run(connection: Any) -> dict[str, Any]:
        queue = build_audit_queue(connection, limit=limit, now=now_ts_value)
        enabled = verifier is not None or llm_enabled()
        base: dict[str, Any] = {
            "ok": True,
            "schema_version": AUDIT_SCHEMA_VERSION,
            "enabled": bool(enabled),
            "dry_run": bool(dry_run),
            "queue": len(queue),
            "limit": int(limit),
            "audited": 0,
            "skipped": 0,
            "errors": [],
            "results": [],
            "candidates": [candidate.to_dict() for candidate in queue],
        }
        if not enabled:
            base["reason"] = "llm_disabled"
            base["results"] = []
            return base
        if not queue:
            base["reason"] = "empty_queue"
            return base
        active_verifier = verifier
        if active_verifier is None:
            from .verify import SemanticVerifier

            active_verifier = SemanticVerifier()
        for candidate in queue:
            row = load_pool1_row(connection, candidate.family_key)
            if row is None:  # pragma: no cover - 并发删除
                continue
            try:
                verification = active_verifier.verify_meme(
                    _meme_from_pool1(row), now=now_ts_value
                )
            except Exception as exc:  # noqa: BLE001 - LLM 失败不影响流水线
                base["errors"].append(
                    {"family_key": candidate.family_key, "error": type(exc).__name__}
                )
                continue
            verdict, error = _verifier_verdict(verification)
            if verdict == "error":
                base["errors"].append(
                    {"family_key": candidate.family_key, "error": error or "llm_error"}
                )
                continue
            item = {
                "family_key": candidate.family_key,
                "verdict": verdict,
                "confidence": getattr(verification, "confidence", None),
                "reason": str(getattr(verification, "reason", "") or "")[:200],
                "model": str(getattr(verification, "model", "") or ""),
                "prompt_version": str(
                    getattr(verification, "prompt_version", "") or ""
                ),
                "evidence_hash": candidate.evidence_hash,
                "dry_run": bool(dry_run),
            }
            if dry_run:
                base["results"].append(item)
                continue
            try:
                written = record_audit(
                    connection,
                    family_key=candidate.family_key,
                    kind="llm",
                    verdict=verdict,
                    reason=item["reason"],
                    confidence=item["confidence"],
                    model=item["model"],
                    prompt_version=item["prompt_version"],
                    evidence_hash=candidate.evidence_hash,
                )
            except AuditError as exc:
                base["errors"].append(
                    {"family_key": candidate.family_key, "error": type(exc).__name__}
                )
                continue
            item.update(
                {
                    "skipped": written.get("skipped", False),
                    "skip_reason": written.get("skip_reason", ""),
                    "delta": written.get("delta"),
                    "score_after": written.get("score_after"),
                    "audit_state": written.get("audit_state"),
                    "pool2_after": written.get("pool2_after"),
                }
            )
            base["results"].append(item)
            if written.get("skipped"):
                base["skipped"] += 1
            else:
                base["audited"] += 1
        return base

    if conn is not None:
        return _run(conn)
    with state.transaction(default_audit_url(url)) as own_conn:
        return _run(own_conn)


# ---------------------------------------------------------------- 汇总（CLI / Admin）
def audit_stats(conn: Any) -> dict[str, Any]:
    """审核统计（只读；不伪造调用次数）。"""
    totals: dict[str, int] = {}
    by_kind_verdict: dict[str, int] = {}
    for kind, verdict, number in conn.execute(
        "SELECT kind, verdict, count(*) FROM audit_event GROUP BY kind, verdict"
    ).fetchall():
        by_kind_verdict[f"{kind}:{verdict}"] = int(number)
        totals[kind] = totals.get(kind, 0) + int(number)
    audited = conn.execute(
        "SELECT count(*) FROM pool1 WHERE audit_state IS NOT NULL "
        "AND audit_state <> 'none'"
    ).fetchone()
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "llm_enabled": llm_enabled(),
        "events": sum(totals.values()),
        "by_kind": totals,
        "by_kind_verdict": by_kind_verdict,
        "audited_families": int(audited[0]) if audited else 0,
        "pool2_total": conn.execute("SELECT count(*) FROM pool2").fetchone()[0],
    }
