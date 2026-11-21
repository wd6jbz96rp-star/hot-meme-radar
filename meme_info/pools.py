"""Phase D：family 观察 → 可信度/比例分数 → 池1/池2 迁移 → Postgres 状态写回。

本模块把 Phase C 的 :class:`~meme_info.models.WindowSample` 转成 family 级观察，
按设计公式计算 cred/score，并（可选）写入 ``pool1`` / ``pool2`` /
``family_round_stat`` / ``family_alias`` / ``pool_transition``。

设计口径（对应 ``docs/pm/REARCHITECTURE.md`` §3.4–§3.8、§5.5–§5.9）：

1. **观察抽取**（:func:`build_observations`）
   - 15 分钟窗口 ``sample.comments/danmaku`` → 比例计数（评论按 id、弹幕按 dmid 去重）；
   - 30 天证据 ``sample.evidence_comments/danmaku`` → 证据/发现与独立视频数；
   - 采样目标视频标题 → :func:`~meme_info.discover.extract_meta_mentions` 元话语；
   - 复用 :func:`~meme_info.discover.extract_candidates` + ``merge_families`` 做归并，
     不重写归并逻辑；上一轮 pool1 的 family 用别名做「粘性」身份对齐，避免
     canonical 抖动导致 family_key 漂移。
   - 计数（PM 2026-11-22 复核修正）：``comment_count`` / ``danmaku_count`` /
     ``video_count`` 全部来自 15 分钟窗口（video_count=窗口命中文档的独立 bvid）；
     30 天证据独立视频只作 ``evidence_video_count``（仅 evidence_json/stats）；
     ``meta_count`` 为 30 天元话语视频。``count_total = 评论+弹幕+窗口视频``，
     meta 与 evidence_video_count 不参与比例；``usage_count = 评论+弹幕``
     （池2「有使用」门槛，口径统一）。
   - 上一轮 pool1 中 active（normal/provisional）但本轮没出现的 family 补
     count=0 观察，触发缺席惩罚。

2. **可信度**：``cred_base = 100*clamp(0.30*meta + 0.30*usage + 0.15*spread
   + 0.10*fresh + 0.15*prior, 0, 1)``，五特征口径见 :class:`PoolConfig`。
   **usage_strength 使用 30 天证据计数**（证据评论/dmid/独立 bvid，PM 2026-11-22 复核）；
   15 分钟窗口计数只用于 ``count_total``/比例/P2 ``usage_count``（两者口径分离）。

3. **初始分/更新分**
   - 新 family 不参与第一轮比例：有使用 ``clamp(cred_base,0,80)``；
     元话语-only（强元话语 + 专门梗视频/≥2 独立元话语/种子或人工）30–35
     ``provisional``；种子/人工 + 强元话语 ``max(40, cred_base)``。
   - 已有 family：``ratio=(count_now+5)/(count_prev+5)``、
     ``delta=clamp(5*log2(ratio),-10,10)``、
     ``score=clamp(0.85*prev+0.15*cred_base+delta,0,100)``；
     ``audit_adjustment`` 只记录「当前已应用的最新审核分」，用于 Phase F 替换旧结论，
     不再每轮重新加到 score（PM 2026-11-22 复核：一次性事件，否则审核分重复放大）；
     本轮 0：prev>0 → -5；prev=0 → -2/轮，zero_streak>=3 再额外 -2；
     provisional 前 2 轮零计数宽限 -0.5。
   - 采集 ``degraded``：非冷启动时冻结分数与比例、不降池，只更新证据/审计字段；
     冷启动例外（``degraded`` 且上一轮无任何 active family）不冻结，按正常初始分
     规则建池（``cold_start=true``，``status`` 仍为 degraded，pool2 门槛不放水）。

4. **池迁移**：P1_in=40 / P1_out=32；P2_in=65 / P2_out=55（滞回）。
   普通条目进池2：``status=normal`` 且 ``score>=65`` 且 ``usage_count>0``。
   explainer-only 例外：强元话语 + 种子/人工确认 + 无当前使用，可进池2，
   每轮最多 2 条，``meta_only`` 且 ``provisional`` 不放行；连续 3 轮无使用且
   无新元话语降池。2×2 只写 ``evidence_json`` 徽标，不参与硬门槛。
   pool1 撤出时同步删除 pool2，迁移写 ``pool_transition``。

5. **数据库**：只通过 :func:`meme_info.state.transaction` 读写；不改 ``round``，
   不写 ``audit_event``；``audit_adjustment`` 只从 pool1 透传（Phase F 用
   ``score - old_adjustment + new_adjustment`` 应用并写回，Phase D 不再累加该字段）。
   纯内存模式（``use_db=False``）返回同样结构的 :class:`PoolRoundResult`。

降级/可解释约定：
- 观测不到但存在于上一轮 pool1 的 out family 不补 0（out 不占活跃位；重新出现时
  会带 count_prev 参与比例）；
- ``pool1.evidence_json`` 使用 dict 包装：``{"items": 前5条证据, "badge_2x2": bool,
  "badge_docs": n, "badge_videos": n, "updated_round": id}``；
- 纯内存模式没有 ``round_id`` 也能算分，但写库必须给定且 round 必须存在。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .discover import (
    DiscoveryConfig,
    extract_candidates,
    extract_meta_mentions,
    merge_families,
)
from .knowledge import Knowledge
from .models import (
    Doc,
    FamilyAliasRow,
    FamilyObservation,
    FamilyRoundStat,
    MetaMention,
    Pool1Row,
    Pool2Row,
    PoolRoundResult,
    PoolTransitionRow,
    Video,
    WindowComment,
    WindowDanmaku,
    WindowSample,
    WindowTarget,
    clamp,
    iso,
    now_ts,
)
from .text import clip, find_quote, normalize_surface, normalize_text

# ---------------------------------------------------------------- 配置
#: 元话语强度（§5.5.2：专门梗视频=1.0 / 盘点解释=0.60 / 搜索关注=0.30）
META_KIND_STRENGTH: dict[str, float] = {
    "dedicated": 1.0,
    "compilation": 0.60,
    "expansion": 0.60,
    "quote": 0.30,
    "search_interest": 0.30,
}

#: 池状态（pool1.status）
STATUS_NORMAL = "normal"
STATUS_PROVISIONAL = "provisional"
STATUS_OUT = "out"
ACTIVE_STATUSES = (STATUS_NORMAL, STATUS_PROVISIONAL)


def sat(value: float, anchor: float = 1.0) -> float:
    """线性饱和：``clamp(value/anchor, 0, 1)``（设计里的 sat(x/40) 等口径）。"""
    if anchor <= 0:
        return 0.0
    return clamp(float(value) / float(anchor), 0.0, 1.0)


@dataclass
class PoolConfig:
    """Phase D 参数（默认值 = 已确认设计值；测试可按需覆盖）。"""

    # 池阈值（滞回）
    p1_in: float = 40.0
    p1_out: float = 32.0
    p2_in: float = 65.0
    p2_out: float = 55.0
    # 比例公式
    ratio_alpha: float = 5.0
    ratio_k: float = 5.0
    ratio_cap: float = 10.0
    # 分数平滑
    smooth_prev: float = 0.85
    smooth_cred: float = 0.15
    # 初始分
    new_score_cap: float = 80.0
    provisional_score_low: float = 30.0
    provisional_score_high: float = 35.0
    seed_meta_min_score: float = 40.0
    # 零计数惩罚
    zero_from_usage: float = -5.0
    zero_absent: float = -2.0
    zero_streak_extra: float = -2.0
    zero_streak_extra_at: int = 3
    provisional_grace_rounds: int = 2
    provisional_grace_delta: float = -0.5
    # explainer-only
    explainer_max_per_round: int = 2
    explainer_idle_rounds: int = 3
    # cred 特征锚点
    strong_meta: float = 0.60
    usage_comment_anchor: float = 40.0
    usage_danmaku_anchor: float = 200.0
    usage_video_anchor: float = 12.0
    spread_zone_anchor: float = 3.0
    spread_video_anchor: float = 10.0
    spread_channel_anchor: float = 2.0
    freshness_days: float = 30.0
    #: 「30 天元话语」过滤（目标视频有 pubdate 时生效；WindowTarget 无 pubdate 视为本轮）
    meta_max_age_days: int = 30
    #: 复用 discover 的候选抽取参数
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)


# ---------------------------------------------------------------- 小工具
def _state_module() -> Any:
    """延迟 import：纯内存模式不要求 psycopg 可用。"""
    from . import state as state_module

    return state_module


def _clamp_score(value: float) -> float:
    return round(clamp(float(value), 0.0, 100.0), 3)


def _round3(value: float) -> float:
    return round(float(value), 3)


@lru_cache(maxsize=4096)
def _ascii_alias_re(alias: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")


def alias_hit(norm_text: str, alias: str) -> bool:
    """归一化文本是否命中归一化别名（ASCII 走词边界，CJK 走子串）。"""
    if not norm_text or not alias:
        return False
    if alias.isascii():
        return _ascii_alias_re(alias).search(norm_text) is not None
    return alias in norm_text


def _norm_aliases(surfaces: Iterable[str]) -> list[str]:
    """归一化别名去重列表（丢弃 <2 字符的空别名，避免单字过匹配）。"""
    result: list[str] = []
    seen: set[str] = set()
    for surface in surfaces:
        norm = normalize_surface(str(surface or ""))
        if len(norm) < 2 or norm in seen:
            continue
        seen.add(norm)
        result.append(norm)
    return result


def _alias_surface_map(surfaces: Iterable[str]) -> dict[str, str]:
    """归一化别名 → 原始展示别名（去重，保留第一个）。"""
    result: dict[str, str] = {}
    for surface in surfaces:
        text = str(surface or "").strip()
        norm = normalize_surface(text)
        if len(norm) < 2 or norm in result:
            continue
        result[norm] = text
    return result


# ---------------------------------------------------------------- 语料 → 使用项
def _usage_from_comments(comments: Sequence[WindowComment]) -> list[Any]:
    from .models import Usage

    items: list[Any] = []
    seen: set[str] = set()
    for comment in comments:
        key = str(comment.id or "")
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(
            Usage(
                doc_id=key,
                bvid=comment.bvid,
                zone=comment.zone,
                kind="comment",
                ctime=int(comment.ctime or 0),
                like=int(comment.like or 0),
                text=comment.text,
                video_title=comment.video_title,
            )
        )
    return items


def _usage_from_danmaku(danmaku: Sequence[WindowDanmaku]) -> list[Any]:
    from .models import Usage

    items: list[Any] = []
    seen: set[str] = set()
    for item in danmaku:
        key = str(item.dmid or item.id or "")
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(
            Usage(
                doc_id=key,
                bvid=item.bvid,
                zone=item.zone,
                kind="danmaku",
                ctime=int(item.ctime or 0),
                like=0,
                text=item.text,
                video_title=item.video_title,
            )
        )
    return items


def _usage_items(sample: WindowSample, *, evidence: bool) -> list[Any]:
    if evidence:
        return _usage_from_comments(sample.evidence_comments) + _usage_from_danmaku(
            sample.evidence_danmaku
        )
    return _usage_from_comments(sample.comments) + _usage_from_danmaku(sample.danmaku)


def _scan_usage(items: Sequence[Any], alias_norms: Sequence[str]) -> list[tuple[Any, str]]:
    """逐条使用证据匹配 family 别名；每条最多计一次（返回 (item, 命中别名)）。"""
    hits: list[tuple[Any, str]] = []
    if not alias_norms:
        return hits
    for item in items:
        text = normalize_text(item.text or "")
        if not text:
            continue
        for alias in alias_norms:
            if alias_hit(text, alias):
                hits.append((item, alias))
                break
    return hits


# ---------------------------------------------------------------- 目标视频 / 元话语
def _video_list_from_targets(sample: WindowSample) -> list[Video]:
    videos: list[Video] = []
    seen: set[str] = set()
    for target in list(sample.comment_targets) + list(sample.danmaku_targets):
        if not isinstance(target, WindowTarget) or not target.bvid or target.bvid in seen:
            continue
        seen.add(target.bvid)
        videos.append(
            Video(
                bvid=target.bvid,
                aid=int(target.aid or 0),
                cid=target.cid,
                title=target.title or "",
                zone=target.zone or "",
                source=f"target:{target.source}" if target.source else "target",
            )
        )
    return videos


def _round_videos(conn: Any, round_id: int) -> dict[str, Video]:
    """读本轮 ``video_metric`` 的视频元数据（仅补 pubdate/zone/title，用于 30 天元话语过滤）。"""
    rows = conn.execute(
        "SELECT bvid, title, zone, COALESCE(pubdate, 0), COALESCE(view, 0) "
        "FROM video_metric WHERE round_id = %s",
        (int(round_id),),
    ).fetchall()
    videos: dict[str, Video] = {}
    for row in rows:
        videos[str(row[0])] = Video(
            bvid=str(row[0]),
            title=str(row[1] or ""),
            zone=str(row[2] or ""),
            pubdate=int(row[3] or 0),
            view=int(row[4] or 0),
            source="video_metric",
        )
    return videos


def _merge_target_videos(
    sample: WindowSample, extra: Mapping[str, Video] | None
) -> list[Video]:
    videos = _video_list_from_targets(sample)
    if not extra:
        return videos
    merged: list[Video] = []
    for video in videos:
        known = extra.get(video.bvid)
        if known is None:
            merged.append(video)
            continue
        merged.append(
            Video(
                bvid=video.bvid,
                aid=video.aid or known.aid,
                cid=video.cid if video.cid is not None else known.cid,
                title=video.title or known.title,
                zone=video.zone or known.zone,
                pubdate=video.pubdate or known.pubdate,
                view=known.view,
                source=video.source,
            )
        )
    return merged


# ---------------------------------------------------------------- 归并身份
@dataclass
class _FamilyGroup:
    """合并后的 family 身份（本轮发现 + 上一轮 sticky 对齐）。"""

    family_key: str
    canonical: str = ""
    aliases: list[str] = field(default_factory=list)
    known: bool = False
    human_confirmed: bool = False
    seed_origin: str = ""
    sources: list[str] = field(default_factory=list)
    meta_mentions: list[MetaMention] = field(default_factory=list)
    meme_prob: float = 0.0
    fetched: bool = False          # 本轮是否来自发现结果（True = 出现）


def _family_alias_surfaces(family: Any) -> list[str]:
    surfaces: list[str] = [family.canonical, family.norm]
    surfaces.extend(family.members or [])
    for candidate in family.candidates or []:
        surfaces.append(candidate.surface)
        surfaces.append(candidate.canonical)
    for mention in family.meta or []:
        surfaces.append(mention.surface)
        surfaces.append(mention.canonical)
    return [surface for surface in surfaces if surface]


def _prev_alias_index(prev_pool1: Mapping[str, Pool1Row]) -> dict[str, str]:
    """上一轮 pool1 的归一化别名 → family_key（先登记 key 本身，再登记别名）。"""
    index: dict[str, str] = {}
    for key, row in prev_pool1.items():
        index[normalize_surface(key)] = key
    for key, row in prev_pool1.items():
        for surface in [row.canonical, *row.aliases]:
            norm = normalize_surface(surface)
            if len(norm) >= 2:
                index.setdefault(norm, key)
    return index


def _group_families(
    families: Sequence[Any],
    prev_pool1: Mapping[str, Pool1Row],
    knowledge: Knowledge,
) -> dict[str, _FamilyGroup]:
    """按 sticky 别名把本轮 family 对齐到上一轮 family_key，并合并同族。"""
    prev_index = _prev_alias_index(prev_pool1)
    buckets: dict[str, list[Any]] = {}
    for family in families:
        own_key = family.family_key
        if own_key in prev_pool1:
            resolved = own_key
        else:
            matched: set[str] = set()
            for surface in _family_alias_surfaces(family):
                norm = normalize_surface(surface)
                if not norm:
                    continue
                target = prev_index.get(norm)
                if target:
                    matched.add(target)
            if not matched:
                resolved = own_key
            else:
                resolved = sorted(
                    matched, key=lambda key: (-prev_pool1[key].score, key)
                )[0]
        buckets.setdefault(resolved, []).append(family)

    groups: dict[str, _FamilyGroup] = {}
    for key, bucket in buckets.items():
        prev = prev_pool1.get(key)
        ordered = sorted(
            bucket,
            key=lambda family: (
                -int(bool(family.known)),
                -float(getattr(family, "meme_prob", 0.0) or 0.0),
                family.family_key,
            ),
        )
        base = ordered[0]
        aliases: list[str] = []
        seen_norm: set[str] = set()

        def add_alias(surface: str) -> None:
            norm = normalize_surface(surface)
            if not norm or norm in seen_norm:
                return
            seen_norm.add(norm)
            aliases.append(surface)

        if prev is not None:
            add_alias(prev.canonical)
            for alias in prev.aliases:
                add_alias(alias)
        add_alias(base.canonical)
        for surface in _family_alias_surfaces(base):
            add_alias(surface)
        sources: set[str] = set()
        mention_keys: set[tuple[str, str]] = set()
        mentions: list[MetaMention] = []
        known = bool(prev is not None and prev.audit_state == "human_pass")
        seed_origin = ""
        meme_prob = 0.0
        for family in ordered:
            for surface in _family_alias_surfaces(family):
                add_alias(surface)
            known = known or bool(family.known)
            seed_origin = seed_origin or (family.seed_origin or "")
            meme_prob = max(meme_prob, float(getattr(family, "meme_prob", 0.0) or 0.0))
            sources.update(family.sources or [])
            for mention in family.meta or []:
                marker = (mention.bvid, mention.norm)
                if marker in mention_keys:
                    continue
                mention_keys.add(marker)
                mentions.append(mention)
        if not known and prev is not None:
            entry = knowledge.lookup(prev.canonical)
            if entry is not None:
                known = True
                seed_origin = seed_origin or entry.origin
        canonical = (prev.canonical if prev is not None and prev.canonical else "") or base.canonical
        groups[key] = _FamilyGroup(
            family_key=key,
            canonical=canonical,
            aliases=aliases,
            known=known,
            human_confirmed=bool(prev is not None and prev.audit_state == "human_pass"),
            seed_origin=seed_origin,
            sources=sorted(sources),
            meta_mentions=mentions,
            meme_prob=meme_prob,
            fetched=True,
        )
    return groups


def _meta_matches(
    mentions: Sequence[MetaMention], alias_norms: Sequence[str], family_key: str
) -> list[MetaMention]:
    targets = set(alias_norms)
    targets.add(normalize_surface(family_key))
    matched: list[MetaMention] = []
    for mention in mentions:
        norm = normalize_surface(mention.norm or mention.surface)
        if norm and norm in targets:
            matched.append(mention)
    return matched


# ---------------------------------------------------------------- 观察抽取
def _prev_evidence_items(prev: Pool1Row | None) -> list[dict[str, Any]]:
    if prev is None:
        return []
    payload = prev.evidence_json or {}
    if isinstance(payload, dict):
        items = payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _prev_latest_ts(prev: Pool1Row | None) -> int:
    latest = 0
    for item in _prev_evidence_items(prev):
        try:
            latest = max(latest, int(item.get("ctime") or 0))
        except (TypeError, ValueError):
            continue
    return latest


def _observation_from_group(
    key: str,
    group: _FamilyGroup,
    prev: Pool1Row | None,
    *,
    window_items: Sequence[Any],
    evidence_items: Sequence[Any],
    now: int,
    config: PoolConfig,
    knowledge: Knowledge,
) -> FamilyObservation:
    aliases = list(group.aliases)
    alias_norms = _norm_aliases(aliases)
    if prev is not None:
        for surface in [prev.canonical, *prev.aliases]:
            norm = normalize_surface(surface)
            if len(norm) >= 2 and norm not in alias_norms:
                alias_norms.append(norm)

    window_hits = _scan_usage(window_items, alias_norms)
    evidence_hits = _scan_usage(evidence_items, alias_norms)
    comment_count = sum(1 for item, _ in window_hits if item.kind == "comment")
    danmaku_count = sum(1 for item, _ in window_hits if item.kind == "danmaku")
    # 比例计数口径：video_count 只取 15 分钟窗口命中文档的独立 bvid。
    window_bvids = {item.bvid for item, _ in window_hits if item.bvid}
    video_count = len(window_bvids)
    # 30 天证据计数：只用于 cred_base 的 usage_strength / 证据展示，不进比例。
    evidence_comment_count = sum(
        1 for item, _ in evidence_hits if item.kind == "comment"
    )
    evidence_danmaku_count = sum(
        1 for item, _ in evidence_hits if item.kind == "danmaku"
    )
    evidence_video_count = len({item.bvid for item, _ in evidence_hits if item.bvid})

    mentions = _meta_matches(group.meta_mentions, alias_norms, key)
    meta_bvids = sorted({mention.bvid for mention in mentions if mention.bvid})
    meta_kinds = sorted({mention.kind for mention in mentions})
    meta_strength = max(
        (META_KIND_STRENGTH.get(kind, 0.30) for kind in meta_kinds), default=0.0
    )
    strong_kinds = ("dedicated", "compilation", "expansion")
    meta_independent = len(
        {mention.bvid for mention in mentions if mention.kind in strong_kinds and mention.bvid}
    )
    dedicated_meta = len(
        {mention.bvid for mention in mentions if mention.kind == "dedicated" and mention.bvid}
    )
    meta_count = len(meta_bvids)

    zones: set[str] = set()
    hit_ts = 0
    for item, _alias in window_hits + evidence_hits:
        if item.zone:
            zones.add(str(item.zone))
        hit_ts = max(hit_ts, int(item.ctime or 0))
    for mention in mentions:
        hit_ts = max(hit_ts, int(mention.pubdate or 0))
    if not hit_ts and mentions:
        # WindowTarget 没有 pubdate：本轮目标视频的元话语视为「当前」，freshness=1。
        hit_ts = now

    badge_meta_bvids = set(meta_bvids)
    badge_hits = [
        (item, alias)
        for item, alias in window_hits + evidence_hits
        if item.bvid not in badge_meta_bvids
    ]
    badge_docs = len({item.doc_id for item, _ in badge_hits})
    badge_videos = len({item.bvid for item, _ in badge_hits if item.bvid})
    badge_2x2 = badge_docs >= 2 and badge_videos >= 2

    items_sorted = sorted(
        window_hits,
        key=lambda pair: (
            -int(getattr(pair[0], "like", 0) or 0),
            -int(pair[0].ctime or 0),
            str(pair[0].doc_id),
        ),
    )
    alias_surface = _alias_surface_map(aliases)
    evidence_json: list[dict[str, Any]] = []
    for item, alias in items_sorted[:5]:
        quote = find_quote(item.text or "", alias_surface.get(alias, alias)) or clip(
            item.text or "", 80
        )
        evidence_json.append(
            {
                "doc_id": item.doc_id,
                "bvid": item.bvid,
                "kind": item.kind,
                "quote": quote,
                "ctime": int(item.ctime or 0),
                "like": int(getattr(item, "like", 0) or 0),
                "zone": item.zone,
                "video_title": item.video_title,
            }
        )

    count_total = comment_count + danmaku_count + video_count
    usage_count = comment_count + danmaku_count
    latest_ts = hit_ts or _prev_latest_ts(prev)
    if not latest_ts and meta_count:
        latest_ts = now

    observation = FamilyObservation(
        family_key=key,
        canonical=group.canonical,
        norm=normalize_surface(group.canonical),
        aliases=aliases,
        present=True,
        discovered=True,
        comment_count=comment_count,
        danmaku_count=danmaku_count,
        video_count=video_count,
        evidence_comment_count=evidence_comment_count,
        evidence_danmaku_count=evidence_danmaku_count,
        evidence_video_count=evidence_video_count,
        meta_count=meta_count,
        count_total=count_total,
        usage_count=usage_count,
        known=bool(group.known),
        human_confirmed=bool(group.human_confirmed),
        seed_origin=group.seed_origin,
        meta_kinds=meta_kinds,
        meta_bvids=meta_bvids,
        meta_independent=meta_independent,
        dedicated_meta=dedicated_meta,
        meta_strength=meta_strength,
        badge_2x2=badge_2x2,
        badge_docs=badge_docs,
        badge_videos=badge_videos,
        zones=sorted(zones),
        sources=list(group.sources),
        evidence=evidence_json,
        latest_evidence_ts=latest_ts,
    )
    _fill_features(observation, now=now, config=config)
    return observation


def _zero_observation(
    key: str,
    prev: Pool1Row,
    *,
    evidence_items: Sequence[Any],
    now: int,
    config: PoolConfig,
    knowledge: Knowledge,
) -> FamilyObservation:
    """上一轮 active 但本轮没出现：补 count=0 观察（触发缺席惩罚）。

    窗口计数为 0，但仍用上一轮 family 别名回扫 30 天证据：cred_base 的
    usage_strength 是 30 天证据口径，不能因为 15 分钟窗口没出现就把证据可信度清零。
    """
    known = prev.audit_state == "human_pass"
    seed_origin = ""
    entry = knowledge.lookup(prev.canonical)
    if entry is not None:
        known = True
        seed_origin = entry.origin
    alias_norms = _norm_aliases([prev.canonical, *prev.aliases])
    evidence_hits = _scan_usage(evidence_items, alias_norms)
    evidence_comment_count = sum(
        1 for item, _ in evidence_hits if item.kind == "comment"
    )
    evidence_danmaku_count = sum(
        1 for item, _ in evidence_hits if item.kind == "danmaku"
    )
    evidence_video_count = len({item.bvid for item, _ in evidence_hits if item.bvid})
    latest_ts = _prev_latest_ts(prev)
    for item, _alias in evidence_hits:
        latest_ts = max(latest_ts, int(item.ctime or 0))
    observation = FamilyObservation(
        family_key=key,
        canonical=prev.canonical,
        norm=normalize_surface(prev.canonical),
        aliases=list(prev.aliases),
        present=False,
        discovered=False,
        evidence_comment_count=evidence_comment_count,
        evidence_danmaku_count=evidence_danmaku_count,
        evidence_video_count=evidence_video_count,
        known=known,
        human_confirmed=prev.audit_state == "human_pass",
        seed_origin=seed_origin,
        latest_evidence_ts=latest_ts,
    )
    _fill_features(observation, now=now, config=config)
    return observation


def _fill_features(observation: FamilyObservation, *, now: int, config: PoolConfig) -> None:
    """计算 cred_base 五特征与 cred_base。

    usage_strength 用 **30 天证据计数**（PM 2026-11-22 复核）；15 分钟窗口计数
    只用于 count_total/比例/P2 usage，不参与 cred_base。
    """
    meta_strength = clamp(observation.meta_strength, 0.0, 1.0)
    usage_strength = clamp(
        0.45 * sat(observation.evidence_comment_count, config.usage_comment_anchor)
        + 0.25 * sat(observation.evidence_danmaku_count, config.usage_danmaku_anchor)
        + 0.30 * sat(observation.evidence_video_count, config.usage_video_anchor),
        0.0,
        1.0,
    )
    channels: set[str] = set()
    if observation.comment_count:
        channels.add("comment")
    if observation.danmaku_count:
        channels.add("danmaku")
    if observation.video_count:
        channels.add("video")
    if observation.meta_count:
        channels.add("meta")
    if observation.known or observation.human_confirmed:
        channels.add("seed")
    spread = clamp(
        0.50 * sat(len(observation.zones), config.spread_zone_anchor)
        + 0.30 * sat(observation.video_count, config.spread_video_anchor)
        + 0.20 * sat(len(channels), config.spread_channel_anchor),
        0.0,
        1.0,
    )
    latest = int(observation.latest_evidence_ts or 0)
    if latest:
        age_days = max(0.0, (now - latest) / 86400.0)
        freshness = clamp(1.0 - age_days / max(1e-6, config.freshness_days), 0.0, 1.0)
    else:
        freshness = 0.0
    prior = 0.0
    if observation.known or observation.human_confirmed:
        prior = 0.90
    else:
        sources = {str(source) for source in observation.sources}
        has_search = any(source.startswith(("search", "recent-search")) for source in sources)
        has_popular = any(
            source.startswith(("popular", "ranking", "related")) for source in sources
        )
        if has_search and has_popular:
            prior = 0.10  # 搜索+热门双命中 +0.1（其余 prior 信息 Phase D 拿不到）
    cred_base = 100.0 * clamp(
        0.30 * meta_strength
        + 0.30 * usage_strength
        + 0.15 * spread
        + 0.10 * freshness
        + 0.15 * prior,
        0.0,
        1.0,
    )
    observation.usage_strength = _round3(usage_strength)
    observation.spread = _round3(spread)
    observation.freshness = _round3(freshness)
    observation.prior = _round3(prior)
    observation.meta_strength = _round3(meta_strength)
    observation.cred_base = _round3(cred_base)
    # 规则标记
    observation.meta_only = (
        observation.usage_count == 0 and observation.meta_count > 0
    )
    observation.explainer_eligible = bool(
        observation.meta_strength >= config.strong_meta
        and (observation.known or observation.human_confirmed)
        and observation.usage_count == 0
        and observation.meta_count > 0
    )


def build_observations(
    sample: WindowSample,
    *,
    knowledge: Knowledge | None = None,
    now: int | None = None,
    prev_pool1: Mapping[str, Pool1Row] | None = None,
    videos: Sequence[Video] | None = None,
    config: PoolConfig | None = None,
) -> list[FamilyObservation]:
    """把 WindowSample 转成 family 级观察（含上一轮 active 的 count=0 补行）。"""
    config = config or PoolConfig()
    knowledge = knowledge or Knowledge()
    prev_pool1 = prev_pool1 or {}
    now = int(now if now is not None else (sample.window_end or now_ts()))

    window_items = _usage_items(sample, evidence=False)
    evidence_items = _usage_items(sample, evidence=True)
    has_evidence = bool(sample.evidence_comments or sample.evidence_danmaku)

    video_list = list(videos) if videos is not None else _merge_target_videos(sample, None)
    meta_mentions = extract_meta_mentions(
        video_list, knowledge, now=now, max_age_days=config.meta_max_age_days
    )

    discovery_docs = sample.docs(evidence=True) if has_evidence else sample.docs()
    discovery_docs = _dedupe_docs(discovery_docs)
    candidates = extract_candidates(
        discovery_docs,
        knowledge,
        now=now,
        meta_mentions=meta_mentions,
        config=config.discovery,
    )
    families = merge_families(candidates, knowledge, now=now, meta_mentions=meta_mentions)
    groups = _group_families(families, prev_pool1, knowledge)

    observations: list[FamilyObservation] = []
    seen_keys: set[str] = set()
    for key in sorted(groups):
        group = groups[key]
        prev = prev_pool1.get(key)
        observation = _observation_from_group(
            key,
            group,
            prev,
            window_items=window_items,
            evidence_items=evidence_items,
            now=now,
            config=config,
            knowledge=knowledge,
        )
        observations.append(observation)
        seen_keys.add(key)
    for key, prev in prev_pool1.items():
        if key in seen_keys or prev.status not in ACTIVE_STATUSES:
            continue
        observations.append(
            _zero_observation(
                key,
                prev,
                evidence_items=evidence_items,
                now=now,
                config=config,
                knowledge=knowledge,
            )
        )
    observations.sort(key=lambda item: item.family_key)
    return observations


def _dedupe_docs(docs: Sequence[Doc]) -> list[Doc]:
    result: list[Doc] = []
    seen: set[tuple[str, str]] = set()
    for doc in docs:
        marker = (doc.kind, str(doc.id))
        if marker in seen:
            continue
        seen.add(marker)
        result.append(doc)
    return result


# ---------------------------------------------------------------- 分数与池迁移
@dataclass
class _Decision:
    observation: FamilyObservation
    row: Pool1Row | None = None
    stat: FamilyRoundStat | None = None
    transitions: list[PoolTransitionRow] = field(default_factory=list)
    p2_action: str = "none"          # none / keep / insert / delete
    p2_reason: str = ""
    explainer_promotion: bool = False
    skipped: bool = False


def _meta_only_initial(observation: FamilyObservation, config: PoolConfig) -> float | None:
    """元话语-only 新 family 的初始分；不满足 provisional 条件返回 None。"""
    if observation.meta_count <= 0 or observation.meta_strength < config.strong_meta:
        return None
    if observation.known or observation.human_confirmed:
        return None  # 种子/人工走 max(40, cred_base) 分支
    if observation.dedicated_meta > 0 or observation.meta_independent >= 2:
        return round(
            clamp(
                config.provisional_score_low
                + (config.provisional_score_high - config.provisional_score_low)
                * observation.meta_strength,
                config.provisional_score_low,
                config.provisional_score_high,
            ),
            2,
        )
    return None


def _initial_decision(
    observation: FamilyObservation, round_id: int | None, config: PoolConfig
) -> _Decision:
    key = observation.family_key
    has_usage_evidence = observation.count_total > 0
    stat = FamilyRoundStat(
        round_id=round_id,
        family_key=key,
        comment_count=observation.comment_count,
        danmaku_count=observation.danmaku_count,
        video_count=observation.video_count,
        meta_count=observation.meta_count,
        count_total=observation.count_total,
        ratio=None,
        score_before=0.0,
    )
    decision = _Decision(observation=observation, stat=stat)

    if has_usage_evidence:
        score = _clamp_score(min(observation.cred_base, config.new_score_cap))
        status = STATUS_NORMAL if score >= config.p1_in else STATUS_OUT
        meta_only = False
        reason = "new:usage" if status == STATUS_NORMAL else "new:usage_below_p1_in"
    else:
        meta_only_score = _meta_only_initial(observation, config)
        if observation.known or observation.human_confirmed:
            if observation.meta_count <= 0 or observation.meta_strength < config.strong_meta:
                decision.skipped = True
                return decision
            score = _clamp_score(max(config.seed_meta_min_score, observation.cred_base))
            status = STATUS_NORMAL
            meta_only = True
            reason = "new:seed_meta"
        elif meta_only_score is not None:
            score = _clamp_score(meta_only_score)
            status = STATUS_PROVISIONAL
            meta_only = True
            reason = "new:meta_only"
        else:
            decision.skipped = True
            return decision

    evidence_payload = {
        "items": observation.evidence,
        "badge_2x2": observation.badge_2x2,
        "badge_docs": observation.badge_docs,
        "badge_videos": observation.badge_videos,
        "evidence_comment_count": int(observation.evidence_comment_count),
        "evidence_danmaku_count": int(observation.evidence_danmaku_count),
        "evidence_video_count": int(observation.evidence_video_count),
        # Phase E（PM 复核）：cred_base 落 evidence_json，供报告 v2 从 DB 重建 cred_score
        "cred_base": _round3(observation.cred_base),
        "updated_round": round_id,
    }
    row = Pool1Row(
        family_key=key,
        canonical=observation.canonical,
        aliases=list(observation.aliases),
        status=status,
        score=score,
        count_prev=0,
        count_now=observation.count_total,
        zero_streak=1 if observation.count_total == 0 else 0,
        audit_state="none",
        audit_adjustment=0.0,
        meta_only=meta_only,
        explainer_only=False,
        first_seen_round=round_id,
        last_seen_round=round_id if observation.present else None,
        evidence_json=evidence_payload,
        known=bool(observation.known),
    )
    decision.row = row
    stat.score_after = score
    stat.delta = score
    stat.reason = reason
    if status in ACTIVE_STATUSES:
        decision.transitions.append(
            PoolTransitionRow(
                family_key=key,
                round_id=round_id,
                from_pool=None,
                to_pool="pool1",
                reason=reason,
                score=score,
            )
        )
    if status == STATUS_NORMAL and observation.usage_count > 0 and score >= config.p2_in:
        decision.p2_action = "insert"
        decision.p2_reason = "p2:in_score"
    elif observation.explainer_eligible and status == STATUS_NORMAL and meta_only:
        decision.p2_action = "explainer_candidate"
        decision.p2_reason = "p2:in_explainer"
    return decision


def _update_decision(
    observation: FamilyObservation,
    prev: Pool1Row,
    round_id: int | None,
    config: PoolConfig,
) -> _Decision:
    key = observation.family_key
    prev_count = int(prev.count_now or 0)
    count_now = observation.count_total
    stat = FamilyRoundStat(
        round_id=round_id,
        family_key=key,
        comment_count=observation.comment_count,
        danmaku_count=observation.danmaku_count,
        video_count=observation.video_count,
        meta_count=observation.meta_count,
        count_total=count_now,
        score_before=_round3(prev.score),
    )
    decision = _Decision(observation=observation, stat=stat)

    ratio: float | None = None
    delta = 0.0
    reasons: list[str] = []
    zero_streak = int(prev.zero_streak or 0)
    grace_round = False
    if count_now == 0:
        zero_streak += 1
        stat.ratio = 0.0
        if prev_count > 0:
            delta = config.zero_from_usage
            reasons.append("zero:after_usage")
        else:
            grace_round = bool(
                prev.meta_only
                and prev.status == STATUS_PROVISIONAL
                and zero_streak <= config.provisional_grace_rounds
            )
            if grace_round:
                delta = config.provisional_grace_delta
                reasons.append("zero:provisional_grace")
            else:
                delta = config.zero_absent
                reasons.append("zero:absent")
                if zero_streak >= config.zero_streak_extra_at:
                    delta += config.zero_streak_extra
                    reasons.append("zero:streak")
    else:
        zero_streak = 0
        ratio = (count_now + config.ratio_alpha) / (prev_count + config.ratio_alpha)
        delta = clamp(
            config.ratio_k * math.log2(ratio), -config.ratio_cap, config.ratio_cap
        )
        stat.ratio = round(ratio, 4)
        reasons.append(f"ratio:{delta:+.2f}")

    # audit_adjustment 只透传（当前已应用的最新审核分），不再每轮加到 score：
    # Phase F 审核时按 score - old_adjustment + new_adjustment 应用并写回，
    # 审核效果由 score 自身延续，避免每轮重复放大。
    audit_adjustment = float(prev.audit_adjustment or 0.0)
    score = _clamp_score(
        config.smooth_prev * float(prev.score)
        + config.smooth_cred * float(observation.cred_base)
        + delta
    )
    status = prev.status
    meta_only = bool(prev.meta_only)
    explainer_only = bool(prev.explainer_only)
    if observation.usage_count > 0 and (meta_only or status == STATUS_PROVISIONAL):
        status = STATUS_NORMAL
        meta_only = False
        reasons.append("usage_transition")
    if observation.usage_count > 0:
        explainer_only = False

    if grace_round:
        # 宽限期（前 2 轮零计数）不按 P1_out 撤池，等第 3 轮正常零惩罚再淘汰
        reasons.append("p1_grace")
    elif status in ACTIVE_STATUSES:
        if score < config.p1_out:
            status = STATUS_OUT
            reasons.append("p1:out_low_score")
    else:  # 之前是 out
        if score >= config.p1_in:
            status = STATUS_NORMAL
            reasons.append("p1:reentry")
        else:
            status = STATUS_OUT

    evidence_payload = {
        "items": observation.evidence,
        "badge_2x2": observation.badge_2x2,
        "badge_docs": observation.badge_docs,
        "badge_videos": observation.badge_videos,
        "evidence_comment_count": int(observation.evidence_comment_count),
        "evidence_danmaku_count": int(observation.evidence_danmaku_count),
        "evidence_video_count": int(observation.evidence_video_count),
        # Phase E（PM 复核）：cred_base 落 evidence_json，供报告 v2 从 DB 重建 cred_score
        "cred_base": _round3(observation.cred_base),
        "updated_round": round_id,
    }
    last_seen = prev.last_seen_round
    if observation.present:
        last_seen = round_id
    row = Pool1Row(
        family_key=key,
        canonical=observation.canonical or prev.canonical,
        aliases=list(observation.aliases) or list(prev.aliases),
        status=status,
        score=score,
        count_prev=prev_count,
        count_now=count_now,
        zero_streak=zero_streak,
        audit_state=prev.audit_state,
        audit_hash=prev.audit_hash,
        audit_adjustment=audit_adjustment,
        meta_only=meta_only,
        explainer_only=explainer_only,
        first_seen_round=prev.first_seen_round,
        last_seen_round=last_seen,
        evidence_json=evidence_payload,
        known=bool(observation.known),
    )
    decision.row = row
    stat.score_after = score
    stat.delta = _round3(score - float(prev.score))
    stat.reason = ";".join(reasons)

    was_active = prev.status in ACTIVE_STATUSES
    is_active = status in ACTIVE_STATUSES
    if was_active and not is_active:
        decision.transitions.append(
            PoolTransitionRow(
                family_key=key,
                round_id=round_id,
                from_pool="pool1",
                to_pool=None,
                reason="p1:out",
                score=score,
            )
        )
        decision.p2_action = "delete"
        decision.p2_reason = "p2:out_pool1_exit"
    elif not was_active and is_active:
        decision.transitions.append(
            PoolTransitionRow(
                family_key=key,
                round_id=round_id,
                from_pool=None,
                to_pool="pool1",
                reason="p1:reentry",
                score=score,
            )
        )
        if status == STATUS_NORMAL and observation.usage_count > 0 and score >= config.p2_in:
            decision.p2_action = "insert"
            decision.p2_reason = "p2:in_score"
    elif status == STATUS_NORMAL and observation.usage_count > 0 and score >= config.p2_in:
        decision.p2_action = "insert"
        decision.p2_reason = "p2:in_score"
    elif status == STATUS_NORMAL and observation.explainer_eligible and meta_only:
        if prev.explainer_only:
            decision.p2_action = "explainer_keep"
            decision.p2_reason = "p2:explainer_keep"
        else:
            decision.p2_action = "explainer_candidate"
            decision.p2_reason = "p2:in_explainer"
    return decision


def _frozen_decision(
    observation: FamilyObservation,
    prev: Pool1Row,
    round_id: int | None,
    config: PoolConfig,
) -> _Decision:
    """degraded：冻结分数/比例与池成员，仅更新证据与审计字段。"""
    key = observation.family_key
    evidence_payload = {
        "items": observation.evidence,
        "badge_2x2": observation.badge_2x2,
        "badge_docs": observation.badge_docs,
        "badge_videos": observation.badge_videos,
        "evidence_comment_count": int(observation.evidence_comment_count),
        "evidence_danmaku_count": int(observation.evidence_danmaku_count),
        "evidence_video_count": int(observation.evidence_video_count),
        # Phase E（PM 复核）：cred_base 落 evidence_json，供报告 v2 从 DB 重建 cred_score
        "cred_base": _round3(observation.cred_base),
        "updated_round": round_id,
    }
    row = Pool1Row(
        family_key=key,
        canonical=prev.canonical,
        aliases=list(prev.aliases) or list(observation.aliases),
        status=prev.status,
        score=float(prev.score),
        count_prev=int(prev.count_prev or 0),
        count_now=int(prev.count_now or 0),
        zero_streak=int(prev.zero_streak or 0),
        audit_state=prev.audit_state,
        audit_hash=prev.audit_hash,
        audit_adjustment=float(prev.audit_adjustment or 0.0),
        meta_only=bool(prev.meta_only),
        explainer_only=bool(prev.explainer_only),
        first_seen_round=prev.first_seen_round,
        last_seen_round=prev.last_seen_round,
        evidence_json=evidence_payload,
        known=bool(observation.known),
    )
    stat = FamilyRoundStat(
        round_id=round_id,
        family_key=key,
        comment_count=observation.comment_count,
        danmaku_count=observation.danmaku_count,
        video_count=observation.video_count,
        meta_count=observation.meta_count,
        count_total=observation.count_total,
        ratio=None,
        score_before=_round3(prev.score),
        score_after=_round3(prev.score),
        delta=0.0,
        reason="degraded:frozen",
    )
    return _Decision(observation=observation, row=row, stat=stat, p2_action="keep_frozen")


def _apply_explainer_cap(
    decisions: list[_Decision], config: PoolConfig
) -> None:
    """explainer-only 每轮最多 2 条：按分数/元话语强度排序取前 N。"""
    candidates = [item for item in decisions if item.p2_action == "explainer_candidate"]
    candidates.sort(
        key=lambda item: (
            -float(item.row.score if item.row else 0.0),
            -float(item.observation.meta_strength),
            item.observation.family_key,
        )
    )
    for index, item in enumerate(candidates):
        if index < config.explainer_max_per_round:
            item.p2_action = "insert_explainer"
            item.p2_reason = "p2:in_explainer"
            item.explainer_promotion = True
            item.row.explainer_only = True
        else:
            item.p2_action = "none"
            item.p2_reason = ""


def _p2_rows(
    decisions: list[_Decision],
    prev_pool2: Mapping[str, Pool2Row],
    round_id: int | None,
    config: PoolConfig,
    stats: dict[str, Any],
) -> dict[str, Pool2Row]:
    result_pool2: dict[str, Pool2Row] = {
        key: Pool2Row(
            family_key=key,
            round_id=row.round_id,
            heat_score=row.heat_score,
            rank=row.rank,
            reason=row.reason,
            explainer_only=bool(row.explainer_only),
        )
        for key, row in prev_pool2.items()
    }
    for decision in decisions:
        row = decision.row
        if row is None:
            continue
        key = row.family_key
        prev2 = prev_pool2.get(key)
        action = decision.p2_action
        if row.status not in ACTIVE_STATUSES:
            if prev2 is not None:
                result_pool2.pop(key, None)
                decision.transitions.append(
                    PoolTransitionRow(
                        family_key=key,
                        round_id=round_id,
                        from_pool="pool2",
                        to_pool=None,
                        reason="p2:out_pool1_exit",
                        score=row.score,
                    )
                )
                stats["p2_exit_with_pool1"] = stats.get("p2_exit_with_pool1", 0) + 1
            row.explainer_only = False
            continue
        if action in ("insert", "insert_explainer"):
            if prev2 is not None:
                # 已在池2：只同步 explainer 标记
                prev2.explainer_only = bool(row.explainer_only)
                prev2.reason = decision.p2_reason or prev2.reason
                if row.explainer_only:
                    stats["explainer_in_pool2"] = stats.get("explainer_in_pool2", 0) + 1
                continue
            result_pool2[key] = Pool2Row(
                family_key=key,
                round_id=round_id,
                heat_score=None,
                rank=None,
                reason=decision.p2_reason or "p2:in",
                explainer_only=bool(row.explainer_only),
            )
            decision.transitions.append(
                PoolTransitionRow(
                    family_key=key,
                    round_id=round_id,
                    from_pool="pool1",
                    to_pool="pool2",
                    reason=decision.p2_reason or "p2:in",
                    score=row.score,
                )
            )
            if action == "insert_explainer":
                stats["p2_in_explainer"] = stats.get("p2_in_explainer", 0) + 1
            else:
                stats["p2_in_normal"] = stats.get("p2_in_normal", 0) + 1
            continue
        if prev2 is None:
            row.explainer_only = False
            continue

        # 已在池2：判断是否降池
        demote_reason = ""
        if action in ("delete", "keep_frozen"):
            # frozen：degraded 轮保持池成员资格，不降池
            if action == "keep_frozen":
                prev2.explainer_only = bool(row.explainer_only)
                continue
            demote_reason = decision.p2_reason or "p2:out"
        elif row.explainer_only:
            # explainer-only：不受 P2_out 低分立即淘汰，按「连续 3 轮无新元话语/无使用」降池
            if (
                row.zero_streak >= config.explainer_idle_rounds
                and decision.observation.meta_count == 0
                and decision.observation.usage_count == 0
            ):
                demote_reason = "p2:out_explainer_idle"
            else:
                prev2.explainer_only = True
        elif row.score < config.p2_out:
            demote_reason = "p2:out_low_score"
        elif row.status != STATUS_NORMAL:
            demote_reason = "p2:out_status"
        if demote_reason:
            result_pool2.pop(key, None)
            decision.transitions.append(
                PoolTransitionRow(
                    family_key=key,
                    round_id=round_id,
                    from_pool="pool2",
                    to_pool="pool1",
                    reason=demote_reason,
                    score=row.score,
                )
            )
            row.explainer_only = False
            if demote_reason == "p2:out_low_score":
                stats["p2_out_low_score"] = stats.get("p2_out_low_score", 0) + 1
            elif demote_reason == "p2:out_explainer_idle":
                stats["p2_out_explainer_idle"] = (
                    stats.get("p2_out_explainer_idle", 0) + 1
                )
            else:
                stats["p2_out_other"] = stats.get("p2_out_other", 0) + 1
        else:
            # 保持池2：保留 Phase E 的 heat/rank，explainer 标记同步
            prev2.reason = prev2.reason or "p2:keep"
            prev2.explainer_only = bool(row.explainer_only)
    return result_pool2


def apply_round(
    observations: Sequence[FamilyObservation],
    *,
    prev_pool1: Mapping[str, Pool1Row] | None = None,
    prev_pool2: Mapping[str, Pool2Row] | None = None,
    round_id: int | None = None,
    degraded: bool = False,
    config: PoolConfig | None = None,
    now: int | None = None,
) -> PoolRoundResult:
    """纯计算：观察 → 新 score/status → 池1/池2 成员与迁移（不碰数据库）。"""
    config = config or PoolConfig()
    prev_pool1 = dict(prev_pool1 or {})
    prev_pool2 = dict(prev_pool2 or {})
    cold_start = bool(degraded) and not any(
        row.status in ACTIVE_STATUSES for row in prev_pool1.values()
    )
    freeze = bool(degraded) and not cold_start
    result = PoolRoundResult(
        generated_at=iso(now if now is not None else now_ts()),
        round_id=round_id,
        status="degraded" if degraded else "ok",
        degraded=bool(degraded),
        cold_start=cold_start,
    )
    result_pool1: dict[str, Pool1Row] = {
        key: Pool1Row(
            family_key=key,
            canonical=row.canonical,
            aliases=list(row.aliases),
            status=row.status,
            score=row.score,
            count_prev=row.count_prev,
            count_now=row.count_now,
            zero_streak=row.zero_streak,
            audit_state=row.audit_state,
            audit_hash=row.audit_hash,
            audit_adjustment=row.audit_adjustment,
            meta_only=row.meta_only,
            explainer_only=row.explainer_only,
            first_seen_round=row.first_seen_round,
            last_seen_round=row.last_seen_round,
            evidence_json=row.evidence_json,
            updated_at=row.updated_at,
            known=row.known,
        )
        for key, row in prev_pool1.items()
    }
    stats: dict[str, Any] = {
        "observations": len(observations),
        "discovered": sum(1 for item in observations if item.discovered),
        "zero_padded": sum(1 for item in observations if not item.discovered),
        "degraded": bool(degraded),
        "cold_start": cold_start,
    }
    decisions: list[_Decision] = []
    for observation in observations:
        key = observation.family_key
        prev = prev_pool1.get(key)
        if freeze:
            if prev is None:
                stats["degraded_new_skipped"] = stats.get("degraded_new_skipped", 0) + 1
                continue
            decision = _frozen_decision(observation, prev, round_id, config)
            stats["frozen"] = stats.get("frozen", 0) + 1
        elif prev is None:
            decision = _initial_decision(observation, round_id, config)
            if decision.skipped:
                stats["skipped_no_pool"] = stats.get("skipped_no_pool", 0) + 1
                continue
            stats["new_pool1"] = stats.get("new_pool1", 0) + 1
            if cold_start:
                stats["cold_start_new_pool1"] = (
                    stats.get("cold_start_new_pool1", 0) + 1
                )
        else:
            decision = _update_decision(observation, prev, round_id, config)
        decisions.append(decision)
        result_pool1[key] = decision.row

    _apply_explainer_cap(decisions, config)
    result_pool2 = _p2_rows(decisions, prev_pool2, round_id, config, stats)

    for decision in decisions:
        row = decision.row
        if row is None:
            continue
        if row.status in ACTIVE_STATUSES:
            stats["pool1_active"] = stats.get("pool1_active", 0) + 1
        if row.status == STATUS_PROVISIONAL:
            stats["pool1_provisional"] = stats.get("pool1_provisional", 0) + 1
        if row.status == STATUS_OUT:
            stats["pool1_out"] = stats.get("pool1_out", 0) + 1
        if decision.transitions:
            reasons = [item.reason for item in decision.transitions]
            if any("p1:out" in reason for reason in reasons):
                stats["p1_exit"] = stats.get("p1_exit", 0) + 1
            if any("p1:reentry" in reason for reason in reasons):
                stats["p1_reentry"] = stats.get("p1_reentry", 0) + 1
        result.transitions.extend(decision.transitions)
        if decision.stat is not None:
            result.stats_rows.append(decision.stat)

    result.pool1 = [result_pool1[key] for key in sorted(result_pool1)]
    result.pool2 = [result_pool2[key] for key in sorted(result_pool2)]
    stats["explainer_in_pool2"] = sum(1 for row in result.pool2 if row.explainer_only)
    result.observations = list(observations)
    result.aliases = _collect_aliases(result.pool1)
    stats["pool1_total"] = len(result.pool1)
    stats["pool2_total"] = len(result.pool2)
    stats["family_round_stats"] = len(result.stats_rows)
    stats["transitions"] = len(result.transitions)
    result.stats = stats
    return result


def _collect_aliases(rows: Sequence[Pool1Row]) -> list[FamilyAliasRow]:
    """为 pool1 全量 family 生成 alias 行（norm_alias 去重，种子优先）。"""
    claimed: dict[str, FamilyAliasRow] = {}
    for row in rows:
        surfaces = [row.canonical, *row.aliases]
        source = "seed" if (row.known or row.audit_state == "human_pass") else "auto"
        for surface in surfaces:
            text = str(surface or "").strip()
            norm = normalize_surface(text)
            if len(norm) < 2 or norm in claimed:
                continue
            claimed[norm] = FamilyAliasRow(
                family_key=row.family_key,
                alias=text,
                norm_alias=norm,
                source=source,
                confidence=0.9 if source == "seed" else 0.5,
            )
    return [claimed[norm] for norm in sorted(claimed)]


# ---------------------------------------------------------------- 纯内存状态续跑
def pool_state_from_result(
    result: PoolRoundResult,
) -> tuple[dict[str, Pool1Row], dict[str, Pool2Row]]:
    """把一轮结果转成下一轮的 prev 状态（测试/影子运行用）。"""
    pool1 = {row.family_key: row for row in result.pool1}
    pool2 = {row.family_key: row for row in result.pool2}
    return pool1, pool2


# ---------------------------------------------------------------- Postgres 读写
def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default


def load_pool_state(
    conn: Any = None,
    *,
    url: str | None = None,
) -> tuple[dict[str, Pool1Row], dict[str, Pool2Row]]:
    """读当前 ``pool1``（含 out 历史）与 ``pool2``（只读）。"""
    sql1 = (
        "SELECT family_key, canonical, aliases_json, status, score, count_prev, "
        "count_now, zero_streak, audit_state, audit_hash, audit_adjustment, "
        "meta_only, explainer_only, first_seen_round, last_seen_round, evidence_json "
        "FROM pool1"
    )
    sql2 = (
        "SELECT family_key, round_id, heat_score, rank, reason, explainer_only "
        "FROM pool2"
    )

    def read(connection: Any) -> tuple[dict[str, Pool1Row], dict[str, Pool2Row]]:
        pool1: dict[str, Pool1Row] = {}
        for row in connection.execute(sql1).fetchall():
            aliases = _json_load(row[2], [])
            pool1[str(row[0])] = Pool1Row(
                family_key=str(row[0]),
                canonical=str(row[1] or ""),
                aliases=[str(item) for item in aliases if item] if isinstance(aliases, list) else [],
                status=str(row[3] or STATUS_NORMAL),
                score=float(row[4] or 0.0),
                count_prev=int(row[5] or 0),
                count_now=int(row[6] or 0),
                zero_streak=int(row[7] or 0),
                audit_state=str(row[8] or "none"),
                audit_hash=str(row[9] or ""),
                audit_adjustment=float(row[10] or 0.0),
                meta_only=bool(row[11]),
                explainer_only=bool(row[12]),
                first_seen_round=int(row[13]) if row[13] is not None else None,
                last_seen_round=int(row[14]) if row[14] is not None else None,
                evidence_json=_json_load(row[15], {}),
            )
        pool2: dict[str, Pool2Row] = {}
        for row in connection.execute(sql2).fetchall():
            pool2[str(row[0])] = Pool2Row(
                family_key=str(row[0]),
                round_id=int(row[1]) if row[1] is not None else None,
                heat_score=float(row[2]) if row[2] is not None else None,
                rank=int(row[3]) if row[3] is not None else None,
                reason=str(row[4] or ""),
                explainer_only=bool(row[5]),
            )
        return pool1, pool2

    if conn is not None:
        return read(conn)
    state = _state_module()
    with state.connection(url, autocommit=True) as connection:
        return read(connection)


def persist_round(
    result: PoolRoundResult,
    conn: Any = None,
    *,
    url: str | None = None,
) -> dict[str, int]:
    """把一轮结果写进 Postgres（pool1/pool2/family_round_stat/family_alias/pool_transition）。

    要求 ``result.round_id`` 非空且 round 存在（family_round_stat 外键）。
    重复执行同一轮是幂等的：pool1/pool2/stat 走 upsert，transition 按 round 重写。
    """
    if result.round_id is None:
        raise ValueError("persist_round 需要 round_id（family_round_stat 外键）")
    counts = {
        "pool1": 0,
        "pool2": 0,
        "deleted_pool2": 0,
        "family_round_stat": 0,
        "family_alias": 0,
        "pool_transition": 0,
    }

    def write(connection: Any) -> dict[str, int]:
        round_id = int(result.round_id)
        # ---- pool1 upsert
        sql1 = """
            INSERT INTO pool1 (
              family_key, canonical, aliases_json, status, score, count_prev,
              count_now, zero_streak, audit_state, audit_hash, audit_adjustment,
              meta_only, explainer_only, first_seen_round, last_seen_round,
              evidence_json
            ) VALUES (
              %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s::jsonb
            )
            ON CONFLICT (family_key) DO UPDATE SET
              canonical = EXCLUDED.canonical,
              aliases_json = EXCLUDED.aliases_json,
              status = EXCLUDED.status,
              score = EXCLUDED.score,
              count_prev = EXCLUDED.count_prev,
              count_now = EXCLUDED.count_now,
              zero_streak = EXCLUDED.zero_streak,
              audit_state = EXCLUDED.audit_state,
              audit_hash = EXCLUDED.audit_hash,
              audit_adjustment = EXCLUDED.audit_adjustment,
              meta_only = EXCLUDED.meta_only,
              explainer_only = EXCLUDED.explainer_only,
              first_seen_round = COALESCE(pool1.first_seen_round, EXCLUDED.first_seen_round),
              last_seen_round = EXCLUDED.last_seen_round,
              evidence_json = EXCLUDED.evidence_json,
              updated_at = now()
            RETURNING family_key
        """
        for row in result.pool1:
            connection.execute(
                sql1,
                (
                    row.family_key,
                    row.canonical,
                    json.dumps(list(row.aliases), ensure_ascii=False),
                    row.status,
                    float(row.score),
                    int(row.count_prev),
                    int(row.count_now),
                    int(row.zero_streak),
                    row.audit_state or "none",
                    row.audit_hash or None,
                    float(row.audit_adjustment or 0.0),
                    bool(row.meta_only),
                    bool(row.explainer_only),
                    row.first_seen_round,
                    row.last_seen_round,
                    json.dumps(row.evidence_json or {}, ensure_ascii=False, default=str),
                ),
            )
            counts["pool1"] += 1

        # ---- pool2：删除已降池/撤出的行，再 upsert 当前成员
        current_keys = [row.family_key for row in result.pool2]
        if current_keys:
            rows = connection.execute(
                "SELECT family_key FROM pool2 WHERE NOT (family_key = ANY(%s))",
                (current_keys,),
            ).fetchall()
        else:
            rows = connection.execute("SELECT family_key FROM pool2").fetchall()
        stale = [str(row[0]) for row in rows]
        if stale:
            connection.execute(
                "DELETE FROM pool2 WHERE family_key = ANY(%s)", (stale,)
            )
            counts["deleted_pool2"] = len(stale)
        sql2 = """
            INSERT INTO pool2 (family_key, round_id, heat_score, rank, reason, explainer_only)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (family_key) DO UPDATE SET
              round_id = COALESCE(EXCLUDED.round_id, pool2.round_id),
              heat_score = COALESCE(EXCLUDED.heat_score, pool2.heat_score),
              rank = COALESCE(EXCLUDED.rank, pool2.rank),
              reason = EXCLUDED.reason,
              explainer_only = EXCLUDED.explainer_only
        """
        for row in result.pool2:
            connection.execute(
                sql2,
                (
                    row.family_key,
                    round_id,
                    row.heat_score,
                    row.rank,
                    row.reason or None,
                    bool(row.explainer_only),
                ),
            )
            counts["pool2"] += 1

        # ---- family_round_stat upsert（每 family 本轮一行，含 count=0）
        sql3 = """
            INSERT INTO family_round_stat (
              round_id, family_key, comment_count, danmaku_count, video_count,
              meta_count, count_total, ratio, score_before, score_after, delta, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (round_id, family_key) DO UPDATE SET
              comment_count = EXCLUDED.comment_count,
              danmaku_count = EXCLUDED.danmaku_count,
              video_count = EXCLUDED.video_count,
              meta_count = EXCLUDED.meta_count,
              count_total = EXCLUDED.count_total,
              ratio = EXCLUDED.ratio,
              score_before = EXCLUDED.score_before,
              score_after = EXCLUDED.score_after,
              delta = EXCLUDED.delta,
              reason = EXCLUDED.reason
        """
        for stat in result.stats_rows:
            connection.execute(
                sql3,
                (
                    round_id,
                    stat.family_key,
                    int(stat.comment_count),
                    int(stat.danmaku_count),
                    int(stat.video_count),
                    int(stat.meta_count),
                    int(stat.count_total),
                    float(stat.ratio) if stat.ratio is not None else None,
                    float(stat.score_before),
                    float(stat.score_after),
                    float(stat.delta),
                    stat.reason,
                ),
            )
            counts["family_round_stat"] += 1

        # ---- family_alias upsert（norm_alias 唯一；不抢占已归属别名）
        sql4 = """
            INSERT INTO family_alias (family_key, alias, norm_alias, source, confidence)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (norm_alias) DO NOTHING
        """
        for alias in result.aliases:
            connection.execute(
                sql4,
                (
                    alias.family_key,
                    alias.alias,
                    alias.norm_alias,
                    alias.source,
                    float(alias.confidence),
                ),
            )
            counts["family_alias"] += 1

        # ---- pool_transition：同一轮重算时整轮替换，保证幂等
        connection.execute(
            "DELETE FROM pool_transition WHERE round_id = %s", (round_id,)
        )
        sql5 = """
            INSERT INTO pool_transition (
              family_key, round_id, from_pool, to_pool, reason, score
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """
        for transition in result.transitions:
            connection.execute(
                sql5,
                (
                    transition.family_key,
                    round_id,
                    transition.from_pool,
                    transition.to_pool,
                    transition.reason,
                    float(transition.score),
                ),
            )
            counts["pool_transition"] += 1
        return counts

    if conn is not None:
        return write(conn)
    state = _state_module()
    with state.transaction(url) as connection:
        return write(connection)


def _resolve_round_id(conn: Any, round_id: int | None) -> int:
    if round_id is not None:
        row = conn.execute("SELECT id FROM round WHERE id = %s", (int(round_id),)).fetchone()
        if row is None:
            raise ValueError(f"round id={round_id} 不存在")
        return int(row[0])
    row = conn.execute(
        "SELECT id FROM round ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("数据库没有 round，无法写入 family_round_stat；请先跑 Phase B/C")
    return int(row[0])


def run_pool_round(
    sample: WindowSample,
    *,
    knowledge: Knowledge | None = None,
    now: int | None = None,
    use_db: bool = True,
    database_url: str | None = None,
    db_conn: Any = None,
    round_id: int | None = None,
    degraded: bool | None = None,
    config: PoolConfig | None = None,
) -> PoolRoundResult:
    """Phase D 一轮编排：观察抽取 → 算分/迁移 →（可选）写 Postgres。

    - ``use_db=False``：纯内存，不读不写库；``round_id`` 可为 None。
    - ``use_db=True``：在单事务内读上一轮 pool1/pool2 → 计算 → 写回；
      ``db_conn`` 用于复用调用方事务（测试回滚）。
    """
    config = config or PoolConfig()
    knowledge = knowledge or Knowledge()
    now = int(now if now is not None else (sample.window_end or now_ts()))
    is_degraded = (
        bool(degraded) if degraded is not None else str(sample.status or "ok") != "ok"
    )

    def compute(
        prev_pool1: Mapping[str, Pool1Row],
        prev_pool2: Mapping[str, Pool2Row],
        video_extra: Mapping[str, Video] | None,
        resolved_round: int | None,
    ) -> PoolRoundResult:
        observation_videos = _merge_target_videos(sample, video_extra)
        observations = build_observations(
            sample,
            knowledge=knowledge,
            now=now,
            prev_pool1=prev_pool1,
            videos=observation_videos,
            config=config,
        )
        result = apply_round(
            observations,
            prev_pool1=prev_pool1,
            prev_pool2=prev_pool2,
            round_id=resolved_round,
            degraded=is_degraded,
            config=config,
            now=now,
        )
        result.window_start = int(sample.window_start or 0)
        result.window_end = int(sample.window_end or 0)
        result.stats.setdefault("round_id", resolved_round)
        result.stats.setdefault("window_start", result.window_start)
        result.stats.setdefault("window_end", result.window_end)
        result.stats.setdefault("sample_status", sample.status)
        return result

    if not use_db:
        result = compute({}, {}, None, int(sample.round_id) if sample.round_id else round_id)
        result.stats["db_written"] = False
        return result

    state = _state_module()
    with state.transaction(db_conn if db_conn is not None else database_url) as conn:
        resolved_round = _resolve_round_id(conn, round_id or sample.round_id)
        prev_pool1, prev_pool2 = load_pool_state(conn)
        video_extra = _round_videos(conn, resolved_round)
        result = compute(prev_pool1, prev_pool2, video_extra, resolved_round)
        counts = persist_round(result, conn)
        result.stats["db_written"] = True
        result.stats["db_counts"] = counts
        return result


# ---------------------------------------------------------------- CLI 辅助
def sample_from_json(path: str | Path) -> WindowSample:
    """从 Phase C ``--json/--output`` 落盘的 WindowSample JSON 还原样本。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("window JSON 顶层必须是对象")

    def targets(items: Any) -> list[WindowTarget]:
        result: list[WindowTarget] = []
        for item in items or []:
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            result.append(
                WindowTarget(
                    bvid=str(item.get("bvid")),
                    aid=int(item.get("aid") or 0),
                    cid=int(item["cid"]) if item.get("cid") is not None else None,
                    title=str(item.get("title") or ""),
                    zone=str(item.get("zone") or ""),
                    reply=int(item.get("reply") or 0),
                    danmaku=int(item.get("danmaku") or 0),
                    hot_score=float(item.get("hot_score") or 0.0),
                    source=str(item.get("source") or ""),
                )
            )
        return result

    def comments(items: Any, *, evidence: bool) -> list[WindowComment]:
        result: list[WindowComment] = []
        for item in items or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            result.append(
                WindowComment(
                    id=str(item.get("id")),
                    bvid=str(item.get("bvid") or ""),
                    text=str(item.get("text") or ""),
                    ctime=int(item.get("ctime") or 0),
                    like=int(item.get("like") or 0),
                    zone=str(item.get("zone") or ""),
                    video_title=str(item.get("video_title") or ""),
                    mode=int(item.get("mode") or 2),
                    evidence=bool(item.get("evidence", evidence)),
                )
            )
        return result

    def danmaku(items: Any, *, evidence: bool) -> list[WindowDanmaku]:
        result: list[WindowDanmaku] = []
        for item in items or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            result.append(
                WindowDanmaku(
                    id=str(item.get("id")),
                    bvid=str(item.get("bvid") or ""),
                    text=str(item.get("text") or ""),
                    ctime=int(item.get("ctime") or 0),
                    dmid=str(item.get("dmid") or ""),
                    zone=str(item.get("zone") or ""),
                    video_title=str(item.get("video_title") or ""),
                    progress=float(item.get("progress") or 0.0),
                    evidence=bool(item.get("evidence", evidence)),
                )
            )
        return result

    return WindowSample(
        generated_at=str(payload.get("generated_at") or ""),
        started_at=int(payload.get("started_at") or 0),
        window_start=int(payload.get("window_start") or 0),
        window_end=int(payload.get("window_end") or 0),
        status=str(payload.get("status") or "ok"),
        round_id=int(payload["round_id"]) if payload.get("round_id") is not None else None,
        comments=comments(payload.get("comments"), evidence=False),
        danmaku=danmaku(payload.get("danmaku"), evidence=False),
        evidence_comments=comments(payload.get("evidence_comments"), evidence=True),
        evidence_danmaku=danmaku(payload.get("evidence_danmaku"), evidence=True),
        comment_targets=targets(payload.get("comment_targets")),
        danmaku_targets=targets(payload.get("danmaku_targets")),
        stats=payload.get("stats") or {},
        errors=payload.get("errors") or [],
    )


__all__ = [
    "ACTIVE_STATUSES",
    "META_KIND_STRENGTH",
    "PoolConfig",
    "STATUS_NORMAL",
    "STATUS_OUT",
    "STATUS_PROVISIONAL",
    "alias_hit",
    "apply_round",
    "build_observations",
    "load_pool_state",
    "persist_round",
    "pool_state_from_result",
    "run_pool_round",
    "sample_from_json",
    "sat",
]
