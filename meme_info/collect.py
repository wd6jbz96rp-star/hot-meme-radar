"""语料采集：多通道查询 + 评论/弹幕证据，产出可回放的 Corpus。

通道分工：
- 排行榜/热门：曝光伏与「视频统计」底数；
- 搜索：当前热梗的元话语（梗百科/梗指南/盘点）与近期候选；
- 评论：梗被真实使用/讨论的语料（带 ctime/like）；
- 弹幕：整句重复，作为模仿性扩散旁证。

模块不写数据库；缓存可选（HTTP 文件缓存），真机验证默认写 /tmp。
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .bili import BiliSource, parse_danmaku_xml
from .models import Comment, Corpus, Danmaku, Doc, Video, iso
from .text import normalize_surface

DEFAULT_SEARCH_QUERIES = (
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


@dataclass
class CollectionPlan:
    zones: tuple[int, ...] = (119, 160, 1, 4, 181, 3, 5, 36)
    popular_size: int = 50
    search_queries: tuple[str, ...] = DEFAULT_SEARCH_QUERIES
    search_limit: int = 20
    comment_videos: int = 30
    comment_modes: tuple[int, ...] = (3, 2)
    comment_pages: dict[int, int] = field(default_factory=lambda: {3: 1, 2: 2})
    comment_window_days: int = 30
    comment_recent_days: int = 7
    comment_max_per_video: int = 40
    comment_max_chars: int = 300
    danmaku_videos: int = 8
    max_danmaku_per_video: int = 200
    max_requests: int = 260
    enable_search: bool = True
    enable_hot_search: bool = True
    search_recent_days: int = 120
    include_titles: bool = True
    headroom_ratio: float = 0.5

    def as_dict(self) -> dict:
        return {
            "zones": list(self.zones),
            "popular_size": self.popular_size,
            "search_queries": list(self.search_queries),
            "search_limit": self.search_limit,
            "comment_videos": self.comment_videos,
            "comment_modes": list(self.comment_modes),
            "comment_pages": dict(self.comment_pages),
            "comment_window_days": self.comment_window_days,
            "comment_recent_days": self.comment_recent_days,
            "comment_max_per_video": self.comment_max_per_video,
            "danmaku_videos": self.danmaku_videos,
            "max_danmaku_per_video": self.max_danmaku_per_video,
            "max_requests": self.max_requests,
            "enable_search": self.enable_search,
            "enable_hot_search": self.enable_hot_search,
            "search_recent_days": self.search_recent_days,
        }


def _now_ts() -> int:
    return int(time.time())


def _age_days(ts: int, now: int) -> float:
    if not ts:
        return 999.0
    return max(0.0, (now - ts) / 86400.0)


def _budget_left(source: BiliSource, start_requests: int, plan: CollectionPlan) -> bool:
    return (source.http.requests - start_requests) < plan.max_requests


def _dedupe_videos(videos: list[Video]) -> tuple[list[Video], dict[str, list[str]]]:
    unique: dict[str, Video] = {}
    hits: dict[str, list[str]] = {}
    for video in videos:
        if not video.bvid:
            continue
        hits.setdefault(video.bvid, [])
        if video.source:
            hits[video.bvid].append(video.source)
        existing = unique.get(video.bvid)
        if existing is None:
            unique[video.bvid] = video
            continue
        # 搜索命中比排行榜更贴近「梗」，保留搜索来源，但补全缺失的统计。
        if video.source.startswith("search:") and not existing.source.startswith("search:"):
            video.aid = video.aid or existing.aid
            unique[video.bvid] = video
        else:
            existing.aid = existing.aid or video.aid
            existing.reply = max(existing.reply, video.reply)
            existing.view = max(existing.view, video.view)
            existing.like = max(existing.like, video.like)
            existing.danmaku = max(existing.danmaku, video.danmaku)
    for bvid, video in unique.items():
        sources = list(dict.fromkeys(hits.get(bvid, [])))
        if video.source and video.source not in sources:
            sources.insert(0, video.source)
        video.query = video.query or ""
        hits[bvid] = sources
    return list(unique.values()), hits


def _pick_comment_targets(
    videos: list[Video], plan: CollectionPlan, now: int
) -> list[dict]:
    """选评论目标：搜索到的梗视频占一半，其余按回复率/热度补。"""
    fresh_search = [
        video
        for video in videos
        if video.source.startswith("search:")
        and video.aid
        and _age_days(video.pubdate, now) <= plan.search_recent_days
    ]
    fresh_search.sort(
        key=lambda video: (video.view + video.like * 8, -_age_days(video.pubdate, now)),
        reverse=True,
    )
    search_quota = max(1, int(plan.comment_videos * plan.headroom_ratio)) if plan.enable_search else 0
    chosen: list[Video] = []
    seen: set[str] = set()
    for video in fresh_search[:search_quota]:
        chosen.append(video)
        seen.add(video.bvid)

    rest = [
        video
        for video in videos
        if video.bvid not in seen and video.aid and not video.source.startswith("search:")
    ]
    rest.sort(
        key=lambda video: (
            (video.reply + 1) / max(1, video.view) * 1000,
            video.reply,
            video.view,
        ),
        reverse=True,
    )
    for video in rest:
        if len(chosen) >= plan.comment_videos:
            break
        chosen.append(video)
        seen.add(video.bvid)
    return [
        {
            "bvid": video.bvid,
            "aid": video.aid,
            "title": video.title,
            "zone": video.zone,
            "source": video.source,
            "pubdate": video.pubdate,
            "view": video.view,
            "reply": video.reply,
            "role": "search" if video.source.startswith("search:") else "ranking",
        }
        for video in chosen
    ]


def _pick_danmaku_targets(videos: list[Video], plan: CollectionPlan) -> list[Video]:
    candidates = [video for video in videos if video.bvid]
    # 优先搜索到的近期视频，其次高弹幕密度视频。
    candidates.sort(
        key=lambda video: (
            1 if video.source.startswith("search:") else 0,
            video.danmaku / max(1, video.duration or 1),
            video.danmaku,
        ),
        reverse=True,
    )
    return candidates[: plan.danmaku_videos]


def _select_comments_for_video(
    per_mode: dict[int, list[Comment]],
    plan: CollectionPlan,
    now: int,
) -> list[Comment]:
    limit = plan.comment_max_per_video
    per_mode_cap = max(1, limit // max(1, len(plan.comment_modes)))
    picked: list[Comment] = []
    leftovers: list[Comment] = []
    for mode in plan.comment_modes:
        bucket = per_mode.get(mode, [])
        if mode == 3:
            bucket.sort(key=lambda item: (item.like, item.ctime), reverse=True)
        else:
            bucket.sort(key=lambda item: (item.ctime, item.like), reverse=True)
        picked.extend(bucket[:per_mode_cap])
        leftovers.extend(bucket[per_mode_cap:])
    if len(picked) < limit:
        leftovers.sort(key=lambda item: (item.like, item.ctime), reverse=True)
        picked.extend(leftovers[: limit - len(picked)])
    picked.sort(key=lambda item: (item.like, item.ctime), reverse=True)
    return picked[:limit]


def _comment_doc(comment: Comment) -> Doc:
    return Doc(
        id=comment.id,
        text=comment.text,
        kind="comment",
        bvid=comment.bvid,
        ctime=comment.ctime,
        like=comment.like,
        zone=comment.zone,
        video_title=comment.video_title,
        source="bilibili",
    )


def _danmaku_doc(danmaku: Danmaku) -> Doc:
    return Doc(
        id=danmaku.id,
        text=danmaku.text,
        kind="danmaku",
        bvid=danmaku.bvid,
        ctime=danmaku.ctime,
        zone=danmaku.zone,
        video_title=danmaku.video_title,
        source="bilibili",
    )


def collect_corpus(
    source: BiliSource,
    plan: CollectionPlan | None = None,
    *,
    now: int | None = None,
) -> Corpus:
    plan = plan or CollectionPlan()
    now = now or _now_ts()
    started = time.time()
    start_requests = source.http.requests
    stats: dict[str, Any] = {
        "videos": 0,
        "ranking_videos": 0,
        "popular_videos": 0,
        "search_videos": 0,
        "search_queries": 0,
        "search_fail": 0,
        "hot_search_terms": 0,
        "comments": 0,
        "comments_7d": 0,
        "comments_filtered_old": 0,
        "comment_dupes": 0,
        "comment_requests": 0,
        "danmaku": 0,
        "danmaku_videos": 0,
        "docs": 0,
        "budget_exhausted": False,
        "errors": [],
    }
    videos: list[Video] = []
    budget_exhausted = False

    # ---- 排行榜 ----
    for rid in plan.zones:
        if not _budget_left(source, start_requests, plan):
            budget_exhausted = True
            break
        try:
            videos.extend(source.ranking(rid))
        except Exception as exc:  # 单通道失败不影响整轮
            stats["errors"].append({"source": f"ranking:{rid}", "error": str(exc)[:200]})
    stats["ranking_videos"] = len(videos)

    # ---- 热门 ----
    if _budget_left(source, start_requests, plan):
        try:
            popular = source.popular(plan.popular_size)
            stats["popular_videos"] = len(popular)
            videos.extend(popular)
        except Exception as exc:
            stats["errors"].append({"source": "popular", "error": str(exc)[:200]})

    # ---- 热搜旁证（事件多、梗少，只记录不参与打分）----
    if plan.enable_hot_search and _budget_left(source, start_requests, plan):
        try:
            hot = source.hot_search()
            stats["hot_search_terms"] = len(hot)
            stats["hot_search"] = [
                {"keyword": item["keyword"], "heat": item["heat"]} for item in hot[:30]
            ]
        except Exception as exc:
            stats["errors"].append({"source": "hot_search", "error": str(exc)[:200]})

    # ---- 搜索（当前热梗的主召回通道）----
    if plan.enable_search:
        current_year = datetime.fromtimestamp(now).year
        for raw_query in plan.search_queries:
            query = raw_query.replace("{year}", str(current_year))
            if not _budget_left(source, start_requests, plan):
                budget_exhausted = True
                break
            try:
                found = source.search_videos(query, limit=plan.search_limit)
            except Exception as exc:
                stats["search_fail"] += 1
                stats["errors"].append({"source": f"search:{query}", "error": str(exc)[:200]})
                continue
            if not found:
                stats["search_fail"] += 1
                continue
            stats["search_queries"] += 1
            stats["search_videos"] += len(found)
            videos.extend(found)

    videos, video_sources = _dedupe_videos(videos)
    stats["videos"] = len(videos)
    stats["video_sources"] = {bvid: sources[:6] for bvid, sources in video_sources.items() if len(sources) > 1}

    # ---- 评论 ----
    targets = _pick_comment_targets(videos, plan, now)
    stats["comment_targets"] = len(targets)
    stats["comment_target_roles"] = dict(Counter(item["role"] for item in targets))
    window_seconds = plan.comment_window_days * 86400
    recent_seconds = plan.comment_recent_days * 86400
    comments: list[Comment] = []
    seen_ids: set[str] = set()
    seen_norm: set[str] = set()

    for target in targets:
        if not _budget_left(source, start_requests, plan):
            budget_exhausted = True
            break
        per_mode: dict[int, list[Comment]] = {mode: [] for mode in plan.comment_modes}
        for mode in plan.comment_modes:
            pages = int(plan.comment_pages.get(mode, 1))
            cursor = 0
            for _page in range(pages):
                if not _budget_left(source, start_requests, plan):
                    budget_exhausted = True
                    break
                try:
                    items, cursor, is_end = source.comments(
                        int(target["aid"]),
                        bvid=target["bvid"],
                        mode=mode,
                        cursor=cursor,
                        zone=target["zone"],
                        video_title=target["title"],
                    )
                except Exception as exc:
                    stats["errors"].append(
                        {"source": f"comments:{target['bvid']}", "error": str(exc)[:200]}
                    )
                    break
                stats["comment_requests"] += 1
                for comment in items:
                    text = (comment.text or "").strip()
                    if not text or len(text) > plan.comment_max_chars:
                        continue
                    if comment.ctime <= 0 or (now - comment.ctime) > window_seconds:
                        stats["comments_filtered_old"] += 1
                        continue
                    if comment.id in seen_ids:
                        stats["comment_dupes"] += 1
                        continue
                    norm = normalize_surface(text)
                    if norm and norm in seen_norm:
                        stats["comment_dupes"] += 1
                        continue
                    seen_ids.add(comment.id)
                    if norm:
                        seen_norm.add(norm)
                    per_mode[mode].append(comment)
                if cursor == 0:
                    break

        picked = _select_comments_for_video(per_mode, plan, now)
        stats["comments"] += len(picked)
        stats["comments_7d"] += sum(
            1 for item in picked if (now - item.ctime) <= recent_seconds
        )
        comments.extend(picked)

    # ---- 弹幕 ----
    danmaku_list: list[Danmaku] = []
    if plan.danmaku_videos > 0:
        danmaku_targets = _pick_danmaku_targets(videos, plan)
        rng = random.Random(20261114)
        for video in danmaku_targets:
            if not _budget_left(source, start_requests, plan):
                budget_exhausted = True
                break
            cid = video.cid
            if not cid:
                try:
                    cid = source.cid_for(video.bvid)
                except Exception:
                    cid = None
                video.cid = cid
            if not cid:
                continue
            try:
                xml = source.danmaku(int(cid))
            except Exception as exc:
                stats["errors"].append(
                    {"source": f"danmaku:{video.bvid}", "error": str(exc)[:200]}
                )
                continue
            parsed = parse_danmaku_xml(
                xml, bvid=video.bvid, zone=video.zone, video_title=video.title
            )
            parsed = [item for item in parsed if 2 <= len(item.text) <= 40]
            if len(parsed) > plan.max_danmaku_per_video:
                parsed = rng.sample(parsed, plan.max_danmaku_per_video)
            stats["danmaku"] += len(parsed)
            stats["danmaku_videos"] += 1
            danmaku_list.extend(parsed)

    # ---- 文档 ----
    docs: list[Doc] = []
    for comment in comments:
        docs.append(_comment_doc(comment))
    for item in danmaku_list:
        docs.append(_danmaku_doc(item))
    if plan.include_titles:
        for video in videos:
            if video.title:
                docs.append(
                    Doc(
                        id=f"{video.bvid}:title",
                        text=video.title,
                        kind="title",
                        bvid=video.bvid,
                        ctime=video.pubdate,
                        zone=video.zone,
                        video_title=video.title,
                        source=video.source,
                    )
                )
            if video.desc:
                docs.append(
                    Doc(
                        id=f"{video.bvid}:desc",
                        text=video.desc,
                        kind="desc",
                        bvid=video.bvid,
                        ctime=video.pubdate,
                        zone=video.zone,
                        video_title=video.title,
                        source=video.source,
                    )
                )
    stats["docs"] = len(docs)
    stats["comment_evidence_ready"] = stats["comments"] >= 200
    stats["elapsed"] = round(time.time() - started, 1)
    stats["requests"] = source.http.requests - start_requests
    stats["cache_hits"] = source.http.cache_hits
    stats["endpoint_counts"] = dict(source.http.endpoint_counts)
    stats["budget_exhausted"] = budget_exhausted
    stats["search_failures"] = dict(source.search_failures)
    stats["source_errors"] = list(source.errors[-30:])

    return Corpus(
        videos=videos,
        comments=comments,
        danmaku=danmaku_list,
        docs=docs,
        stats=stats,
        generated_at=iso(now),
        plan=plan.as_dict(),
    )


def collect_comments(
    targets: list[dict],
    client: BiliSource,
    *,
    window_days: int = 30,
    max_per_video: int = 40,
    now: int | None = None,
) -> list[Comment]:
    """交接单要求的精简接口：只采评论，返回 Comment 列表。

    ``targets`` 每项至少含 aid/bvid；可选 zone/title。
    """
    now = now or _now_ts()
    plan = CollectionPlan(
        comment_videos=len(targets),
        comment_window_days=window_days,
        comment_max_per_video=max_per_video,
        danmaku_videos=0,
        enable_search=False,
        enable_hot_search=False,
        include_titles=False,
    )
    result: list[Comment] = []
    window_seconds = window_days * 86400
    seen: set[str] = set()
    for target in targets:
        aid = int(target.get("aid") or 0)
        bvid = str(target.get("bvid") or "")
        if not aid:
            continue
        for mode in plan.comment_modes:
            pages = int(plan.comment_pages.get(mode, 1))
            cursor = 0
            for _page in range(pages):
                items, cursor, _is_end = client.comments(
                    aid,
                    bvid=bvid,
                    mode=mode,
                    cursor=cursor,
                    zone=str(target.get("zone") or ""),
                    video_title=str(target.get("title") or ""),
                )
                for item in items:
                    if item.ctime <= 0 or (now - item.ctime) > window_seconds:
                        continue
                    if item.id in seen:
                        continue
                    seen.add(item.id)
                    result.append(item)
                if cursor == 0:
                    break
    return result
