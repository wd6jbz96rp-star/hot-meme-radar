"""端到端查询：采集 → 元话语/使用证据 → 候选 → 梗族 → 热度 → 报告。

模块不碰数据库、不启服务；``find_hot_memes`` 只依赖注入的 source。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .bili import BiliSource
from .collect import CollectionPlan, collect_corpus
from .discover import (
    DiscoveryConfig,
    extract_candidates,
    extract_meta_mentions,
    merge_families,
    parse_title_meta,
)
from .heat import HeatConfig, HeatModel
from .knowledge import Knowledge
from .models import (
    SCHEMA_VERSION,
    Corpus,
    HotMemeReport,
    Meme,
    MetaMention,
    iso,
    now_ts,
)
from .text import normalize_surface, normalize_text


@dataclass
class QueryConfig:
    plan: CollectionPlan = field(default_factory=CollectionPlan)
    heat: HeatConfig = field(default_factory=HeatConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    limit: int = 20
    include_candidates: int = 200
    expand_queries: bool = True
    expand_limit: int = 10
    expand_results: int = 8
    interest_window_days: int = 60
    verify: bool = False
    verify_gate: bool = False
    verify_limit: int = 12
    meta_max_age_days: int = 365

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.as_dict(),
            "heat": self.heat.as_dict(),
            "discovery": {
                "min_count": self.discovery.min_count,
                "min_df": self.discovery.min_df,
                "min_cohesion": self.discovery.min_cohesion,
                "min_entropy": self.discovery.min_entropy,
                "max_candidates": self.discovery.max_candidates,
            },
            "limit": self.limit,
            "expand_queries": self.expand_queries,
            "expand_limit": self.expand_limit,
            "interest_window_days": self.interest_window_days,
            "verify": self.verify,
            "verify_gate": self.verify_gate,
            "verify_limit": self.verify_limit,
            "meta_max_age_days": self.meta_max_age_days,
        }


def _requests_used(source: Any, baseline: int) -> int:
    http = getattr(source, "http", None)
    if http is None:
        return 0
    return max(0, int(getattr(http, "requests", 0)) - baseline)


def _prepare(corpus: Corpus, knowledge: Knowledge, config: QueryConfig, now: int) -> tuple[list, list[Meme]]:
    mentions = extract_meta_mentions(
        corpus.videos, knowledge, now=now, max_age_days=config.meta_max_age_days
    )
    candidates = extract_candidates(
        corpus.docs,
        knowledge,
        now=now,
        meta_mentions=mentions,
        config=config.discovery,
    )
    memes = merge_families(candidates, knowledge, now=now, meta_mentions=mentions)
    return mentions, memes


def _expand_query(
    source: Any,
    corpus: Corpus,
    mentions: list[MetaMention],
    memes: list[Meme],
    knowledge: Knowledge,
    config: QueryConfig,
    now: int,
) -> tuple[list[MetaMention], dict[str, Any]]:
    """对「有使用、但没有元话语」的候选做一次定向搜索，补「X 是什么梗」证据。

    这是查询精度的重要一环：n-gram 只能说明某串字反复出现，定向搜索能回答
    「有没有人把它当梗讲」。只查候选本身，不搜泛词，避免请求膨胀。
    """
    stats: dict[str, Any] = {
        "attempted": 0, "hit": 0, "miss": 0, "errors": 0,
        "meta_added": 0, "interest_added": 0, "queries": [],
    }
    if not config.expand_queries or config.expand_limit <= 0:
        return mentions, stats
    baseline = int(getattr(getattr(source, "http", None), "requests", 0))
    added: list[MetaMention] = []
    base_candidates = [
        meme for meme in memes
        if not meme.known
        and 2 <= len(meme.norm) <= 14
        and (meme.meta or meme.usage)
        and meme.meme_prob >= 0.45
        and not knowledge.is_generic(meme.canonical)
        and not knowledge.blocked_reason(meme.canonical)
        # 2 字候选只查有专门梗视频或跨视频使用的；否则「音乐/游戏」这类
        # 通用名词会把定向搜索预算吃光。
        and (
            len(meme.norm) >= 3
            or any(item.kind == "dedicated" for item in meme.meta)
            or len({item.bvid for item in meme.usage if item.bvid}) >= 2
        )
    ]
    dedicated_pool = [
        meme for meme in base_candidates
        if any(item.kind == "dedicated" for item in meme.meta)
    ]
    dedicated_ids = {id(meme) for meme in dedicated_pool}
    other_pool = [meme for meme in base_candidates if id(meme) not in dedicated_ids]
    dedicated_pool.sort(
        key=lambda meme: (meme.meme_prob, len(meme.usage), meme.last_seen),
        reverse=True,
    )
    other_pool.sort(
        key=lambda meme: (bool(meme.meta), meme.meme_prob, len(meme.usage), meme.last_seen),
        reverse=True,
    )
    half = max(1, config.expand_limit // 2)
    candidates = dedicated_pool[:half] + other_pool[: config.expand_limit - min(half, len(dedicated_pool))]
    if len(candidates) < config.expand_limit:
        candidates += dedicated_pool[half : half + (config.expand_limit - len(candidates))]
    for meme in candidates:
        if stats["attempted"] >= config.expand_limit:
            break
        if _requests_used(source, baseline) >= config.expand_limit:
            break
        query = f"{meme.canonical} 梗"
        stats["attempted"] += 1
        stats["queries"].append(query)
        try:
            videos = source.search_videos(query, limit=max(8, config.expand_results))
        except Exception:
            stats["errors"] += 1
            continue
        if not videos:
            stats["miss"] += 1
            continue
        matched = 0
        used_bvids: set[str] = set()
        for rank, video in enumerate(videos, start=1):
            if not video.bvid or video.bvid in used_bvids:
                continue
            if video.pubdate and now - video.pubdate > config.meta_max_age_days * 86400:
                continue
            parsed = parse_title_meta(video.title, knowledge)
            for surface, norm, confidence in parsed:
                if norm != meme.norm:
                    continue
                mention = MetaMention(
                    surface=surface,
                    canonical=surface,
                    norm=norm,
                    bvid=video.bvid,
                    title=video.title,
                    pubdate=video.pubdate,
                    view=video.view,
                    like=video.like,
                    reply=video.reply,
                    danmaku=video.danmaku,
                    owner=video.owner,
                    query=query,
                    kind="dedicated" if confidence >= 0.9 else "expansion",
                    confidence=max(0.8, confidence),
                )
                added.append(mention)
                used_bvids.add(video.bvid)
                stats["meta_added"] += 1
                matched += 1
                break
            else:
                # 没有明确「是什么梗」标题时，用近期且命中的搜索结果作为
                # 「当前关注度」旁证；它不进 meta 覆盖的 editorial 计数，
                # 但计入热度里的相关视频/播放。
                title_norm = normalize_surface(video.title)
                if meme.norm not in title_norm and (
                    rank > 3 or len(meme.usage) < 3
                ):
                    continue
                age_days = (now - video.pubdate) / 86400 if video.pubdate else 999
                if age_days > config.interest_window_days:
                    continue
                added.append(
                    MetaMention(
                        surface=meme.canonical,
                        canonical=meme.canonical,
                        norm=meme.norm,
                        bvid=video.bvid,
                        title=video.title,
                        pubdate=video.pubdate,
                        view=video.view,
                        like=video.like,
                        reply=video.reply,
                        danmaku=video.danmaku,
                        owner=video.owner,
                        query=query,
                        kind="search_interest",
                        confidence=0.45,
                    )
                )
                used_bvids.add(video.bvid)
                stats["interest_added"] += 1
                matched += 1
        if matched:
            stats["hit"] += 1
        else:
            stats["miss"] += 1
    if added:
        # 补进语料，后续 merge 会按 norm 归并。
        mentions = mentions + added
    return mentions, stats


def _attach_verification(
    memes: list[Meme],
    verifier: Callable[..., Any],
    *,
    limit: int,
    now: int,
    gate: bool = False,
) -> dict[str, Any]:
    stats: dict[str, Any] = {"attempted": 0, "ok": 0, "rejected": 0, "errors": 0}
    for meme in memes[:limit]:
        if meme.status == "rejected":
            continue
        stats["attempted"] += 1
        try:
            try:
                result = verifier(meme, now=now)
            except TypeError:
                result = verifier(meme)
        except Exception as exc:  # 审核失败绝不影响规则榜
            stats["errors"] += 1
            meme.verified = {"error": str(exc)[:200]}
            continue
        if result is None:
            stats["errors"] += 1
            continue
        payload = result if isinstance(result, dict) else getattr(result, "as_dict", lambda: {})()
        meme.verified = payload
        if gate and payload.get("is_meme") is False and (
            float(payload.get("confidence", 0) or 0) >= 0.5
            or float(payload.get("heat", 0) or 0) <= 15
        ):
            meme.status = "rejected"
            meme.rejection = "llm_not_meme"
            meme.heat_level = "剔除"
            stats["rejected"] += 1
        else:
            stats["ok"] += 1
            if gate and payload.get("is_meme") and payload.get("is_current") is False:
                meme.status = "rejected"
                meme.rejection = "llm_not_current"
                meme.heat_level = "剔除"
                stats["rejected"] += 1
    return stats


def build_report(
    corpus: Corpus,
    *,
    knowledge: Knowledge | None = None,
    config: QueryConfig | None = None,
    now: int | None = None,
    history: dict[str, float] | None = None,
    verifier: Callable[[Meme], Any] | None = None,
    extra_stats: dict[str, Any] | None = None,
    mentions: list[MetaMention] | None = None,
    memes: list[Meme] | None = None,
) -> HotMemeReport:
    knowledge = knowledge or Knowledge()
    config = config or QueryConfig()
    now = now or int(time.time())
    started = time.time()
    if memes is None:
        mentions, memes = _prepare(corpus, knowledge, config, now)
    model = HeatModel(config.heat)
    scored = model.score_memes(memes, now=now, history=history, knowledge=knowledge)
    verification_stats: dict[str, Any] = {}
    if verifier is not None and config.verify:
        verification_stats = _attach_verification(
            scored, verifier, limit=config.verify_limit, now=now, gate=config.verify_gate
        )
        client = getattr(verifier, "llm", None)
        if client is not None and hasattr(client, "stats"):
            try:
                verification_stats["llm"] = client.stats.as_dict()
                verification_stats["llm_config"] = client.config.redacted()
            except Exception:
                pass
        scored.sort(
            key=lambda meme: (
                meme.status in ("hot", "rising", "observing"),
                meme.smoothed_score if meme.smoothed_score is not None else meme.score,
                meme.confidence,
            ),
            reverse=True,
        )
    current = [
        meme for meme in scored
        if meme.status in ("hot", "rising", "observing") and meme.signals.get("is_current")
    ]
    stats = dict(corpus.stats)
    stats.update(
        {
            "discovery_candidates": sum(len(meme.candidates) for meme in scored),
            "families": len(scored),
            "families_with_meta": sum(1 for meme in scored if meme.meta),
            "families_with_usage": sum(1 for meme in scored if meme.usage),
            "current_families": len(current),
            "rejected_families": sum(1 for meme in scored if meme.status == "rejected"),
            "verification": verification_stats,
            "report_elapsed": round(time.time() - started, 2),
        }
    )
    if extra_stats:
        stats.update(extra_stats)
    report = HotMemeReport(
        generated_at=iso(now),
        as_of=datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        window_days=config.plan.comment_window_days,
        memes=current[: config.limit],
        candidates=scored[: config.include_candidates],
        stats=stats,
        config=config.as_dict(),
        schema_version=SCHEMA_VERSION,
        notes=[
            "热度分 0–100，由元话语、使用、增速、互动、广度、先验合成；低于门槛或证据不足不下发。",
            "证据为公开 B站 数据，评论样例会截断；原始视频/评论可通过 evidence_ids 回查。",
        ],
    )
    return report


def find_hot_memes(
    source: BiliSource,
    *,
    config: QueryConfig | None = None,
    now: int | None = None,
    knowledge: Knowledge | None = None,
    history: dict[str, float] | None = None,
    verifier: Callable[[Meme], Any] | None = None,
) -> HotMemeReport:
    config = config or QueryConfig()
    now = now or now_ts()
    started = time.time()
    knowledge = knowledge or Knowledge()
    corpus = collect_corpus(source, config.plan, now=now)
    base_stats = {
        "collection_elapsed": corpus.stats.get("elapsed"),
        "collection_requests": corpus.stats.get("requests"),
    }
    mentions, memes = _prepare(corpus, knowledge, config, now)
    if config.expand_queries and config.expand_limit > 0:
        mentions, expansion_stats = _expand_query(
            source,
            corpus,
            mentions,
            memes,
            knowledge,
            config,
            now,
        )
        # 重新归并（新增 expansion 元话语）
        candidates = extract_candidates(
            corpus.docs,
            knowledge,
            now=now,
            meta_mentions=mentions,
            config=config.discovery,
        )
        memes = merge_families(
            candidates, knowledge, now=now, meta_mentions=mentions
        )
        base_stats["expansion"] = expansion_stats
    report = build_report(
        corpus,
        knowledge=knowledge,
        config=config,
        now=now,
        history=history,
        verifier=verifier,
        extra_stats={
            **base_stats,
            "requests_total": int(getattr(getattr(source, "http", None), "requests", 0)),
            "elapsed_total": round(time.time() - started, 1),
            "source_stats": source.stats() if hasattr(source, "stats") else {},
        },
        mentions=mentions,
        memes=memes,
    )
    return report


def query_from_corpus(
    corpus: Corpus,
    *,
    knowledge: Knowledge | None = None,
    config: QueryConfig | None = None,
    now: int | None = None,
    history: dict[str, float] | None = None,
    verifier: Callable[[Meme], Any] | None = None,
) -> HotMemeReport:
    return build_report(
        corpus,
        knowledge=knowledge,
        config=config,
        now=now,
        history=history,
        verifier=verifier,
    )


def load_history(path: str | Path | None) -> dict[str, float]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict):
        values = payload.get("scores") if isinstance(payload.get("scores"), dict) else payload
        result: dict[str, float] = {}
        for key, value in values.items():
            try:
                result[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return result
    return {}


def history_from_report(report: HotMemeReport) -> dict[str, float]:
    result: dict[str, float] = {}
    for meme in report.memes:
        key = normalize_surface(meme.family_key)
        if key:
            result[key] = float(meme.smoothed_score if meme.smoothed_score is not None else meme.score)
    return result
