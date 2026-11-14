"""热度打分：多信号、可解释、可跨轮比较。

热度不是「曝光量」：本模型把「当前是否有人专门讲它」（元话语）、「是否被真实
使用/模仿」（评论/弹幕）、「增速」「互动质量」「广度」分开计量，再用新鲜度、
置信度与集中度惩罚收敛到 0–100。

关键设计：
- 各信号先做 log/饱和归一（带固定锚点），再按权重相加；批内百分位只作辅助，
  避免「同一批里矮子拔将军」导致跨轮不可比。
- 低证据候选按置信度向 0 收缩，不靠一条评论冲榜。
- 时间衰减按「最后一条证据」而不是「首次入库」，30 天无新证据自然掉热。
- 所有分项写入 components，报告里能逐项核对。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .knowledge import Knowledge
from .models import Meme, clamp
from .text import pct


@dataclass
class HeatConfig:
    meta_window_days: int = 90
    current_meta_days: int = 60
    usage_window_days: int = 30
    recent_days: int = 7
    meta_half_life_days: float = 21.0
    usage_half_life_days: float = 10.0
    # 固定锚点：达到该量级即视为该维度 1.0（避免长尾把分布拉平）。
    video_view_anchor: float = 5_000_000
    meta_count_anchor: int = 6
    usage_count_anchor: int = 40
    usage_video_anchor: int = 12
    like_anchor: float = 120
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "meta_reach": 0.45,
            "meta_coverage": 0.18,
            "organic": 0.13,
            "velocity": 0.07,
            "engagement": 0.09,
            "breadth": 0.04,
            "prior": 0.04,
        }
    )
    min_meme_prob: float = 0.5
    hot_threshold: float = 50.0
    observe_threshold: float = 15.0
    min_usage_videos: int = 2
    min_usage_docs: int = 2
    history_alpha: float = 0.65

    def as_dict(self) -> dict[str, Any]:
        return {
            "meta_window_days": self.meta_window_days,
            "current_meta_days": self.current_meta_days,
            "usage_window_days": self.usage_window_days,
            "recent_days": self.recent_days,
            "meta_half_life_days": self.meta_half_life_days,
            "usage_half_life_days": self.usage_half_life_days,
            "video_view_anchor": self.video_view_anchor,
            "meta_count_anchor": self.meta_count_anchor,
            "usage_count_anchor": self.usage_count_anchor,
            "usage_video_anchor": self.usage_video_anchor,
            "like_anchor": self.like_anchor,
            "weights": dict(self.weights),
            "min_meme_prob": self.min_meme_prob,
            "hot_threshold": self.hot_threshold,
            "observe_threshold": self.observe_threshold,
            "min_usage_videos": self.min_usage_videos,
            "min_usage_docs": self.min_usage_docs,
            "history_alpha": self.history_alpha,
        }


def _saturate(value: float, anchor: float) -> float:
    if anchor <= 0:
        return 0.0
    return clamp(math.log1p(max(0.0, value)) / math.log1p(anchor))


def _age_days(ts: int, now: int) -> float:
    if not ts:
        return 999.0
    return max(0.0, (now - ts) / 86400.0)


def _decay(ts: int, now: int, half_life_days: float) -> float:
    if not ts or half_life_days <= 0:
        return 0.0
    return math.pow(0.5, _age_days(ts, now) / half_life_days)


@dataclass
class HeatSignals:
    meta_videos: int = 0
    meta_interest_videos: int = 0
    meta_ups: int = 0
    meta_views: float = 0.0
    meta_views_editorial: float = 0.0
    meta_views_interest: float = 0.0
    meta_likes: float = 0.0
    meta_replies: float = 0.0
    meta_7d: int = 0
    meta_prev: int = 0
    usage_docs: int = 0
    usage_videos: int = 0
    usage_zones: int = 0
    evidence_docs: int = 0
    evidence_videos: int = 0
    evidence_rate_2x2: bool = False
    usage_7d: int = 0
    usage_prev: int = 0
    organic_docs: int = 0
    organic_videos: int = 0
    organic_likes: float = 0.0
    like_weighted: float = 0.0
    mean_like: float = 0.0
    max_video_share: float = 0.0
    last_ts: int = 0
    days_since_last: float = 999.0
    recency: float = 0.0
    velocity: float = 0.0
    confidence: float = 0.0
    is_current: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "meta_videos": self.meta_videos,
            "meta_interest_videos": self.meta_interest_videos,
            "meta_ups": self.meta_ups,
            "meta_views": round(self.meta_views, 1),
            "meta_views_editorial": round(self.meta_views_editorial, 1),
            "meta_views_interest": round(self.meta_views_interest, 1),
            "meta_likes": round(self.meta_likes, 1),
            "meta_replies": round(self.meta_replies, 1),
            "meta_7d": self.meta_7d,
            "meta_prev": self.meta_prev,
            "usage_docs": self.usage_docs,
            "usage_videos": self.usage_videos,
            "usage_zones": self.usage_zones,
            "evidence_docs": self.evidence_docs,
            "evidence_videos": self.evidence_videos,
            "evidence_rate_2x2": self.evidence_rate_2x2,
            "usage_7d": self.usage_7d,
            "usage_prev": self.usage_prev,
            "organic_docs": self.organic_docs,
            "organic_videos": self.organic_videos,
            "organic_likes": round(self.organic_likes, 1),
            "like_weighted": round(self.like_weighted, 2),
            "mean_like": round(self.mean_like, 2),
            "max_video_share": round(self.max_video_share, 3),
            "last_ts": self.last_ts,
            "days_since_last": round(self.days_since_last, 2),
            "recency": round(self.recency, 3),
            "velocity": round(self.velocity, 3),
            "confidence": round(self.confidence, 3),
            "is_current": self.is_current,
        }


def _velocity(usage_7d: int, usage_prev: int, meta_7d: int, meta_prev: int) -> float:
    usage_ratio = ((usage_7d / 7.0) + 0.02) / ((usage_prev / 23.0) + 0.02)
    meta_ratio = ((meta_7d / 7.0) + 0.02) / ((meta_prev / 23.0) + 0.02)
    usage_ratio = clamp(usage_ratio, 0.05, 8.0)
    meta_ratio = clamp(meta_ratio, 0.05, 8.0)
    raw = 0.55 * math.log2(usage_ratio) + 0.45 * math.log2(meta_ratio)
    return clamp((raw + 1.0) / 3.0, 0.0, 1.0)


class HeatModel:
    def __init__(self, config: HeatConfig | None = None):
        self.config = config or HeatConfig()

    def signals(self, meme: Meme, *, now: int) -> HeatSignals:
        config = self.config
        meta_recent = [
            item for item in meme.meta
            if item.pubdate and _age_days(item.pubdate, now) <= config.meta_window_days
        ]
        meta_editorial = [item for item in meta_recent if item.kind != "search_interest"]
        meta_interest = [item for item in meta_recent if item.kind == "search_interest"]
        meta_by_bvid = {item.bvid: item for item in meta_recent if item.bvid}
        editorial_bvids = {item.bvid: item for item in meta_editorial if item.bvid}
        interest_bvids = {item.bvid: item for item in meta_interest if item.bvid}
        # 标题/简介里的提及只是「被谈论」，不等于「被使用」；热度主口径只算
        # 评论/弹幕。标题提及由 meta（梗百科/盘点）单独计量，避免作品名刷分。
        usage_recent = [
            item for item in meme.usage
            if item.ctime
            and item.kind in ("comment", "danmaku")
            and _age_days(item.ctime, now) <= config.usage_window_days
        ]
        usage_7d = [item for item in usage_recent if _age_days(item.ctime, now) <= config.recent_days]
        usage_prev = [item for item in usage_recent if config.recent_days < _age_days(item.ctime, now) <= config.usage_window_days]
        meta_7d = [item for item in meta_recent if _age_days(item.pubdate, now) <= config.recent_days]
        meta_prev = [item for item in meta_recent if config.recent_days < _age_days(item.pubdate, now) <= config.meta_window_days]
        meta_current = [
            item for item in meta_recent
            if _age_days(item.pubdate, now) <= config.current_meta_days
        ]

        organic = [item for item in usage_recent if item.bvid not in meta_by_bvid]
        organic_videos = len({item.bvid for item in organic if item.bvid})
        organic_zones = len({item.zone for item in organic if item.zone})
        usage_videos = len({item.bvid for item in usage_recent if item.bvid})
        usage_zones = len({item.zone for item in usage_recent if item.zone})
        # 2×2 只认「独立使用」：排除该梗自己的梗百科/梗指南视频下的评论。
        evidence_docs = len({item.doc_id for item in organic})
        evidence_videos = len({item.bvid for item in organic if item.bvid})
        evidence_rate_2x2 = evidence_docs >= 2 and evidence_videos >= 2
        per_video: dict[str, int] = {}
        for item in usage_recent:
            if item.bvid:
                per_video[item.bvid] = per_video.get(item.bvid, 0) + 1
        max_share = (max(per_video.values()) / len(usage_recent)) if len(usage_recent) >= 3 else 0.0

        like_weighted = sum(
            math.log1p(max(0, item.like)) * _decay(item.ctime, now, 14.0)
            for item in usage_recent
            if item.ctime
        )
        mean_like = (
            sum(item.like for item in organic) / len(organic) if organic else 0.0
        )
        meta_views_editorial = sum(item.view for item in editorial_bvids.values())
        meta_views_interest = sum(item.view for item in interest_bvids.values())
        # 搜索「X 梗」命中但标题不一定是专门讲梗的视频，按 0.35 折算，
        # 避免把任何同名/相关视频都当成等价的梗视频。
        meta_views = meta_views_editorial + 0.35 * meta_views_interest
        meta_likes = sum(item.like for item in editorial_bvids.values())
        meta_replies = sum(item.reply for item in editorial_bvids.values())
        meta_ups = len({item.owner for item in meta_editorial if item.owner})

        last_candidates = [item.pubdate for item in meta_recent if item.pubdate]
        last_candidates += [item.ctime for item in usage_recent if item.ctime]
        last_ts = max(last_candidates) if last_candidates else 0
        days_since_last = _age_days(last_ts, now) if last_ts else 999.0
        half_life = config.meta_half_life_days
        if usage_recent and meta_recent:
            half_life = (config.meta_half_life_days + config.usage_half_life_days) / 2
        elif usage_recent:
            half_life = config.usage_half_life_days
        recency = math.pow(0.5, days_since_last / half_life) if last_ts else 0.05
        recency = clamp(recency, 0.05, 1.0)
        velocity = _velocity(len(usage_7d), len(usage_prev), len(meta_7d), len(meta_prev))

        volume = min(
            1.0,
            math.log1p(len(usage_recent) + 2 * len(editorial_bvids) + 0.5 * len(interest_bvids))
            / math.log1p(24),
        )
        diversity = min(
            1.0,
            (usage_videos + 1.5 * len(editorial_bvids) + 0.5 * len(interest_bvids)) / 6.0,
        )
        confidence = clamp(0.2 + 0.45 * volume + 0.35 * diversity, 0.0, 1.0)
        if any(item.kind == "dedicated" for item in meta_editorial):
            confidence = clamp(confidence + 0.08, 0.0, 1.0)
        if not organic:
            confidence *= 0.85

        is_current = bool(
            meta_current
            or usage_7d
            or (len(usage_recent) >= 2 and days_since_last <= config.usage_window_days)
        )
        # 只有旧梗百科、没有近期使用 → 不是「当前热梗」。
        if not usage_7d and not usage_recent and meta_recent and days_since_last > config.meta_window_days:
            is_current = False

        return HeatSignals(
            meta_videos=len(editorial_bvids),
            meta_interest_videos=len(interest_bvids),
            meta_ups=meta_ups,
            meta_views=meta_views,
            meta_views_editorial=meta_views_editorial,
            meta_views_interest=meta_views_interest,
            meta_likes=meta_likes,
            meta_replies=meta_replies,
            meta_7d=len(meta_7d),
            meta_prev=len(meta_prev),
            usage_docs=len(usage_recent),
            usage_videos=usage_videos,
            usage_zones=usage_zones,
            evidence_docs=evidence_docs,
            evidence_videos=evidence_videos,
            evidence_rate_2x2=evidence_rate_2x2,
            usage_7d=len(usage_7d),
            usage_prev=len(usage_prev),
            organic_docs=len(organic),
            organic_videos=organic_videos,
            organic_likes=sum(item.like for item in organic),
            like_weighted=like_weighted,
            mean_like=mean_like,
            max_video_share=max_share,
            last_ts=last_ts,
            days_since_last=days_since_last,
            recency=recency,
            velocity=velocity,
            confidence=confidence,
            is_current=is_current,
        )

    def score_meme(
        self,
        meme: Meme,
        *,
        now: int,
        history: dict[str, float] | None = None,
        knowledge: Knowledge | None = None,
    ) -> Meme:
        config = self.config
        signals = self.signals(meme, now=now)
        meme.signals = signals.as_dict()
        meme.confidence = signals.confidence

        rejection = ""
        if meme.meme_prob < config.min_meme_prob:
            rejection = "low_meme_prob"
        elif knowledge and knowledge.blocked_reason(meme.canonical) and (
            not meme.known or knowledge.blocked_reason(meme.canonical) == "sensitive"
        ):
            rejection = knowledge.blocked_reason(meme.canonical)
        elif signals.usage_docs == 0 and signals.meta_videos == 0:
            rejection = "no_evidence"
        elif signals.usage_docs < config.min_usage_docs and not meme.known and signals.meta_videos == 0:
            rejection = "weak_usage"
        elif signals.usage_videos < config.min_usage_videos and not meme.known and not meme.meta:
            rejection = "single_video"
        elif not signals.is_current:
            rejection = "stale"
        meme.rejection = rejection
        meme.meme_prob = max(meme.meme_prob, 0.0)

        meta_reach = _saturate(signals.meta_views, config.video_view_anchor)
        dedicated_count = sum(1 for item in meme.meta if item.kind == "dedicated" and item.pubdate and _age_days(item.pubdate, now) <= config.meta_window_days)
        compilation_count = sum(1 for item in meme.meta if item.kind in ("compilation", "expansion") and item.pubdate and _age_days(item.pubdate, now) <= config.meta_window_days)
        interest_count = sum(1 for item in meme.meta if item.kind == "search_interest" and item.pubdate and _age_days(item.pubdate, now) <= config.meta_window_days)
        # 「专门讲这个梗」的视频，其覆盖权重高于「盘点里提一句」，
        # 后者又高于搜索结果里的相关视频；不是简单的条数百分位。
        if dedicated_count:
            meta_coverage = 0.6 + 0.4 * min(1.0, (dedicated_count - 1) / 3.0)
        elif compilation_count:
            meta_coverage = 0.45 + 0.35 * min(1.0, compilation_count / 2.0)
        else:
            meta_coverage = 0.25 * min(1.0, interest_count / 2.0)
        meta_coverage = clamp(meta_coverage, 0.0, 1.0)
        organic_volume = _saturate(signals.organic_docs, config.usage_count_anchor)
        organic_spread = _saturate(signals.organic_videos, config.usage_video_anchor)
        like_quality = _saturate(signals.like_weighted, config.like_anchor)
        organic = 0.45 * organic_volume + 0.30 * organic_spread + 0.25 * like_quality

        meta_ratio = (signals.meta_likes / signals.meta_views) if signals.meta_views > 0 else 0.0
        meta_engagement = clamp(meta_ratio / 0.06, 0.0, 1.0)
        organic_engagement = _saturate(signals.mean_like, 200)
        engagement = 0.6 * meta_engagement + 0.4 * organic_engagement

        breadth = clamp(
            0.55 * _saturate(signals.usage_videos, 15)
            + 0.30 * _saturate(signals.usage_zones, 8)
            + 0.15 * _saturate(signals.meta_ups, 4),
            0.0,
            1.0,
        )
        dedicated = sum(
            1 for item in meme.meta
            if item.kind == "dedicated" and item.pubdate
            and _age_days(item.pubdate, now) <= config.meta_window_days
        )
        historical_dedicated = any(
            item.kind == "dedicated" and item.pubdate
            and _age_days(item.pubdate, now) <= 365
            for item in meme.meta
        )
        if meme.known:
            prior = 1.0
        elif dedicated or historical_dedicated:
            prior = 0.9
        elif meme.meta:
            prior = 0.6
        elif any(candidate.pattern for candidate in meme.candidates):
            prior = 0.35
        else:
            prior = 0.2

        norms = {
            "meta_reach": meta_reach,
            "meta_coverage": meta_coverage,
            "organic": organic,
            "velocity": signals.velocity,
            "engagement": engagement,
            "breadth": breadth,
            "prior": prior,
        }
        weights = config.weights
        base = sum(weights[key] * norms.get(key, 0.0) for key in weights)
        concentration_penalty = 1.0
        if signals.usage_videos >= 3 and signals.max_video_share > 0.7:
            concentration_penalty = 1.0 - 0.3 * ((signals.max_video_share - 0.7) / 0.3)
        if not signals.organic_docs and signals.meta_videos <= 1:
            concentration_penalty *= 0.9
        confidence_factor = 0.7 + 0.3 * signals.confidence
        raw_score = 100.0 * base
        score = raw_score * signals.recency * confidence_factor * concentration_penalty
        evergreen_penalty = 1.0
        if knowledge and knowledge.is_evergreen(meme.canonical) and not dedicated_count:
            # 常青表达仍在被使用是事实，但不能只靠「用得多」冒充当前热梗；
            # 有专门梗视频时不打这个折。
            evergreen_penalty = 0.75
            score *= evergreen_penalty
        if rejection:
            score = 0.0

        meme.raw_score = round(raw_score, 2)
        meme.score = round(clamp(score, 0, 100), 2)
        if history:
            previous = history.get(meme.family_key)
            if previous is not None:
                meme.smoothed_score = round(
                    config.history_alpha * meme.score
                    + (1.0 - config.history_alpha) * float(previous),
                    2,
                )
        if rejection:
            meme.status = "rejected"
            meme.heat_level = "剔除"
        elif meme.score >= config.hot_threshold:
            meme.status = "hot"
            meme.heat_level = "爆发" if meme.score >= 70 else "高位"
        elif meme.score >= config.observe_threshold:
            meme.status = "rising" if signals.velocity >= 0.62 else "observing"
            meme.heat_level = "上升" if signals.velocity >= 0.62 else "活跃"
        else:
            meme.status = "weak"
            meme.heat_level = "低证据"

        components: dict[str, Any] = {}
        for key in weights:
            norm_value = norms.get(key, 0.0)
            components[key] = {
                "raw": round(norm_value, 4),
                "weight": weights[key],
                "points": round(100.0 * weights[key] * norm_value, 2),
            }
        components["decay"] = {
            "recency": round(signals.recency, 4),
            "days_since_last": round(signals.days_since_last, 2),
            "points": round(raw_score * (signals.recency - 1.0), 2),
        }
        components["confidence"] = {
            "value": round(signals.confidence, 4),
            "factor": round(confidence_factor, 4),
        }
        components["penalty"] = {
            "concentration": round(concentration_penalty, 4),
            "max_video_share": round(signals.max_video_share, 3),
            "evergreen": round(evergreen_penalty, 4),
        }
        components["source"] = {
            "known": meme.known,
            "dedicated_meta": dedicated,
            "meta_kinds": sorted({item.kind for item in meme.meta}),
        }
        meme.components = components
        meme.reasons = self._reasons(meme, signals, rejection)
        # 2×2 独立使用是「证据质量徽章」，不是查询硬门槛：用户盲标确认
        # 部分 explainer-only 条目（吴家村/灭建国/MJ宇宙/狗尾草等）是真梗。
        # 发布门再用 evidence_rate_2x2 决定是否只发「强证据」。
        return meme

    def _reasons(self, meme: Meme, signals: HeatSignals, rejection: str) -> list[str]:
        if rejection:
            mapping = {
                "low_meme_prob": "梗性证据不足（不像一个可复用的梗）",
                "stale": "近 30 天没有新证据，按当前热度剔除",
                "no_evidence": "没有可用证据",
                "weak_usage": "评论/弹幕证据太少",
                "single_video": "只在单视频出现，无法排除单点刷屏",
                "news_like": "命中新闻/事件模板",
                "marketing": "命中营销模板",
                "sensitive": "命中敏感词",
                "common_word": "命中通用词表",
            }
            return [mapping.get(rejection, f"剔除：{rejection}")]
        reasons: list[str] = []
        if signals.meta_videos:
            reasons.append(
                f"近 {self.config.meta_window_days} 天有 {signals.meta_videos} 个梗视频，"
                f"合计 {int(signals.meta_views_editorial):,} 播放"
            )
        if signals.meta_interest_videos:
            reasons.append(
                f"定向搜索到 {signals.meta_interest_videos} 个近期相关视频，"
                f"合计 {int(signals.meta_views_interest):,} 播放"
            )
        if signals.organic_docs:
            reasons.append(
                f"评论/弹幕使用 {signals.organic_docs} 条、覆盖 {signals.organic_videos} 个视频"
            )
        if signals.usage_7d:
            reasons.append(f"近 7 天新增 {signals.usage_7d} 条证据")
        if signals.velocity >= 0.65:
            reasons.append(f"增速偏快（velocity={signals.velocity:.2f}）")
        elif signals.velocity <= 0.35 and signals.usage_prev:
            reasons.append("近 7 天较前期降温")
        if signals.evidence_rate_2x2:
            reasons.append(
                f"独立使用证据 {signals.evidence_docs} 条 / {signals.evidence_videos} 个视频"
            )
        else:
            reasons.append(
                f"独立使用证据不足：{signals.evidence_docs} 条 / {signals.evidence_videos} 个视频"
            )
        if signals.days_since_last <= 1:
            reasons.append("最近 24 小时仍有新证据")
        elif signals.days_since_last <= 7:
            reasons.append(f"最后一条证据在 {signals.days_since_last:.1f} 天前")
        if meme.known:
            reasons.append("命中已知梗/别名")
        return reasons

    def score_memes(
        self,
        memes: list[Meme],
        *,
        now: int,
        history: dict[str, float] | None = None,
        knowledge: Knowledge | None = None,
        current_only: bool = False,
    ) -> list[Meme]:
        scored = [
            self.score_meme(meme, now=now, history=history, knowledge=knowledge)
            for meme in memes
        ]
        if current_only:
            scored = [
                meme for meme in scored
                if meme.status != "rejected" and meme.signals.get("is_current")
            ]
        rank = {"hot": 0, "rising": 1, "observing": 2, "weak": 3, "rejected": 4}
        scored.sort(
            key=lambda meme: (
                rank.get(meme.status, 9),
                -(meme.smoothed_score if meme.smoothed_score is not None else meme.score),
                -meme.confidence,
                -meme.last_seen,
                meme.family_key,
            )
        )
        return scored
