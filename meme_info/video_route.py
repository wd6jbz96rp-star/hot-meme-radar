"""B 线（Phase B）：当前热门候选视频发现 → 在线/新增指标 → 45 评论 + 10 弹幕选片。

设计见 ``docs/pm/REARCHITECTURE.md`` §3.2 / §5.2 / §5.3 / §7 Phase B。

模块边界（Phase B 刻意不做的事）：

- 不接 worker、不切线上、不改 ``app/`` / 现有服务；
- 只写 ``round`` / ``video_metric``，不写 ``pool1`` / ``pool2`` / ``audit_event``；
- 不做评论/弹幕采样（Phase C），所以 ``comments_sampled`` / ``danmaku_sampled`` 恒为 0；
- 无 ``DATABASE_URL`` 时可用纯内存模式（``run_video_route(..., use_db=False)``）。

一轮流程：

1. **候选发现**：8 分区榜 + 全站热门 + 10 组搜索（常规 ~240）；
   分区 ``newlist`` + 按发布时间搜索 + 相关推荐（探索 ~60）；按 bvid 去重。
2. **指标测量**：缺 cid/统计的视频补一次 ``/x/web-interface/view``；
   每个候选查一次 ``/x/player/online/total``（2 rps，单点失败不中断整轮）。
3. **指标计算**：从 Postgres 读 7 天内上一轮 ``video_metric`` 基线；
   计算新增量 / 新增速率 / 播放量代理，robust z 后加权得到 ``hot_video``。
4. **选片**：45 评论目标（35 按热度 + 10 探索/分区配额，单分区 ≤40%）；
   10 弹幕目标（8 按新增弹幕/热度 + 2 探索）。
5. **写入**：``state.transaction()`` 内写 ``round`` + ``video_metric``（仅本轮单轮数据）。
"""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from .bili import BiliSource, ZONE_NAMES
from .models import Video, VideoCandidate, VideoMetric, VideoRouteResult, iso

# ---------------------------------------------------------------- 默认参数
#: 8 个分区（与现有采集一致：鬼畜/生活/动画/游戏/影视/音乐/娱乐/知识）
DEFAULT_ZONES: tuple[int, ...] = (119, 160, 1, 4, 181, 3, 5, 36)

#: 分区权重（相对值；常规候选/探索 newlist 配额按权重分配）
DEFAULT_ZONE_WEIGHTS: dict[int, float] = {
    119: 1.30,   # 鬼畜：梗密度最高
    160: 1.20,   # 生活
    36: 1.10,    # 知识（梗百科/解说常驻）
    4: 1.05,     # 游戏
    5: 1.00,     # 娱乐
    1: 0.95,     # 动画
    181: 0.90,   # 影视
    3: 0.85,     # 音乐
}

#: 10 组常规搜索（``{year}`` 会按本轮年份替换）
DEFAULT_SEARCH_QUERIES: tuple[str, ...] = (
    "热梗",
    "近期热梗",
    "{year}热梗",
    "梗百科",
    "梗指南",
    "什么梗",
    "爆火热梗",
    "网络热梗",
    "盘点热梗",
    "梗学堂",
)

#: 按发布时间排序的探索搜索（新视频，非热度排序）
DEFAULT_RECENT_SEARCH_QUERIES: tuple[str, ...] = ("热梗", "网络热梗", "梗百科")

#: 视频热度权重（§5.3 已确认）
HOT_WEIGHTS: dict[str, float] = {
    "online": 0.40,
    "new_comments": 0.30,
    "new_danmaku": 0.20,
    "play_proxy": 0.10,
}

#: 首见代理的年龄兜底：pubdate 缺失时 age_minutes=1380（即 view/1440 的等价形式）
DEFAULT_AGE_MINUTES = 1380.0

#: 二次见 dt < 5 分钟退回首见公式（§5.3）
MIN_DELTA_MINUTES = 5.0


# ---------------------------------------------------------------- 配置
@dataclass
class VideoRoutePlan:
    """一轮 B 线的全部可调参数（CLI/测试可覆盖）。"""

    zones: tuple[int, ...] = DEFAULT_ZONES
    zone_weights: dict[int, float] = field(
        default_factory=lambda: dict(DEFAULT_ZONE_WEIGHTS)
    )
    search_queries: tuple[str, ...] = DEFAULT_SEARCH_QUERIES
    recent_search_queries: tuple[str, ...] = DEFAULT_RECENT_SEARCH_QUERIES

    # 时间窗口（§5.1）：window_end=本轮开始，window_start=上一轮 window_end
    window_minutes: int = 15

    # 候选池：约 240 常规 + 60 探索 = 300
    candidate_target: int = 300
    routine_target: int = 240
    exploration_target: int = 60
    #: 仅用于 smoke/调试：限制候选池并提前停止发现（None = 完整一轮）
    max_candidates: int | None = None

    # 选片配额（§5.3）
    comment_target: int = 45
    comment_hot_slots: int = 35
    danmaku_target: int = 10
    danmaku_hot_slots: int = 8
    #: 单分区最多占评论目标的 40%
    zone_cap_ratio: float = 0.40

    # 常规来源份额（占 routine_target）
    zone_share: float = 0.55
    popular_share: float = 0.20
    search_share: float = 0.25
    #: 探索份额中 newlist 占比；其余给按发布时间搜索
    newlist_share: float = 0.67

    # 各来源取量
    popular_size: int = 50
    search_limit: int = 20
    newlist_size: int = 20
    related_limit: int = 40
    related_seeds: int = 5
    #: 完整一轮为「相关推荐」预留的候选名额（第二阶段补足到 candidate_target）
    related_reserve: int = 20

    enable_newlist: bool = True
    enable_recent_search: bool = True
    enable_related: bool = True

    # 指标
    baseline_days: int = 7
    #: 一轮最多补查多少条 view（search/newlist 候选没有统计）
    enrich_budget: int = 160

    @property
    def effective_candidate_target(self) -> int:
        if self.max_candidates is not None:
            return max(1, min(self.candidate_target, int(self.max_candidates)))
        return self.candidate_target

    @property
    def max_zone_comment(self) -> int:
        return max(1, int(math.floor(self.comment_target * self.zone_cap_ratio)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "zones": list(self.zones),
            "zone_weights": {str(k): v for k, v in self.zone_weights.items()},
            "search_queries": list(self.search_queries),
            "recent_search_queries": list(self.recent_search_queries),
            "window_minutes": self.window_minutes,
            "candidate_target": self.candidate_target,
            "routine_target": self.routine_target,
            "exploration_target": self.exploration_target,
            "max_candidates": self.max_candidates,
            "comment_target": self.comment_target,
            "comment_hot_slots": self.comment_hot_slots,
            "danmaku_target": self.danmaku_target,
            "danmaku_hot_slots": self.danmaku_hot_slots,
            "zone_cap_ratio": self.zone_cap_ratio,
            "max_zone_comment": self.max_zone_comment,
            "popular_size": self.popular_size,
            "search_limit": self.search_limit,
            "newlist_size": self.newlist_size,
            "related_limit": self.related_limit,
            "related_seeds": self.related_seeds,
            "related_reserve": self.related_reserve,
            "enable_newlist": self.enable_newlist,
            "enable_recent_search": self.enable_recent_search,
            "enable_related": self.enable_related,
            "baseline_days": self.baseline_days,
            "enrich_budget": self.enrich_budget,
            "hot_weights": dict(HOT_WEIGHTS),
        }


# ---------------------------------------------------------------- 纯计算工具
def age_minutes(pubdate: int, now: int, *, default: float = DEFAULT_AGE_MINUTES) -> float:
    """视频年龄（分钟），下限 1；pubdate 缺失时返回兜底值。"""
    if not pubdate:
        return default
    return max((float(now) - float(pubdate)) / 60.0, 1.0)


def log1p_rate(numerator: float, minutes: float) -> float | None:
    """log1p(每分钟速率)；输入非法时返回 None。"""
    try:
        value = float(numerator)
        denom = float(minutes)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0 or not math.isfinite(denom) or denom <= 0:
        return None
    return math.log1p(value / denom)


def median_mad(values: Sequence[float]) -> tuple[float, float]:
    """中位数与 MAD（未乘 1.4826）。空列表返回 (0, 0)。"""
    data = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not data:
        return 0.0, 0.0
    med = statistics.median(data)
    mad = statistics.median([abs(v - med) for v in data])
    return float(med), float(mad)


def robust_z(values: Sequence[float | None], *, clamp: float = 3.0) -> list[float]:
    """robust z-score：``(x - median) / (1.4826 * MAD)``。

    缺失值（None）按中位数处理 → z=0；MAD=0（常数序列）时全部记 0；
    结果截断到 ``[-clamp, clamp]`` 防单点极值主导热度。
    """
    known = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not known:
        return [0.0 for _ in values]
    med, mad = median_mad(known)
    if mad <= 1e-12:
        return [0.0 for _ in values]
    scale = 1.4826 * mad
    result: list[float] = []
    for value in values:
        if value is None or not math.isfinite(float(value)):
            result.append(0.0)
            continue
        z = (float(value) - med) / scale
        result.append(max(-clamp, min(clamp, z)))
    return result


def _safe_call(func: Callable[[], Any], errors: list[dict[str, Any]], source: str) -> Any:
    """调用外部来源：异常/None 记入 errors 并返回空值，不中断整轮。"""
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - 单来源失败不能拖垮整轮
        errors.append({"source": source, "error": str(exc)[:200]})
        return None


def _format_query(query: str, now: int) -> str:
    year = datetime.fromtimestamp(float(now), tz=timezone.utc).year
    return query.format(year=year)


def _weighted_quotas(keys: Sequence[Any], weights: Sequence[float], total: int) -> dict[Any, int]:
    """按权重把 total 拆成整数配额（最大余数法，保证总和 <= total）。"""
    keys = list(keys)
    if not keys or total <= 0:
        return {key: 0 for key in keys}
    weight_total = sum(max(0.0, float(w)) for w in weights)
    if weight_total <= 0:
        weights = [1.0 for _ in keys]
        weight_total = float(len(keys))
    raw = [total * max(0.0, float(w)) / weight_total for w in weights]
    quotas = {key: int(math.floor(value)) for key, value in zip(keys, raw)}
    remainder = total - sum(quotas.values())
    order = sorted(
        range(len(keys)),
        key=lambda index: (raw[index] - math.floor(raw[index])),
        reverse=True,
    )
    for index in order[: max(0, remainder)]:
        quotas[keys[index]] += 1
    return quotas


# ---------------------------------------------------------------- 候选发现
def _stats_known_for(video: Video) -> bool:
    """ranking/popular/related 的 stat 可信；search/newlist 的统计残缺需补查 view。"""
    source = video.source or ""
    if source.startswith("search:") or source.startswith("newlist:"):
        return False
    return (video.view or 0) > 0 or (video.reply or 0) > 0 or (video.danmaku or 0) > 0


def _zone_rid_from_source(source: str) -> int:
    for prefix in ("ranking:", "newlist:"):
        if source.startswith(prefix):
            try:
                return int(source.split(":", 1)[1])
            except (IndexError, ValueError):
                return 0
    return 0


def _candidate_from_video(
    video: Video,
    *,
    exploration: bool = False,
    exploration_source: str = "",
) -> VideoCandidate:
    return VideoCandidate(
        bvid=video.bvid,
        title=video.title,
        aid=video.aid,
        cid=video.cid,
        zone=video.zone or ZONE_NAMES.get(_zone_rid_from_source(video.source), ""),
        zone_rid=_zone_rid_from_source(video.source),
        pubdate=video.pubdate,
        view=video.view,
        reply=video.reply,
        danmaku=video.danmaku,
        owner=video.owner,
        source=video.source,
        sources=[video.source] if video.source else [],
        source_rank=video.source_rank,
        exploration=exploration,
        exploration_source=exploration_source,
        stats_known=_stats_known_for(video),
    )


def _merge_candidate(pool: dict[str, VideoCandidate], video: Video) -> VideoCandidate:
    """按 bvid 去重合并：保留首见来源，补全 cid/统计，记录全部 source。"""
    existing = pool.get(video.bvid)
    if existing is None:
        candidate = _candidate_from_video(video)
        pool[video.bvid] = candidate
        return candidate
    if video.source and video.source not in existing.sources:
        existing.sources.append(video.source)
    existing.cid = existing.cid or video.cid
    existing.aid = existing.aid or video.aid
    existing.pubdate = existing.pubdate or video.pubdate
    if not existing.title:
        existing.title = video.title
    if not existing.zone:
        existing.zone = video.zone
    if not existing.zone_rid:
        existing.zone_rid = _zone_rid_from_source(video.source)
    if _stats_known_for(video) and not existing.stats_known:
        existing.stats_known = True
        existing.view = max(existing.view, video.view)
        existing.reply = max(existing.reply, video.reply)
        existing.danmaku = max(existing.danmaku, video.danmaku)
    return existing


@dataclass
class _Discovery:
    """发现阶段的中间结果（不进对外 contract）。"""

    pool: dict[str, VideoCandidate]
    zone_buffers: dict[int, list[str]]
    popular_buffer: list[str]
    search_buffer: list[str]
    exploration_buffers: dict[str, list[str]]
    stats: dict[str, Any]


def _discover_regular(source: Any, plan: VideoRoutePlan, now: int,
                      errors: list[dict[str, Any]]) -> _Discovery:
    pool: dict[str, VideoCandidate] = {}
    zone_buffers: dict[int, list[str]] = {rid: [] for rid in plan.zones}
    popular_buffer: list[str] = []
    search_buffer: list[str] = []
    stats: dict[str, Any] = {"raw_sources": {}, "search_failed": []}
    limit = plan.max_candidates

    def add(videos: Iterable[Video], buffer: list[str] | None) -> None:
        for video in videos:
            if not video.bvid:
                continue
            is_new = video.bvid not in pool
            _merge_candidate(pool, video)
            if is_new and buffer is not None:
                buffer.append(video.bvid)

    def enough() -> bool:
        return limit is not None and len(pool) >= limit

    # 1) 分区榜：按权重从高到低（权重高的分区先占 zone 归属）
    zone_order = sorted(plan.zones, key=lambda rid: plan.zone_weights.get(rid, 1.0), reverse=True)
    for rid in zone_order:
        if enough():
            break
        videos = _safe_call(lambda rid=rid: source.ranking(rid), errors, f"ranking:{rid}")
        videos = list(videos or [])
        stats["raw_sources"][f"ranking:{rid}"] = len(videos)
        add(videos, zone_buffers.get(rid))
        if not videos:
            stats.setdefault("ranking_failed", []).append(rid)

    # 2) 全站热门
    if not enough():
        videos = _safe_call(lambda: source.popular(plan.popular_size), errors, "popular")
        videos = list(videos or [])
        stats["raw_sources"]["popular"] = len(videos)
        add(videos, popular_buffer)

    # 3) 搜索（10 组关键词）
    if not enough():
        for query in plan.search_queries:
            if enough():
                break
            keyword = _format_query(query, now)
            videos = _safe_call(
                lambda keyword=keyword: source.search_videos(keyword, plan.search_limit),
                errors,
                f"search:{keyword}",
            )
            videos = list(videos or [])
            stats["raw_sources"][f"search:{keyword}"] = len(videos)
            if not videos:
                stats["search_failed"].append(keyword)
            add(videos, search_buffer)

    return _Discovery(
        pool=pool,
        zone_buffers=zone_buffers,
        popular_buffer=popular_buffer,
        search_buffer=search_buffer,
        exploration_buffers={},
        stats=stats,
    )


def _take(buffer: Sequence[str], quota: int, pool: dict[str, VideoCandidate],
          chosen: list[VideoCandidate], seen: set[str]) -> int:
    taken = 0
    for bvid in buffer:
        if taken >= quota:
            break
        if bvid in seen:
            continue
        candidate = pool.get(bvid)
        if candidate is None:
            continue
        seen.add(bvid)
        chosen.append(candidate)
        taken += 1
    return taken


def _select_routine(discovery: _Discovery, plan: VideoRoutePlan) -> list[VideoCandidate]:
    """常规候选：分区榜（按权重配额）+ 全站热门 + 搜索，共 routine_target 个。"""
    routine_target = plan.routine_target
    if plan.max_candidates is not None:
        routine_target = max(1, int(round(plan.effective_candidate_target * plan.routine_target
                                          / plan.candidate_target)))
    chosen: list[VideoCandidate] = []
    seen: set[str] = set()

    zone_keys = [rid for rid in plan.zones if discovery.zone_buffers.get(rid)]
    zone_quota_total = int(round(routine_target * plan.zone_share))
    zone_quotas = _weighted_quotas(
        zone_keys, [plan.zone_weights.get(rid, 1.0) for rid in zone_keys], zone_quota_total
    )
    for rid in zone_keys:
        _take(discovery.zone_buffers[rid], zone_quotas.get(rid, 0), discovery.pool, chosen, seen)

    popular_quota = int(round(routine_target * plan.popular_share))
    search_quota = int(round(routine_target * plan.search_share))
    _take(discovery.popular_buffer, popular_quota, discovery.pool, chosen, seen)
    _take(discovery.search_buffer, search_quota, discovery.pool, chosen, seen)

    # 未满时按 权重分区 → 热门 → 搜索 → 其余 补齐
    if len(chosen) < routine_target:
        for rid in zone_keys:
            if len(chosen) >= routine_target:
                break
            _take(discovery.zone_buffers[rid], routine_target - len(chosen), discovery.pool, chosen, seen)
        for buffer in (discovery.popular_buffer, discovery.search_buffer, list(discovery.pool)):
            if len(chosen) >= routine_target:
                break
            _take(buffer, routine_target - len(chosen), discovery.pool, chosen, seen)
    return chosen[:routine_target]


def _discover_exploration(source: Any, plan: VideoRoutePlan, now: int,
                          discovery: _Discovery, errors: list[dict[str, Any]]) -> None:
    """探索来源：分区 newlist + 按发布时间搜索（相关推荐在测量后二阶段补充）。"""
    pools = discovery.exploration_buffers
    stats = discovery.stats

    def add(videos: Iterable[Video], kind: str) -> None:
        buffer = pools.setdefault(kind, [])
        for video in videos:
            if not video.bvid:
                continue
            is_new = video.bvid not in discovery.pool
            _merge_candidate(discovery.pool, video)
            if is_new:
                discovery.pool[video.bvid].exploration = True
                discovery.pool[video.bvid].exploration_source = kind
                buffer.append(video.bvid)

    zone_order = sorted(plan.zones, key=lambda rid: plan.zone_weights.get(rid, 1.0), reverse=True)
    if plan.enable_newlist:
        for rid in zone_order:
            videos = _safe_call(
                lambda rid=rid: source.newlist(rid, plan.newlist_size),
                errors,
                f"newlist:{rid}",
            )
            videos = list(videos or [])
            stats.setdefault("raw_sources", {})[f"newlist:{rid}"] = len(videos)
            add(videos, f"newlist:{rid}")

    if plan.enable_recent_search:
        for query in plan.recent_search_queries:
            keyword = _format_query(query, now)
            videos = _safe_call(
                lambda keyword=keyword: source.search_recent(keyword, plan.search_limit),
                errors,
                f"recent-search:{keyword}",
            )
            videos = list(videos or [])
            stats.setdefault("raw_sources", {})[f"recent-search:{keyword}"] = len(videos)
            add(videos, f"recent-search:{keyword}")


def _select_exploration(discovery: _Discovery, plan: VideoRoutePlan,
                        routine: list[VideoCandidate],
                        target: int | None = None) -> list[VideoCandidate]:
    """从探索缓冲中按配额挑 exploration_target 个（newlist 按分区权重 + 发布时间搜索）。"""
    exploration_target = plan.exploration_target if target is None else max(0, int(target))
    if plan.max_candidates is not None and target is None:
        exploration_target = max(0, plan.effective_candidate_target - len(routine))
    chosen: list[VideoCandidate] = []
    seen = {candidate.bvid for candidate in routine}

    newlist_kinds = [k for k in discovery.exploration_buffers if k.startswith("newlist:")]
    recent_kinds = [k for k in discovery.exploration_buffers if k.startswith("recent-search:")]
    newlist_quota = int(round(exploration_target * plan.newlist_share))
    recent_quota = exploration_target - newlist_quota

    newlist_quotas = _weighted_quotas(
        newlist_kinds,
        [plan.zone_weights.get(int(k.split(":")[1]), 1.0) for k in newlist_kinds],
        newlist_quota,
    )
    for kind in newlist_kinds:
        _take(discovery.exploration_buffers[kind], newlist_quotas.get(kind, 0),
              discovery.pool, chosen, seen)
    for kind in recent_kinds:
        _take(discovery.exploration_buffers[kind], plan.search_limit,
              discovery.pool, chosen, seen)
    # 探索额度没用完：从剩余探索候选补齐
    if len(chosen) < exploration_target:
        leftover = [
            bvid
            for kind in list(newlist_kinds) + list(recent_kinds)
            for bvid in discovery.exploration_buffers.get(kind, [])
        ]
        _take(leftover, exploration_target - len(chosen), discovery.pool, chosen, seen)
    return chosen[:exploration_target]


def discover_candidates(
    source: Any,
    plan: VideoRoutePlan | None = None,
    *,
    now: int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> tuple[list[VideoCandidate], dict[str, Any]]:
    """多来源发现候选池（按 bvid 去重；常规 ~240 + 探索 ~60）。

    ``source`` 只需提供 ``ranking/popular/search_videos/newlist/search_recent``
    这些方法（``BiliSource`` 或测试 fake 均可）。
    """
    plan = plan or VideoRoutePlan()
    now = int(now if now is not None else time.time())
    errors = errors if errors is not None else []
    started = time.monotonic()

    discovery = _discover_regular(source, plan, now, errors)
    stats = discovery.stats
    stats["raw_pool"] = len(discovery.pool)

    if plan.max_candidates is not None:
        # smoke/调试模式：不做配额裁剪，直接取前 N 个候选
        candidates = list(discovery.pool.values())[: plan.effective_candidate_target]
        stats.update(
            {
                "candidates": len(candidates),
                "routine": len(candidates),
                "exploration": sum(1 for c in candidates if c.exploration),
                "mode": "limited",
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "config": plan.as_dict(),
            }
        )
        return candidates, stats

    reserve = plan.related_reserve if (plan.enable_related and plan.related_seeds > 0) else 0
    fill_target = max(1, plan.candidate_target - max(0, reserve))
    routine = _select_routine(discovery, plan)
    _discover_exploration(source, plan, now, discovery, errors)
    exploration = _select_exploration(
        discovery, plan, routine, target=fill_target - len(routine)
    )

    candidates = routine + exploration
    # 未满发现目标：用剩余原始候选补齐（保持原有 exploration 标记）；相关推荐留给第二阶段
    if len(candidates) < fill_target:
        seen = {candidate.bvid for candidate in candidates}
        for bvid, candidate in discovery.pool.items():
            if len(candidates) >= fill_target:
                break
            if bvid in seen:
                continue
            candidates.append(candidate)
    candidates = candidates[:fill_target]

    stats.update(
        {
            "candidates": len(candidates),
            "routine": sum(1 for c in candidates if not c.exploration),
            "exploration": sum(1 for c in candidates if c.exploration),
            "exploration_sources": _count_by(
                candidate.exploration_source for candidate in candidates if candidate.exploration
            ),
            "source_counts": _count_by(
                _source_group(candidate) for candidate in candidates
            ),
            "zone_counts": _count_by(candidate.zone or "unknown" for candidate in candidates),
            "mode": "full",
            "discovery_target": fill_target,
            "related_reserve": reserve,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "config": plan.as_dict(),
        }
    )
    return candidates, stats


def _source_group(candidate: VideoCandidate) -> str:
    source = candidate.source or ""
    if source.startswith("ranking:"):
        return "ranking"
    if source.startswith("newlist:"):
        return "newlist"
    if source.startswith("related:"):
        return "related"
    if source.startswith("search:") or source.startswith("recent-search:"):
        return "search"
    return source.split(":", 1)[0] or "unknown"


def _count_by(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


# ---------------------------------------------------------------- 指标测量
def measure_candidates(
    source: Any,
    candidates: list[VideoCandidate],
    plan: VideoRoutePlan | None = None,
    *,
    now: int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """补全 cid/统计（受限预算）并逐候选查在线人数；单点失败只记错不中断。"""
    plan = plan or VideoRoutePlan()
    now = int(now if now is not None else time.time())
    errors = errors if errors is not None else []
    stats = {
        "candidates": len(candidates),
        "enrich_attempts": 0,
        "enriched": 0,
        "enrich_failed": 0,
        "online_ok": 0,
        "online_failed": 0,
        "online_coverage": 0.0,
        "no_cid": 0,
    }

    for candidate in candidates:
        problems: list[str] = []
        need_view = (not candidate.stats_known) or (not candidate.cid)
        if need_view and stats["enrich_attempts"] < plan.enrich_budget:
            stats["enrich_attempts"] += 1
            video = _safe_call(lambda bvid=candidate.bvid: source.view(bvid), errors,
                               f"view:{candidate.bvid}")
            if video is not None:
                candidate.cid = candidate.cid or video.cid
                candidate.aid = candidate.aid or video.aid
                candidate.title = candidate.title or video.title
                candidate.pubdate = candidate.pubdate or video.pubdate
                candidate.zone = candidate.zone or video.zone
                candidate.view = max(candidate.view, video.view)
                candidate.reply = max(candidate.reply, video.reply)
                candidate.danmaku = max(candidate.danmaku, video.danmaku)
                candidate.stats_known = True
                stats["enriched"] += 1
            else:
                stats["enrich_failed"] += 1
                problems.append("view")
        elif need_view:
            problems.append("enrich_budget")

        if not candidate.cid:
            stats["no_cid"] += 1
            stats["online_failed"] += 1
            problems.append("cid")
            candidate.error = ";".join(problems)
            continue

        online = _safe_call(
            lambda c=candidate: source.online_total(c.bvid, c.cid),
            errors,
            f"online:{candidate.bvid}",
        )
        if online is None:
            stats["online_failed"] += 1
            problems.append("online")
        else:
            candidate.online = int(online)
            stats["online_ok"] += 1
        candidate.error = ";".join(problems)

    effective = stats["online_ok"] + stats["online_failed"]
    stats["online_coverage"] = round(stats["online_ok"] / effective, 3) if effective else 0.0
    return stats


def expand_related(
    source: Any,
    candidates: list[VideoCandidate],
    plan: VideoRoutePlan | None = None,
    *,
    now: int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> list[VideoCandidate]:
    """二阶段探索：用在线人数最高的若干候选拉相关推荐，补足候选池到 target。"""
    plan = plan or VideoRoutePlan()
    errors = errors if errors is not None else []
    if not plan.enable_related or plan.max_candidates is not None:
        return []
    remaining = plan.effective_candidate_target - len(candidates)
    if remaining <= 0 or plan.related_seeds <= 0:
        return []
    seeds = sorted(
        [c for c in candidates if c.bvid],
        key=lambda c: (c.online is not None, c.online or -1, c.view),
        reverse=True,
    )[: plan.related_seeds]
    added: list[VideoCandidate] = []
    seen = {candidate.bvid for candidate in candidates}
    for seed in seeds:
        if len(added) >= remaining:
            break
        videos = _safe_call(
            lambda bvid=seed.bvid: source.related(bvid, plan.related_limit),
            errors,
            f"related:{seed.bvid}",
        )
        for video in list(videos or []):
            if not video.bvid or video.bvid in seen:
                continue
            seen.add(video.bvid)
            candidate = _candidate_from_video(
                video, exploration=True, exploration_source=f"related:{seed.bvid}"
            )
            candidates.append(candidate)
            added.append(candidate)
            if len(added) >= remaining:
                break
    return added


# ---------------------------------------------------------------- 指标计算
@dataclass
class PreviousMetric:
    """上一轮 ``video_metric`` 基线（7 天内）。"""

    bvid: str
    view: int = 0
    reply: int = 0
    danmaku: int = 0
    measured_at: int = 0


def compute_metrics(
    candidates: Sequence[VideoCandidate],
    *,
    previous: dict[str, PreviousMetric] | None = None,
    now: int | None = None,
    plan: VideoRoutePlan | None = None,
) -> list[VideoMetric]:
    """计算本轮指标 + robust z + ``hot_video``（§5.3）。

    首见（无 7 天基线）没有增量：评论/弹幕用**年龄归一化代理**
    ``reply/(age_minutes+60)``、``danmaku/(age_minutes+60)``（log1p 后 robust z），
    播放量用 ``view/(age_minutes+60)``；二次见用基线差值算速率。
    """
    plan = plan or VideoRoutePlan()
    previous = previous or {}
    now = int(now if now is not None else time.time())
    rows: list[VideoMetric] = []

    for candidate in candidates:
        prev = previous.get(candidate.bvid)
        age = age_minutes(candidate.pubdate, now)
        metric = VideoMetric(
            bvid=candidate.bvid,
            cid=candidate.cid,
            title=candidate.title,
            zone=candidate.zone,
            pubdate=candidate.pubdate,
            source=candidate.source,
            view=candidate.view,
            reply=candidate.reply,
            danmaku=candidate.danmaku,
            online=candidate.online,
            error=candidate.error,
            exploration=candidate.exploration,
            exploration_source=candidate.exploration_source,
            age_minutes=round(age, 2),
            stats_known=candidate.stats_known,
        )

        view = max(0, int(candidate.view or 0))
        reply = max(0, int(candidate.reply or 0))
        danmaku = max(0, int(candidate.danmaku or 0))
        stats_known = candidate.stats_known

        if prev is None:
            metric.first_seen = True
            metric.play_rate = view / (age + 60.0) if view > 0 else None
            metric.comment_rate = reply / (age + 60.0) if stats_known else None
            metric.danmaku_rate = danmaku / (age + 60.0) if stats_known else None
        else:
            metric.first_seen = False
            dt_minutes = max(0.0, (now - prev.measured_at) / 60.0)
            metric.dt_minutes = round(dt_minutes, 2)
            metric.new_view = max(0, view - prev.view) if view > 0 else None
            metric.new_reply = max(0, reply - prev.reply) if stats_known else None
            metric.new_danmaku = max(0, danmaku - prev.danmaku) if stats_known else None
            if dt_minutes >= MIN_DELTA_MINUTES:
                metric.play_rate = (
                    metric.new_view / dt_minutes if metric.new_view is not None else None
                )
                metric.comment_rate = (
                    metric.new_reply / dt_minutes if metric.new_reply is not None else None
                )
                metric.danmaku_rate = (
                    metric.new_danmaku / dt_minutes if metric.new_danmaku is not None else None
                )
            else:
                # dt < 5min：退回首见年龄归一化公式（§5.3）
                metric.play_rate = view / (age + 60.0) if view > 0 else None
                metric.comment_rate = reply / (age + 60.0) if stats_known else None
                metric.danmaku_rate = danmaku / (age + 60.0) if stats_known else None

        metric.play_proxy = (
            math.log1p(metric.play_rate) if metric.play_rate is not None else None
        )
        rows.append(metric)

    # robust z（缺失/常数序列 → 0）
    online_z = robust_z([metric.online for metric in rows])
    comment_z = robust_z([
        math.log1p(metric.comment_rate) if metric.comment_rate is not None else None
        for metric in rows
    ])
    danmaku_z = robust_z([
        math.log1p(metric.danmaku_rate) if metric.danmaku_rate is not None else None
        for metric in rows
    ])
    play_z = robust_z([metric.play_proxy for metric in rows])

    for index, metric in enumerate(rows):
        metric.online_z = round(online_z[index], 4)
        metric.comment_z = round(comment_z[index], 4)
        metric.danmaku_z = round(danmaku_z[index], 4)
        metric.play_z = round(play_z[index], 4)
        metric.hot_score = round(
            HOT_WEIGHTS["online"] * metric.online_z
            + HOT_WEIGHTS["new_comments"] * metric.comment_z
            + HOT_WEIGHTS["new_danmaku"] * metric.danmaku_z
            + HOT_WEIGHTS["play_proxy"] * metric.play_z,
            4,
        )
    return rows


# ---------------------------------------------------------------- 选片
def select_targets(
    metrics: Sequence[VideoMetric],
    plan: VideoRoutePlan | None = None,
    *,
    stats: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """45 评论目标 + 10 弹幕目标（§5.3）；返回值同时写入 metric 的 selected 标记。"""
    plan = plan or VideoRoutePlan()
    stats = stats if stats is not None else {}
    for metric in metrics:
        metric.selected_comment = False
        metric.selected_danmaku = False

    comment = _select_comment(metrics, plan, stats)
    danmaku = _select_danmaku(metrics, plan, stats)
    comment_set, danmaku_set = set(comment), set(danmaku)
    for metric in metrics:
        metric.selected_comment = metric.bvid in comment_set
        metric.selected_danmaku = metric.bvid in danmaku_set
    stats["comment_selected"] = len(comment)
    stats["danmaku_selected"] = len(danmaku)
    stats["comment_overlap"] = len(comment_set & danmaku_set)
    stats["comment_zone_counts"] = _count_by(
        metric.zone or "unknown" for metric in metrics if metric.selected_comment
    )
    return comment, danmaku


def _select_comment(
    metrics: Sequence[VideoMetric], plan: VideoRoutePlan, stats: dict[str, Any]
) -> list[str]:
    """35 按 hot_video 排名 + 10 探索/分区配额；单分区最多占评论目标的 40%。"""
    target = min(plan.comment_target, len(metrics))
    hot_slots = min(plan.comment_hot_slots, target)
    explore_slots = max(0, target - hot_slots)
    max_zone = plan.max_zone_comment
    ordered = sorted(metrics, key=lambda m: (-(m.hot_score or 0.0), m.bvid))
    chosen: list[VideoMetric] = []
    zone_counts: dict[str, int] = {}

    def can_add(metric: VideoMetric) -> bool:
        zone = metric.zone or "unknown"
        return zone_counts.get(zone, 0) < max_zone

    def add(metric: VideoMetric) -> None:
        zone = metric.zone or "unknown"
        chosen.append(metric)
        zone_counts[zone] = zone_counts.get(zone, 0) + 1

    def chosen_ids() -> set[str]:
        return {metric.bvid for metric in chosen}

    # 1) 按 hot_video 排名取 35（遵守单分区上限）
    for metric in ordered:
        if len(chosen) >= hot_slots:
            break
        if can_add(metric):
            add(metric)

    # 2) 探索/分区配额 10：优先探索候选，再按「当前入选最少的分区」做分区多样性
    if len(chosen) < target:
        selected = chosen_ids()
        remaining = [m for m in ordered if m.bvid not in selected]
        for metric in sorted(remaining, key=lambda m: (not m.exploration, -(m.hot_score or 0.0))):
            if len(chosen) >= hot_slots + explore_slots:
                break
            if can_add(metric):
                add(metric)

    # 3) 仍未满：按分区多样性补齐（分区配额），最后按热度
    if len(chosen) < target:
        selected = chosen_ids()
        remaining = [m for m in ordered if m.bvid not in selected]
        remaining.sort(key=lambda m: (zone_counts.get(m.zone or "unknown", 0),
                                      -(m.hot_score or 0.0), m.bvid))
        for metric in remaining:
            if len(chosen) >= target:
                break
            if can_add(metric):
                add(metric)
    zone_limited = len(chosen) < target
    stats["comment_zone_cap_limited"] = zone_limited
    stats["comment_max_zone"] = max_zone
    return [metric.bvid for metric in chosen]


def _select_danmaku(
    metrics: Sequence[VideoMetric], plan: VideoRoutePlan, stats: dict[str, Any]
) -> list[str]:
    """8 按新增弹幕/热度 + 2 探索。"""
    target = min(plan.danmaku_target, len(metrics))
    hot_slots = min(plan.danmaku_hot_slots, target)
    ordered = sorted(metrics, key=lambda m: (-m.danmaku_rank_score, m.bvid))
    chosen: list[VideoMetric] = []
    selected_ids: set[str] = set()

    for metric in ordered:
        if len(chosen) >= hot_slots:
            break
        chosen.append(metric)
        selected_ids.add(metric.bvid)

    if len(chosen) < target:
        remaining = [m for m in ordered if m.bvid not in selected_ids]
        for metric in sorted(remaining, key=lambda m: (not m.exploration, -m.danmaku_rank_score)):
            if len(chosen) >= target:
                break
            chosen.append(metric)
            selected_ids.add(metric.bvid)
    if len(chosen) < target:
        for metric in ordered:
            if len(chosen) >= target:
                break
            if metric.bvid in selected_ids:
                continue
            chosen.append(metric)
            selected_ids.add(metric.bvid)
    stats["danmaku_exploration_selected"] = sum(1 for m in chosen if m.exploration)
    return [metric.bvid for metric in chosen]


# ---------------------------------------------------------------- Postgres
def _state_module():
    """延迟 import：``--no-db`` 纯内存模式不要求 psycopg 可用。"""
    from . import state as state_module

    return state_module


def load_last_window(
    *,
    now: int | None = None,
    plan: VideoRoutePlan | None = None,
    url: str | None = None,
    conn: Any = None,
) -> tuple[int, int]:
    """窗口（§5.1）：``window_start = 上一轮 window_end``，``window_end = 本轮开始``。

    没有历史轮次时退化为 ``now - window_minutes``；只读，短连接。
    """
    plan = plan or VideoRoutePlan()
    now = int(now if now is not None else time.time())
    sql = (
        "SELECT EXTRACT(EPOCH FROM window_end)::bigint FROM round "
        "WHERE status IN ('ok', 'degraded') ORDER BY started_at DESC LIMIT 1"
    )

    def read(connection: Any) -> int | None:
        row = connection.execute(sql).fetchone()
        return int(row[0]) if row and row[0] else None

    previous_end: int | None = None
    if conn is not None:
        previous_end = read(conn)
    else:
        state = _state_module()
        with state.connection(url, autocommit=True) as connection:
            previous_end = read(connection)
    if previous_end:
        return previous_end, now
    return now - plan.window_minutes * 60, now


def load_previous_metrics(
    bvids: Sequence[str],
    *,
    now: int | None = None,
    lookback_days: int | None = None,
    url: str | None = None,
    conn: Any = None,
    exclude_round_id: int | None = None,
) -> dict[str, PreviousMetric]:
    """读每个 bvid 最近一次 ``video_metric``（默认 7 天内）作为基线。"""
    bvid_list = [bvid for bvid in dict.fromkeys(bvids) if bvid]
    if not bvid_list:
        return {}
    now = int(now if now is not None else time.time())
    lookback = int(lookback_days if lookback_days is not None else 7)
    sql = """
        SELECT DISTINCT ON (vm.bvid)
               vm.bvid,
               COALESCE(vm.view, 0),
               COALESCE(vm.reply, 0),
               COALESCE(vm.danmaku, 0),
               EXTRACT(EPOCH FROM r.started_at)::bigint
          FROM video_metric vm
          JOIN round r ON r.id = vm.round_id
         WHERE vm.bvid = ANY(%s::text[])
           AND r.status IN ('ok', 'degraded')
           AND r.started_at >= to_timestamp(%s::bigint) - make_interval(days => %s::int)
           AND (%s::bigint IS NULL OR vm.round_id <> %s::bigint)
         ORDER BY vm.bvid, r.started_at DESC
    """
    params = (bvid_list, now, lookback, exclude_round_id, exclude_round_id)

    def read(connection: Any) -> dict[str, PreviousMetric]:
        rows = connection.execute(sql, params).fetchall()
        return {
            str(row[0]): PreviousMetric(
                bvid=str(row[0]),
                view=int(row[1] or 0),
                reply=int(row[2] or 0),
                danmaku=int(row[3] or 0),
                measured_at=int(row[4] or 0),
            )
            for row in rows
        }

    if conn is not None:
        return read(conn)
    state = _state_module()
    with state.connection(url, autocommit=True) as connection:
        return read(connection)


def persist_result(
    result: VideoRouteResult,
    *,
    conn: Any = None,
    url: str | None = None,
) -> int:
    """在一个事务内写 ``round`` + 本轮全部 ``video_metric``；返回 round id。

    只写本轮数据，不动 pool1/pool2/audit_event。传入 ``conn`` 时复用（测试可在
    外层事务里回滚），否则用 ``state.transaction(url)`` 新建事务。
    """
    stats = dict(result.stats or {})
    round_sql = """
        INSERT INTO round (
            started_at, finished_at, window_start, window_end, status,
            videos_sampled, comments_sampled, danmaku_sampled, degraded_reason, stats
        ) VALUES (
            to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), %s,
            %s, %s, %s, %s, %s::jsonb
        ) RETURNING id
    """
    metric_sql = """
        INSERT INTO video_metric (
            round_id, bvid, cid, title, zone, pubdate, source, view, reply, danmaku,
            online, new_view, new_reply, new_danmaku, play_proxy, hot_score,
            selected_comment, selected_danmaku, error
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """
    degraded_reason = stats.get("degraded_reason") or None
    round_params = (
        result.started_at or int(time.time()),
        result.started_at or int(time.time()),
        result.window_start or (result.started_at - 900),
        result.window_end or result.started_at,
        result.status or "ok",
        len(result.metrics),
        0,  # Phase B 不采样评论（Phase C）
        0,  # Phase B 不采样弹幕（Phase C）
        degraded_reason,
        json.dumps(stats, ensure_ascii=False, default=str),
    )
    rows = [
        (
            metric.bvid,
            metric.cid,
            metric.title,
            metric.zone,
            metric.pubdate,
            metric.source,
            metric.view,
            metric.reply,
            metric.danmaku,
            metric.online,
            metric.new_view,
            metric.new_reply,
            metric.new_danmaku,
            metric.play_proxy,
            metric.hot_score,
            bool(metric.selected_comment),
            bool(metric.selected_danmaku),
            metric.error or None,
        )
        for metric in result.metrics
    ]

    def write(connection: Any) -> int:
        row = connection.execute(round_sql, round_params).fetchone()
        round_id = int(row[0])
        if rows:
            payload = [(round_id, *values) for values in rows]
            with connection.cursor() as cursor:
                cursor.executemany(metric_sql, payload)
        return round_id

    if conn is not None:
        round_id = write(conn)
    else:
        state = _state_module()
        with state.transaction(url) as connection:
            round_id = write(connection)
    result.round_id = round_id
    return round_id


def _determine_status(candidates: Sequence[VideoCandidate],
                      metrics: Sequence[VideoMetric],
                      plan: VideoRoutePlan) -> tuple[str, str]:
    """阶段状态：正常 ok；候选/在线覆盖明显不足时 degraded（不阻断写入）。"""
    if not metrics:
        return "failed", "no candidates"
    target = plan.effective_candidate_target
    reasons: list[str] = []
    if len(candidates) < max(5, int(target * 0.5)):
        reasons.append(f"candidates={len(candidates)}<target*0.5")
    online_ok = sum(1 for candidate in candidates if candidate.online is not None)
    if candidates and online_ok / len(candidates) < 0.5:
        reasons.append(f"online_coverage={online_ok}/{len(candidates)}")
    if reasons:
        return "degraded", ";".join(reasons)
    return "ok", ""


# ---------------------------------------------------------------- 一轮编排
def run_video_route(
    source: Any | None = None,
    plan: VideoRoutePlan | None = None,
    *,
    now: int | None = None,
    use_db: bool = True,
    database_url: str | None = None,
    previous: dict[str, PreviousMetric] | None = None,
    bili_source: BiliSource | None = None,
    db_conn: Any = None,
) -> VideoRouteResult:
    """跑一轮 B 线：发现 → 测在线 → 算指标 → 选片 →（可选）写库。

    - ``use_db=False``：纯内存模式，不读也不写 Postgres（``--no-db``）；
    - ``previous``：显式注入基线（测试/回放），缺省从 Postgres 读 7 天内数据；
    - ``source`` 缺省用 ``BiliSource``（真机）；测试注入 fake。
    """
    plan = plan or VideoRoutePlan()
    started = int(now if now is not None else time.time())
    wall_started = time.monotonic()
    if source is None:
        source = bili_source or BiliSource()
    errors: list[dict[str, Any]] = []

    result = VideoRouteResult(
        generated_at=iso(started),
        started_at=started,
        window_end=started,
    )
    url: str | None = None
    if use_db:
        url = _state_module().database_url(database_url)
        result.window_start, result.window_end = load_last_window(
            now=started, plan=plan, url=url, conn=db_conn
        )
    else:
        result.window_start = started - plan.window_minutes * 60

    # 1) 候选发现
    candidates, discovery_stats = discover_candidates(source, plan, now=started, errors=errors)
    # 2) 测量：补统计 + 在线人数
    measure_stats = measure_candidates(source, candidates, plan, now=started, errors=errors)
    # 3) 二阶段探索：高在线候选的相关推荐
    related_added = expand_related(source, candidates, plan, now=started, errors=errors)
    related_measure: dict[str, Any] = {}
    if related_added:
        related_measure = measure_candidates(source, related_added, plan, now=started, errors=errors)

    # 4) 基线（上一轮 video_metric，7 天内）
    baseline: dict[str, PreviousMetric] = dict(previous or {})
    if previous is None and use_db:
        baseline = load_previous_metrics(
            [candidate.bvid for candidate in candidates],
            now=started,
            lookback_days=plan.baseline_days,
            url=url,
            conn=db_conn,
            exclude_round_id=result.round_id,
        )
    # 5) 指标
    metrics = compute_metrics(candidates, previous=baseline, now=started, plan=plan)
    # 6) 选片
    selection_stats: dict[str, Any] = {}
    selected_comment, selected_danmaku = select_targets(metrics, plan, stats=selection_stats)

    result.candidates = candidates
    result.metrics = metrics
    result.selected_comment = selected_comment
    result.selected_danmaku = selected_danmaku
    result.errors = list(errors)

    status, degraded_reason = _determine_status(candidates, metrics, plan)
    result.status = status
    first_seen = sum(1 for metric in metrics if metric.first_seen)
    discovery_stats.update(
        {
            "candidates": len(candidates),
            "routine": sum(1 for candidate in candidates if not candidate.exploration),
            "exploration": sum(1 for candidate in candidates if candidate.exploration),
            "exploration_sources": _count_by(
                candidate.exploration_source for candidate in candidates if candidate.exploration
            ),
        }
    )
    result.stats = {
        **discovery_stats,
        "measure": measure_stats,
        "related": {
            "added": len(related_added),
            "measured": related_measure,
        },
        "baseline": {
            "count": len(baseline),
            "first_seen": first_seen,
            "second_seen": len(metrics) - first_seen,
        },
        "selection": selection_stats,
        "online_coverage": measure_stats.get("online_coverage", 0.0),
        "degraded_reason": degraded_reason,
        "errors": len(errors),
        "elapsed_seconds": round(time.monotonic() - wall_started, 1),
        "generated_at": result.generated_at,
        "window_start": result.window_start,
        "window_end": result.window_end,
    }

    # 7) 写库（只 round + video_metric，单事务）
    result.stats["db_written"] = bool(use_db)
    if use_db:
        persist_result(result, conn=db_conn, url=url)
        result.stats["round_id"] = result.round_id
    return result


# ---------------------------------------------------------------- 便捷入口
def run_once(
    *,
    use_db: bool = True,
    limit: int | None = None,
    database_url: str | None = None,
    now: int | None = None,
) -> VideoRouteResult:
    """CLI 用便捷入口：真机 ``BiliSource`` + 可选候选上限。"""
    plan = VideoRoutePlan(max_candidates=limit)
    return run_video_route(None, plan, now=now, use_db=use_db, database_url=database_url)
