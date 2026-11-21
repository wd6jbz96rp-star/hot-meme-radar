"""Phase C：15 分钟窗口评论/弹幕采样与计数（比例计数输入）。

设计依据 ``docs/pm/REARCHITECTURE.md`` §3.3 / §5.4 / §7 Phase C：

- 15 分钟计数：评论只用 ``mode=2`` 时间序，从最新往前翻到 ``window_start``，
  只保留 ``ctime ∈ [window_start, window_end]``；弹幕解析 ``ctime`` + ``dmid``，
  只保留窗口内、按 ``dmid`` 去重、长度 2–40。
- 固定目标条数：评论 1000 / 弹幕 **600**（PM 复核：B站 XML 封顶，15 分钟弹幕量
  远低于 2000，按真实数据下调）；低于 60%（评论 600）/ 50%（弹幕 300）标
  ``degraded``。超过时按每视频配额 + 轮转（round-robin）截断，固定总量、无随机偏置。
- 评论文本去重默认 **per_video**（同一视频内归一化文本去重；跨视频同文本不算重复，
  避免误伤扩散计数）；``global`` 保留为可选口径。
- 30 天证据：评论 ``mode=3`` 热评 + ``mode=2`` 时间序（每视频 ≤40），弹幕按
  30 天过滤（每视频 ≤200）；证据只用于可信度，不参与比例，字段与窗口样本分开。

本模块不接 worker、不切线上、不写 pool1/pool2/audit；``--no-db`` 时纯内存。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .bili import parse_danmaku_xml
from .models import (
    Comment,
    Danmaku,
    VideoRouteResult,
    WindowComment,
    WindowDanmaku,
    WindowSample,
    WindowTarget,
    iso,
)
from .text import normalize_surface

DEFAULT_WINDOW_MINUTES = 15
COMMENT_TARGETS = 45
DANMAKU_TARGETS = 10
COMMENT_GOAL = 1000
DANMAKU_GOAL = 600
COMMENT_MIN = 600
DANMAKU_MIN = 300
EVIDENCE_DAYS = 30
EVIDENCE_COMMENT_PER_VIDEO = 40
EVIDENCE_DANMAKU_PER_VIDEO = 200
COMMENT_MAX_CHARS = 300
DANMAKU_MIN_CHARS = 2
DANMAKU_MAX_CHARS = 40
DEFAULT_COMMENT_MAX_PAGES = 10
DEFAULT_EVIDENCE_COMMENT_PAGES = 2
DEFAULT_EVIDENCE_MODE3_PAGES = 1
DEFAULT_MAX_REQUESTS = 700
#: 异常窗口阈值：round 窗口跨度超过 20 分钟（停机/漏跑）时计数窗口 clamp 到 15 分钟
WINDOW_CLAMP_THRESHOLD_MINUTES = 20


# ---------------------------------------------------------------- 计划参数
@dataclass
class WindowSamplePlan:
    """窗口采样参数（全部可在 CLI 覆盖；默认值来自设计文档）。"""

    window_minutes: int = DEFAULT_WINDOW_MINUTES
    comment_targets: int = COMMENT_TARGETS
    danmaku_targets: int = DANMAKU_TARGETS
    comment_goal: int = COMMENT_GOAL
    danmaku_goal: int = DANMAKU_GOAL
    comment_min: int = COMMENT_MIN
    #: 弹幕目标 600 / 下限 300（真实 XML 封顶后的实测口径，PM 复核调整）
    danmaku_min: int = DANMAKU_MIN
    #: 15 分钟评论单视频最多翻页数（防慢视频把预算吃光）
    comment_max_pages: int = DEFAULT_COMMENT_MAX_PAGES
    comment_page_size: int = 20
    comment_max_chars: int = COMMENT_MAX_CHARS
    danmaku_min_chars: int = DANMAKU_MIN_CHARS
    danmaku_max_chars: int = DANMAKU_MAX_CHARS
    #: 30 天证据（不参与比例）；``--no-evidence`` 关闭
    evidence: bool = True
    evidence_days: int = EVIDENCE_DAYS
    evidence_comment_per_video: int = EVIDENCE_COMMENT_PER_VIDEO
    evidence_comment_pages: int = DEFAULT_EVIDENCE_COMMENT_PAGES
    evidence_mode3_pages: int = DEFAULT_EVIDENCE_MODE3_PAGES
    evidence_page_size: int = 20
    evidence_danmaku_per_video: int = EVIDENCE_DANMAKU_PER_VIDEO
    #: 整轮 HTTP 请求预算（含 view/aid 解析）；超预算标 degraded
    max_requests: int = DEFAULT_MAX_REQUESTS
    #: 评论文本去重范围：per_video（默认，仅同视频；跨视频同文本保留）/ global
    text_dedupe_scope: str = "per_video"

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_minutes": self.window_minutes,
            "comment_targets": self.comment_targets,
            "danmaku_targets": self.danmaku_targets,
            "comment_goal": self.comment_goal,
            "danmaku_goal": self.danmaku_goal,
            "comment_min": self.comment_min,
            "danmaku_min": self.danmaku_min,
            "comment_max_pages": self.comment_max_pages,
            "comment_page_size": self.comment_page_size,
            "comment_max_chars": self.comment_max_chars,
            "danmaku_min_chars": self.danmaku_min_chars,
            "danmaku_max_chars": self.danmaku_max_chars,
            "evidence": self.evidence,
            "evidence_days": self.evidence_days,
            "evidence_comment_per_video": self.evidence_comment_per_video,
            "evidence_comment_pages": self.evidence_comment_pages,
            "evidence_mode3_pages": self.evidence_mode3_pages,
            "evidence_danmaku_per_video": self.evidence_danmaku_per_video,
            "max_requests": self.max_requests,
            "text_dedupe_scope": self.text_dedupe_scope,
        }

    @property
    def window_seconds(self) -> int:
        return max(1, int(self.window_minutes)) * 60


# ---------------------------------------------------------------- 小工具
def per_video_cap(goal: int, video_count: int) -> int:
    """每视频配额：固定总量下各视频相同上限（ceil），保证容量 ≥ 目标。"""
    if video_count <= 0:
        return 0
    return max(1, math.ceil(max(1, int(goal)) / int(video_count)))


def round_robin_truncate(groups: Sequence[Sequence[Any]], limit: int) -> list[Any]:
    """按视频轮转取前 ``limit`` 条：各视频轮流贡献，固定总量且不偏向某个视频。"""
    if limit <= 0:
        return []
    selected: list[Any] = []
    positions = [0] * len(groups)
    while len(selected) < limit:
        moved = False
        for index, group in enumerate(groups):
            if positions[index] >= len(group):
                continue
            selected.append(group[positions[index]])
            positions[index] += 1
            moved = True
            if len(selected) >= limit:
                break
        if not moved:
            break
    return selected


def _norm_text(text: str) -> str:
    """评论去重用的归一化文本；纯表情/标点归一化为空时退回原文。"""
    norm = normalize_surface(text or "")
    return norm or (text or "").strip()


def _clean_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _request_count(source: Any) -> int:
    http = getattr(source, "http", None)
    return int(getattr(http, "requests", 0) or 0)


class _Budget:
    """整轮请求预算：超过后停止采样并标记 degraded。"""

    def __init__(self, source: Any, limit: int):
        self.source = source
        self.start = _request_count(source)
        self.limit = max(1, int(limit or 1))
        self.exhausted = False

    def ok(self) -> bool:
        if _request_count(self.source) - self.start >= self.limit:
            self.exhausted = True
            return False
        return True

    @property
    def used(self) -> int:
        return max(0, _request_count(self.source) - self.start)


def _record_error(errors: list[dict[str, Any]], stage: str, bvid: str, exc: Any) -> None:
    errors.append(
        {
            "stage": stage,
            "bvid": bvid,
            "error": str(exc)[:240],
        }
    )


def _resolve_aid(
    source: Any,
    target: WindowTarget,
    aid_cache: dict[str, int],
    stats: dict[str, Any],
    errors: list[dict[str, Any]],
) -> int:
    """评论接口需要 aid：目标自带优先，否则 ``/x/web-interface/view`` 反查（按 bvid 缓存）。"""
    if target.aid:
        aid_cache[target.bvid] = int(target.aid)
        return int(target.aid)
    cached = aid_cache.get(target.bvid)
    if cached:
        return cached
    view = getattr(source, "view", None)
    if view is None:
        return 0
    try:
        video = view(target.bvid)
    except Exception as exc:  # noqa: BLE001 - 单个视频失败不中断整轮
        stats["aid_view_failures"] = stats.get("aid_view_failures", 0) + 1
        _record_error(errors, "view", target.bvid, exc)
        return 0
    aid = int(getattr(video, "aid", 0) or 0) if video is not None else 0
    if not aid:
        stats["aid_view_failures"] = stats.get("aid_view_failures", 0) + 1
        return 0
    stats["aid_view_resolved"] = stats.get("aid_view_resolved", 0) + 1
    aid_cache[target.bvid] = aid
    target.aid = aid
    if target.cid is None and getattr(video, "cid", None):
        target.cid = int(video.cid)
    return aid


# ---------------------------------------------------------------- 15 分钟评论
def sample_window_comments(
    source: Any,
    targets: Sequence[WindowTarget],
    window_start: int,
    window_end: int,
    plan: WindowSamplePlan,
    *,
    stats: dict[str, Any],
    errors: list[dict[str, Any]],
    budget: _Budget,
    aid_cache: dict[str, int],
) -> list[WindowComment]:
    """mode=2 时间序翻页；只计数窗口内新评论（id + 归一化文本去重）。"""
    cap = per_video_cap(plan.comment_goal, len(targets))
    stats["comment_quota_per_video"] = cap
    groups: list[list[WindowComment]] = []
    seen_ids: set[str] = set()
    global_norm: set[str] = set()
    text_dedupe_global = plan.text_dedupe_scope != "per_video"

    for target in targets:
        local: list[WindowComment] = []
        local_seen_ids: set[str] = set()
        local_norm: set[str] = set()
        seen_norm = global_norm if text_dedupe_global else local_norm
        aid = _resolve_aid(source, target, aid_cache, stats, errors)
        if not aid:
            stats["comment_videos_no_aid"] = stats.get("comment_videos_no_aid", 0) + 1
            groups.append(local)
            continue
        cursor = 0
        pages = 0
        stopped_reason = "pages_exhausted"
        while pages < max(1, plan.comment_max_pages):
            if not budget.ok():
                stopped_reason = "budget"
                break
            try:
                items, next_cursor, is_end = source.comments(
                    aid,
                    bvid=target.bvid,
                    mode=2,
                    cursor=cursor,
                    page_size=plan.comment_page_size,
                    zone=target.zone,
                    video_title=target.title,
                    include_top=False,
                )
            except Exception as exc:  # noqa: BLE001
                stats["comment_errors"] = stats.get("comment_errors", 0) + 1
                _record_error(errors, "comments:mode2", target.bvid, exc)
                stopped_reason = "error"
                break
            pages += 1
            stats["comment_requests"] = stats.get("comment_requests", 0) + 1
            if not items:
                stopped_reason = "empty_page"
                break
            page_ctimes = [item.ctime for item in items if int(item.ctime or 0) > 0]
            for item in items:
                text = _clean_text(item.text)
                if not text:
                    continue
                if len(text) > plan.comment_max_chars:
                    stats["comment_too_long"] = stats.get("comment_too_long", 0) + 1
                    continue
                ctime = int(item.ctime or 0)
                if ctime <= 0 or ctime < window_start or ctime > window_end:
                    stats["comment_out_of_window"] = stats.get("comment_out_of_window", 0) + 1
                    continue
                if item.id in local_seen_ids or item.id in seen_ids:
                    stats["comment_dupes_id"] = stats.get("comment_dupes_id", 0) + 1
                    continue
                norm = _norm_text(text)
                if norm in seen_norm:
                    stats["comment_dupes_text"] = stats.get("comment_dupes_text", 0) + 1
                    continue
                local_seen_ids.add(item.id)
                seen_ids.add(item.id)
                seen_norm.add(norm)
                local.append(
                    WindowComment(
                        id=item.id,
                        bvid=target.bvid,
                        text=text,
                        ctime=ctime,
                        like=int(item.like or 0),
                        zone=target.zone,
                        video_title=target.title,
                        mode=2,
                        evidence=False,
                    )
                )
                if len(local) >= cap:
                    break
            if len(local) >= cap:
                stopped_reason = "quota"
                break
            # mode=2 为时间倒序：翻到窗口起点之前即停止，不再消耗请求。
            if page_ctimes and min(page_ctimes) < window_start:
                stopped_reason = "reached_window_start"
                break
            if is_end or not next_cursor or int(next_cursor) == int(cursor):
                stopped_reason = "cursor_end"
                break
            cursor = int(next_cursor)
        stats.setdefault("comment_pages", {})[target.bvid] = pages
        stats.setdefault("comment_stop", {})[target.bvid] = stopped_reason
        if len(local) >= cap:
            stats["comment_quota_hit_videos"] = (
                stats.get("comment_quota_hit_videos", 0) + 1
            )
        groups.append(local)

    selected = round_robin_truncate(groups, plan.comment_goal)
    stats["comment_collected_raw"] = sum(len(group) for group in groups)
    stats["comment_videos"] = sum(1 for group in groups if group)
    stats["comment_truncated"] = len(selected) < sum(len(group) for group in groups)
    selected = sorted(selected, key=lambda item: (item.bvid, -item.ctime, item.id))
    return selected


# ---------------------------------------------------------------- 15 分钟弹幕
def sample_window_danmaku(
    source: Any,
    targets: Sequence[WindowTarget],
    window_start: int,
    window_end: int,
    plan: WindowSamplePlan,
    *,
    stats: dict[str, Any],
    errors: list[dict[str, Any]],
    budget: _Budget,
    danmaku_cache: dict[int, list[Danmaku]],
) -> list[WindowDanmaku]:
    """拉弹幕 XML，按 ctime 过滤窗口、按 dmid 去重、长度 2–40。"""
    cap = per_video_cap(plan.danmaku_goal, len(targets))
    stats["danmaku_quota_per_video"] = cap
    groups: list[list[WindowDanmaku]] = []
    seen_keys: set[tuple[str, str]] = set()
    for target in targets:
        local: list[WindowDanmaku] = []
        cid = int(target.cid or 0)
        if not cid:
            cid_for = getattr(source, "cid_for", None)
            if cid_for is not None:
                try:
                    cid = int(cid_for(target.bvid) or 0)
                except Exception as exc:  # noqa: BLE001
                    _record_error(errors, "cid_for", target.bvid, exc)
                    cid = 0
            if cid:
                target.cid = cid
                stats["cid_resolved"] = stats.get("cid_resolved", 0) + 1
        if not cid:
            stats["danmaku_videos_no_cid"] = stats.get("danmaku_videos_no_cid", 0) + 1
            groups.append(local)
            continue
        if cid not in danmaku_cache:
            parsed_items: list[Danmaku] | None = None
            if budget.ok():
                try:
                    xml = source.danmaku(cid)
                except Exception as exc:  # noqa: BLE001
                    stats["danmaku_errors"] = stats.get("danmaku_errors", 0) + 1
                    _record_error(errors, "danmaku", target.bvid, exc)
                    xml = ""
                if xml:
                    parsed_items = parse_danmaku_xml(
                        xml,
                        bvid=target.bvid,
                        zone=target.zone,
                        video_title=target.title,
                    )
                danmaku_cache[cid] = parsed_items or []
            else:
                groups.append(local)
                continue
        else:
            stats["danmaku_cache_hits"] = stats.get("danmaku_cache_hits", 0) + 1
        parsed = danmaku_cache.get(cid) or []
        stats["danmaku_videos_fetched"] = stats.get("danmaku_videos_fetched", 0) + 1
        for item in parsed:
            text = _clean_text(item.text)
            if not (plan.danmaku_min_chars <= len(text) <= plan.danmaku_max_chars):
                stats["danmaku_bad_length"] = stats.get("danmaku_bad_length", 0) + 1
                continue
            ctime = int(item.ctime or 0)
            if ctime <= 0 or ctime < window_start or ctime > window_end:
                stats["danmaku_out_of_window"] = stats.get("danmaku_out_of_window", 0) + 1
                continue
            dmid = str(getattr(item, "dmid", "") or "")
            if not dmid:
                stats["danmaku_missing_dmid"] = stats.get("danmaku_missing_dmid", 0) + 1
                key = ("id", item.id)
            else:
                key = ("dmid", dmid)
            if key in seen_keys:
                stats["danmaku_dupes"] = stats.get("danmaku_dupes", 0) + 1
                continue
            seen_keys.add(key)
            local.append(
                WindowDanmaku(
                    id=item.id,
                    bvid=target.bvid,
                    text=text,
                    ctime=ctime,
                    dmid=dmid,
                    zone=target.zone,
                    video_title=target.title,
                    progress=float(item.progress or 0.0),
                    evidence=False,
                )
            )
            if len(local) >= cap:
                break
        groups.append(local)

    selected = round_robin_truncate(groups, plan.danmaku_goal)
    stats["danmaku_collected_raw"] = sum(len(group) for group in groups)
    stats["danmaku_videos"] = sum(1 for group in groups if group)
    stats["danmaku_truncated"] = len(selected) < sum(len(group) for group in groups)
    selected = sorted(
        selected, key=lambda item: (item.bvid, -item.ctime, item.dmid or item.id)
    )
    return selected


# ---------------------------------------------------------------- 30 天证据
def sample_evidence_comments(
    source: Any,
    targets: Sequence[WindowTarget],
    window_end: int,
    plan: WindowSamplePlan,
    *,
    stats: dict[str, Any],
    errors: list[dict[str, Any]],
    budget: _Budget,
    aid_cache: dict[str, int],
) -> list[WindowComment]:
    """30 天证据：mode=3 热评 + mode=2 时间序，每视频 ≤40 条（不参与比例）。"""
    since = int(window_end) - int(plan.evidence_days) * 86400
    per_video = max(1, int(plan.evidence_comment_per_video))
    per_mode_cap = max(1, per_video // 2)
    seen_ids: set[str] = set()
    evidence: list[WindowComment] = []
    for target in targets:
        if not budget.ok():
            break
        aid = _resolve_aid(source, target, aid_cache, stats, errors)
        if not aid:
            continue
        buckets: dict[int, list[Comment]] = {}
        for mode, max_pages in (
            (3, max(1, plan.evidence_mode3_pages)),
            (2, max(1, plan.evidence_comment_pages)),
        ):
            items_all: list[Comment] = []
            cursor = 0
            for _page in range(max_pages):
                if not budget.ok():
                    break
                try:
                    items, next_cursor, is_end = source.comments(
                        aid,
                        bvid=target.bvid,
                        mode=mode,
                        cursor=cursor,
                        page_size=plan.evidence_page_size,
                        zone=target.zone,
                        video_title=target.title,
                        include_top=(mode != 2),
                    )
                except Exception as exc:  # noqa: BLE001
                    stats["evidence_comment_errors"] = (
                        stats.get("evidence_comment_errors", 0) + 1
                    )
                    _record_error(errors, f"evidence:mode{mode}", target.bvid, exc)
                    break
                stats["evidence_comment_requests"] = (
                    stats.get("evidence_comment_requests", 0) + 1
                )
                if not items:
                    break
                page_ctimes = [item.ctime for item in items if int(item.ctime or 0) > 0]
                for item in items:
                    ctime = int(item.ctime or 0)
                    if ctime <= 0 or ctime < since or ctime > window_end:
                        stats["evidence_comment_out_of_window"] = (
                            stats.get("evidence_comment_out_of_window", 0) + 1
                        )
                        continue
                    text = _clean_text(item.text)
                    if not text or len(text) > plan.comment_max_chars:
                        continue
                    if item.id in seen_ids:
                        stats["evidence_comment_dupes"] = (
                            stats.get("evidence_comment_dupes", 0) + 1
                        )
                        continue
                    seen_ids.add(item.id)
                    items_all.append(item)
                if mode == 2 and page_ctimes and min(page_ctimes) < since:
                    break
                if is_end or not next_cursor or int(next_cursor) == int(cursor):
                    break
                cursor = int(next_cursor)
            buckets[mode] = items_all

        hot = sorted(buckets.get(3, []), key=lambda c: (int(c.like or 0), c.ctime), reverse=True)
        latest = sorted(buckets.get(2, []), key=lambda c: c.ctime, reverse=True)
        picked: list[Comment] = hot[:per_mode_cap] + latest[:per_mode_cap]
        if len(picked) < per_video:
            leftovers = hot[per_mode_cap:] + latest[per_mode_cap:]
            leftovers.sort(key=lambda c: (int(c.like or 0), c.ctime), reverse=True)
            picked.extend(leftovers[: per_video - len(picked)])
        for item in picked[:per_video]:
            evidence.append(
                WindowComment(
                    id=item.id,
                    bvid=target.bvid,
                    text=_clean_text(item.text),
                    ctime=int(item.ctime or 0),
                    like=int(item.like or 0),
                    zone=target.zone,
                    video_title=target.title,
                    mode=int(item.mode or 0),
                    evidence=True,
                )
            )
    stats["evidence_comments"] = len(evidence)
    stats["evidence_comment_videos"] = len({item.bvid for item in evidence})
    evidence.sort(key=lambda item: (item.bvid, -item.ctime, item.id))
    return evidence


def sample_evidence_danmaku(
    targets: Sequence[WindowTarget],
    window_end: int,
    plan: WindowSamplePlan,
    danmaku_cache: dict[int, list[Danmaku]],
    *,
    stats: dict[str, Any],
) -> list[WindowDanmaku]:
    """30 天证据弹幕：复用已拉取 XML，按 30 天过滤、每视频 ≤200。"""
    since = int(window_end) - int(plan.evidence_days) * 86400
    per_video = max(1, int(plan.evidence_danmaku_per_video))
    seen_keys: set[tuple[str, str]] = set()
    evidence: list[WindowDanmaku] = []
    for target in targets:
        parsed = danmaku_cache.get(int(target.cid or 0)) or []
        if not parsed:
            continue
        picked = 0
        for item in parsed:
            if picked >= per_video:
                break
            text = _clean_text(item.text)
            if not (plan.danmaku_min_chars <= len(text) <= plan.danmaku_max_chars):
                continue
            ctime = int(item.ctime or 0)
            if ctime <= 0 or ctime < since or ctime > window_end:
                continue
            dmid = str(getattr(item, "dmid", "") or "")
            key = ("dmid", dmid) if dmid else ("id", item.id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            evidence.append(
                WindowDanmaku(
                    id=item.id,
                    bvid=target.bvid,
                    text=text,
                    ctime=ctime,
                    dmid=dmid,
                    zone=target.zone,
                    video_title=target.title,
                    progress=float(item.progress or 0.0),
                    evidence=True,
                )
            )
            picked += 1
    stats["evidence_danmaku"] = len(evidence)
    stats["evidence_danmaku_videos"] = len({item.bvid for item in evidence})
    evidence.sort(key=lambda item: (item.bvid, -item.ctime, item.dmid or item.id))
    return evidence


# ---------------------------------------------------------------- 目标解析
def targets_from_video_route(
    payload: VideoRouteResult | dict[str, Any],
    plan: WindowSamplePlan | None = None,
) -> tuple[list[WindowTarget], list[WindowTarget]]:
    """从 Phase B 的 ``VideoRouteResult``（对象或 JSON dict）还原 45/10 目标。"""
    plan = plan or WindowSamplePlan()
    data = payload.to_dict() if isinstance(payload, VideoRouteResult) else dict(payload or {})
    metrics = {
        str(item.get("bvid") or ""): item
        for item in (data.get("metrics") or [])
        if isinstance(item, dict) and item.get("bvid")
    }

    def build(bvids: Iterable[Any], limit: int) -> list[WindowTarget]:
        result: list[WindowTarget] = []
        for bvid in list(bvids)[: max(0, int(limit))]:
            bvid = str(bvid or "")
            if not bvid:
                continue
            metric = metrics.get(bvid) or {}
            result.append(
                WindowTarget(
                    bvid=bvid,
                    aid=int(metric.get("aid") or 0),
                    cid=metric.get("cid"),
                    title=str(metric.get("title") or ""),
                    zone=str(metric.get("zone") or ""),
                    reply=int(metric.get("reply") or 0),
                    danmaku=int(metric.get("danmaku") or 0),
                    hot_score=float(metric.get("hot_score") or 0.0),
                    source="video_route",
                )
            )
        return result

    comment_targets = build(data.get("selected_comment") or [], plan.comment_targets)
    danmaku_targets = build(data.get("selected_danmaku") or [], plan.danmaku_targets)
    return comment_targets, danmaku_targets


def targets_from_payload(payload: dict[str, Any]) -> tuple[list[WindowTarget], list[WindowTarget]]:
    """解析 ``--targets-json``：支持 VideoRouteResult 或 {comment_targets, danmaku_targets}。"""
    if not isinstance(payload, dict):
        raise ValueError("targets JSON 必须是对象")
    if "comment_targets" in payload or "danmaku_targets" in payload:
        def build(items: Any) -> list[WindowTarget]:
            result: list[WindowTarget] = []
            for item in items or []:
                if isinstance(item, str):
                    item = {"bvid": item}
                if not isinstance(item, dict) or not item.get("bvid"):
                    continue
                result.append(
                    WindowTarget(
                        bvid=str(item["bvid"]),
                        aid=int(item.get("aid") or 0),
                        cid=item.get("cid"),
                        title=str(item.get("title") or ""),
                        zone=str(item.get("zone") or ""),
                        reply=int(item.get("reply") or 0),
                        danmaku=int(item.get("danmaku") or 0),
                        hot_score=float(item.get("hot_score") or 0.0),
                        source=str(item.get("source") or "targets_json"),
                    )
                )
            return result

        return build(payload.get("comment_targets")), build(payload.get("danmaku_targets"))
    return targets_from_video_route(payload)


# ---------------------------------------------------------------- Postgres
def _state_module() -> Any:
    """延迟 import：纯内存模式不要求 psycopg 可用。"""
    from . import state as state_module

    return state_module


def load_round(
    *,
    round_id: int | None = None,
    url: str | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """读最近一轮（或指定 id）的 round 行；只读。"""
    if round_id is not None:
        sql = (
            "SELECT id, EXTRACT(EPOCH FROM started_at)::bigint, "
            "EXTRACT(EPOCH FROM window_start)::bigint, "
            "EXTRACT(EPOCH FROM window_end)::bigint, status, "
            "COALESCE(stats, '{}'::jsonb) FROM round WHERE id = %s"
        )
        params: tuple[Any, ...] = (int(round_id),)
    else:
        sql = (
            "SELECT id, EXTRACT(EPOCH FROM started_at)::bigint, "
            "EXTRACT(EPOCH FROM window_start)::bigint, "
            "EXTRACT(EPOCH FROM window_end)::bigint, status, "
            "COALESCE(stats, '{}'::jsonb) FROM round "
            "ORDER BY started_at DESC, id DESC LIMIT 1"
        )
        params = ()

    def read(connection: Any) -> dict[str, Any] | None:
        row = connection.execute(sql, params).fetchone()
        if not row:
            return None
        stats = row[5]
        if isinstance(stats, (str, bytes, bytearray)):
            import json as _json

            try:
                stats = _json.loads(stats)
            except ValueError:
                stats = {}
        return {
            "round_id": int(row[0]),
            "started_at": int(row[1] or 0),
            "window_start": int(row[2] or 0),
            "window_end": int(row[3] or 0),
            "status": str(row[4] or ""),
            "stats": stats if isinstance(stats, dict) else {},
        }

    if conn is not None:
        return read(conn)
    state = _state_module()
    with state.connection(url, autocommit=True) as connection:
        return read(connection)


def load_round_targets(
    round_id: int,
    *,
    plan: WindowSamplePlan,
    url: str | None = None,
    conn: Any = None,
) -> tuple[list[WindowTarget], list[WindowTarget], dict[str, Any]]:
    """读本轮 ``video_metric``：优先 selected 标记，缺失时按 hot_score 取前 45/10 兜底。"""
    sql = """
        SELECT bvid, cid, title, zone, reply, danmaku,
               COALESCE(hot_score, 0), selected_comment, selected_danmaku
          FROM video_metric
         WHERE round_id = %s
         ORDER BY COALESCE(hot_score, 0) DESC, bvid
    """

    def read(connection: Any) -> list[Any]:
        return connection.execute(sql, (int(round_id),)).fetchall()

    if conn is not None:
        rows = read(conn)
    else:
        state = _state_module()
        with state.connection(url, autocommit=True) as connection:
            rows = read(connection)

    def build(row: Any, source: str) -> WindowTarget:
        return WindowTarget(
            bvid=str(row[0]),
            cid=int(row[1]) if row[1] is not None else None,
            title=str(row[2] or ""),
            zone=str(row[3] or ""),
            reply=int(row[4] or 0),
            danmaku=int(row[5] or 0),
            hot_score=float(row[6] or 0.0),
            source=source,
        )

    selected_comment = [build(row, "selected") for row in rows if row[7]]
    selected_danmaku = [build(row, "selected") for row in rows if row[8]]
    stats: dict[str, Any] = {
        "round_metrics": len(rows),
        "selected_comment_rows": len(selected_comment),
        "selected_danmaku_rows": len(selected_danmaku),
    }
    fallback_comment = False
    fallback_danmaku = False
    if not selected_comment:
        selected_comment = [build(row, "fallback_hot") for row in rows][
            : plan.comment_targets
        ]
        fallback_comment = True
    if not selected_danmaku:
        selected_danmaku = [build(row, "fallback_hot") for row in rows][
            : plan.danmaku_targets
        ]
        fallback_danmaku = True
    selected_comment = selected_comment[: plan.comment_targets]
    selected_danmaku = selected_danmaku[: plan.danmaku_targets]
    stats["comment_fallback_hot"] = fallback_comment
    stats["danmaku_fallback_hot"] = fallback_danmaku
    return selected_comment, selected_danmaku, stats


def persist_sample(
    sample: WindowSample,
    *,
    conn: Any = None,
    url: str | None = None,
) -> int | None:
    """把采样结果写回本轮 ``round``（只更新计数/状态/stats，不动 pool/audit/报告）。"""
    if sample.round_id is None:
        return None
    payload = {
        "phase_c": sample.stats,
        "phase_c_status": sample.status,
        "phase_c_generated_at": sample.generated_at,
        "phase_c_window": {
            "window_start": sample.window_start,
            "window_end": sample.window_end,
        },
    }
    import json as _json

    stats_json = _json.dumps(payload, ensure_ascii=False, default=str)
    reason = sample.stats.get("degraded_reason") or None
    sql = """
        UPDATE round SET
            comments_sampled = %s,
            danmaku_sampled = %s,
            status = CASE
                WHEN %s = 'degraded' AND status <> 'failed' THEN 'degraded'
                ELSE status
            END,
            degraded_reason = CASE
                WHEN %s::text IS NULL THEN degraded_reason
                WHEN degraded_reason IS NULL OR degraded_reason = '' THEN %s::text
                ELSE degraded_reason || ';' || %s::text
            END,
            stats = COALESCE(stats, '{}'::jsonb) || %s::jsonb
        WHERE id = %s
        RETURNING id
    """
    params = (
        len(sample.comments),
        len(sample.danmaku),
        sample.status,
        reason,
        reason,
        reason,
        stats_json,
        int(sample.round_id),
    )

    def write(connection: Any) -> int | None:
        row = connection.execute(sql, params).fetchone()
        return int(row[0]) if row else None

    if conn is not None:
        return write(conn)
    state = _state_module()
    with state.transaction(url) as connection:
        return write(connection)


# ---------------------------------------------------------------- 一轮编排
def _determine_status(
    plan: WindowSamplePlan,
    comment_count: int,
    danmaku_count: int,
    *,
    comment_target_count: int,
    danmaku_target_count: int,
    budget_exhausted: bool,
    window_clamped: bool = False,
    no_targets: bool = False,
) -> tuple[str, str]:
    reasons: list[str] = []
    if no_targets or (comment_target_count == 0 and danmaku_target_count == 0):
        reasons.append("no targets")
    if window_clamped:
        reasons.append("window_clamped")
    if comment_count < plan.comment_min:
        reasons.append(f"comments={comment_count}<{plan.comment_min}")
    if danmaku_count < plan.danmaku_min:
        reasons.append(f"danmaku={danmaku_count}<{plan.danmaku_min}")
    if budget_exhausted:
        reasons.append("request budget exhausted")
    if reasons:
        return "degraded", ";".join(reasons)
    return "ok", ""


def run_window_sample(
    source: Any | None = None,
    plan: WindowSamplePlan | None = None,
    *,
    now: int | None = None,
    comment_targets: Sequence[WindowTarget] | None = None,
    danmaku_targets: Sequence[WindowTarget] | None = None,
    video_route: VideoRouteResult | dict[str, Any] | None = None,
    window_start: int | None = None,
    window_end: int | None = None,
    use_db: bool = True,
    database_url: str | None = None,
    round_id: int | None = None,
    db_conn: Any = None,
) -> WindowSample:
    """跑一轮 Phase C 窗口采样（不接 worker、不切线上）。

    - ``source`` 缺省为 ``BiliSource``；测试注入 fake；
    - ``video_route`` / ``comment_targets`` / ``danmaku_targets``：显式目标，供测试与
      ``--no-db``；缺省从 Postgres 最近一轮 ``round`` + ``video_metric`` 读取；
    - ``use_db=True`` 但无 ``DATABASE_URL`` 时自动降级为纯内存（不报错）；
    - ``db_conn``：复用已有连接（测试事务回滚）。
    """
    plan = plan or WindowSamplePlan()
    started = int(now if now is not None else time.time())
    wall_started = time.monotonic()
    if source is None:
        from .bili import BiliSource

        source = BiliSource()
    errors: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "window_minutes": plan.window_minutes,
        "plan": plan.as_dict(),
        "use_db": bool(use_db),
        "db_written": False,
        "db_skipped": False,
        "db_error": "",
        "round_loaded": False,
        "target_source": "explicit",
    }
    sample = WindowSample(
        generated_at=iso(started),
        started_at=started,
        comment_targets=[],
        danmaku_targets=[],
    )

    # 1) 数据库读取（round / targets）；无连接串时静默降级为纯内存
    url: str | None = None
    round_row: dict[str, Any] | None = None
    if use_db:
        try:
            url = _state_module().database_url(database_url)
        except Exception as exc:  # noqa: BLE001 - 无 DATABASE_URL 不报错
            stats["db_skipped"] = True
            stats["db_note"] = f"no database url: {str(exc)[:120]}"
            url = None
        if url:
            try:
                round_row = load_round(round_id=round_id, url=url, conn=db_conn)
                if round_row is None:
                    stats["db_note"] = "no round row"
            except Exception as exc:  # noqa: BLE001
                stats["db_error"] = str(exc)[:240]
                _record_error(errors, "db:load_round", "", exc)
                round_row = None
    else:
        stats["db_skipped"] = True

    explicit_targets = comment_targets is not None or danmaku_targets is not None or video_route is not None
    if video_route is not None:
        route_comment, route_danmaku = targets_from_video_route(video_route, plan)
        if comment_targets is None:
            comment_targets = route_comment
        if danmaku_targets is None:
            danmaku_targets = route_danmaku
        route_data = (
            video_route.to_dict() if isinstance(video_route, VideoRouteResult) else video_route
        )
        if window_start is None and int(route_data.get("window_start") or 0) > 0:
            window_start = int(route_data["window_start"])
        if window_end is None and int(route_data.get("window_end") or 0) > 0:
            window_end = int(route_data["window_end"])
    if comment_targets is None or danmaku_targets is None:
        if round_row is not None:
            loaded_comment, loaded_danmaku, target_stats = load_round_targets(
                int(round_row["round_id"]),
                plan=plan,
                url=url,
                conn=db_conn,
            )
            stats.update(target_stats)
            if comment_targets is None:
                comment_targets = loaded_comment
            if danmaku_targets is None:
                danmaku_targets = loaded_danmaku
            stats["target_source"] = "db"
        else:
            if comment_targets is None:
                comment_targets = []
            if danmaku_targets is None:
                danmaku_targets = []
            stats["target_source"] = "none"
    if explicit_targets:
        stats["target_source"] = "explicit"

    # 2) 窗口（§5.4）：显式 > round > 默认 now-15min
    if round_row is not None:
        sample.round_id = int(round_row["round_id"])
        stats["round_loaded"] = True
        stats["round_status_before"] = round_row["status"]
        if window_start is None:
            window_start = int(round_row["window_start"] or 0)
        if window_end is None:
            window_end = int(round_row["window_end"] or 0)
    if not window_start or not window_end or int(window_end) <= int(window_start):
        window_start = started - plan.window_seconds
        window_end = started
        stats["window_source"] = "default"
    else:
        stats["window_source"] = "explicit" if not round_row else "round"
    # 异常窗口 clamp：停机/漏跑导致窗口跨度 > 20 分钟时，计数窗口只取最后 15 分钟；
    # round 原 window_start/window_end 字段不改，degraded_reason 加 window_clamped
    # （Phase D 据此冻结比例加减分）。
    raw_window_seconds = int(window_end) - int(window_start)
    if raw_window_seconds > WINDOW_CLAMP_THRESHOLD_MINUTES * 60:
        window_start = int(window_end) - DEFAULT_WINDOW_MINUTES * 60
        stats["window_clamped"] = True
        stats["window_original_minutes"] = round(raw_window_seconds / 60.0, 2)
    else:
        stats["window_clamped"] = False
    sample.window_start = int(window_start)
    sample.window_end = int(window_end)
    stats["window_actual_minutes"] = round(
        (int(window_end) - int(window_start)) / 60.0, 2
    )
    comment_targets = list(comment_targets)[: plan.comment_targets]
    danmaku_targets = list(danmaku_targets)[: plan.danmaku_targets]
    sample.comment_targets = list(comment_targets)
    sample.danmaku_targets = list(danmaku_targets)
    stats["comment_target_count"] = len(comment_targets)
    stats["danmaku_target_count"] = len(danmaku_targets)
    stats["comment_target_selected"] = sum(1 for t in comment_targets if t.source == "selected")
    stats["danmaku_target_selected"] = sum(1 for t in danmaku_targets if t.source == "selected")
    if comment_targets:
        stats["comment_targets_top"] = [t.bvid for t in comment_targets[:10]]
    if danmaku_targets:
        stats["danmaku_targets_top"] = [t.bvid for t in danmaku_targets[:10]]

    if not comment_targets and not danmaku_targets:
        no_target_reasons = ["no targets"]
        if stats.get("window_clamped"):
            no_target_reasons.append("window_clamped")
        stats["degraded_reason"] = ";".join(no_target_reasons)
        sample.status = "degraded"
        sample.stats = _finish_stats(stats, sample, started, wall_started)
        if use_db and url and sample.round_id is not None:
            try:
                persist_sample(sample, conn=db_conn, url=url)
                sample.stats["db_written"] = True
            except Exception as exc:  # noqa: BLE001
                _record_error(errors, "db:persist", "", exc)
                sample.stats["db_error"] = str(exc)[:240]
        sample.errors = errors
        return sample

    # 3) 15 分钟窗口计数
    budget = _Budget(source, plan.max_requests)
    aid_cache: dict[str, int] = {}
    danmaku_cache: dict[int, list[Danmaku]] = {}
    try:
        comments = sample_window_comments(
            source,
            comment_targets,
            int(window_start),
            int(window_end),
            plan,
            stats=stats,
            errors=errors,
            budget=budget,
            aid_cache=aid_cache,
        )
    except Exception as exc:  # noqa: BLE001 - 单阶段失败降级，不中断整轮
        _record_error(errors, "sample_comments", "", exc)
        stats["comment_stage_error"] = str(exc)[:240]
        comments = []
    try:
        danmaku = sample_window_danmaku(
            source,
            danmaku_targets,
            int(window_start),
            int(window_end),
            plan,
            stats=stats,
            errors=errors,
            budget=budget,
            danmaku_cache=danmaku_cache,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(errors, "sample_danmaku", "", exc)
        stats["danmaku_stage_error"] = str(exc)[:240]
        danmaku = []

    # 4) 30 天证据（不参与比例）
    evidence_comments: list[WindowComment] = []
    evidence_danmaku: list[WindowDanmaku] = []
    if plan.evidence:
        try:
            evidence_comments = sample_evidence_comments(
                source,
                comment_targets,
                int(window_end),
                plan,
                stats=stats,
                errors=errors,
                budget=budget,
                aid_cache=aid_cache,
            )
        except Exception as exc:  # noqa: BLE001
            _record_error(errors, "evidence_comments", "", exc)
            stats["evidence_comment_stage_error"] = str(exc)[:240]
        try:
            evidence_danmaku = sample_evidence_danmaku(
                danmaku_targets,
                int(window_end),
                plan,
                danmaku_cache,
                stats=stats,
            )
        except Exception as exc:  # noqa: BLE001
            _record_error(errors, "evidence_danmaku", "", exc)
            stats["evidence_danmaku_stage_error"] = str(exc)[:240]
    else:
        stats["evidence"] = False

    sample.comments = comments
    sample.danmaku = danmaku
    sample.evidence_comments = evidence_comments
    sample.evidence_danmaku = evidence_danmaku
    stats["budget_exhausted"] = budget.exhausted
    stats["http_requests"] = budget.used

    status, reason = _determine_status(
        plan,
        len(comments),
        len(danmaku),
        comment_target_count=len(comment_targets),
        danmaku_target_count=len(danmaku_targets),
        budget_exhausted=budget.exhausted,
        window_clamped=bool(stats.get("window_clamped")),
        no_targets=False,
    )
    sample.status = status
    stats["degraded_reason"] = reason
    stats["profile"] = {
        "comment_goal": plan.comment_goal,
        "comment_min": plan.comment_min,
        "danmaku_goal": plan.danmaku_goal,
        "danmaku_min": plan.danmaku_min,
        "comment_share": round(len(comments) / max(1, plan.comment_goal), 4),
        "danmaku_share": round(len(danmaku) / max(1, plan.danmaku_goal), 4),
    }
    sample.stats = _finish_stats(stats, sample, started, wall_started)

    # 5) 写回 round（只更新计数/状态/stats）
    if use_db and url and sample.round_id is not None:
        try:
            persist_sample(sample, conn=db_conn, url=url)
            sample.stats["db_written"] = True
        except Exception as exc:  # noqa: BLE001
            _record_error(errors, "db:persist", "", exc)
            sample.stats["db_error"] = str(exc)[:240]
    sample.errors = errors
    return sample


def _finish_stats(
    stats: dict[str, Any],
    sample: WindowSample,
    started: int,
    wall_started: float,
) -> dict[str, Any]:
    stats["comments_sampled"] = len(sample.comments)
    stats["danmaku_sampled"] = len(sample.danmaku)
    stats["evidence_comments_sampled"] = len(sample.evidence_comments)
    stats["evidence_danmaku_sampled"] = len(sample.evidence_danmaku)
    stats["status"] = sample.status
    stats["elapsed_seconds"] = round(time.monotonic() - wall_started, 2)
    stats["generated_at"] = sample.generated_at
    stats["window_start"] = sample.window_start
    stats["window_end"] = sample.window_end
    stats["round_id"] = sample.round_id
    if sample.errors:
        stats["errors"] = len(sample.errors)
    return stats


# ---------------------------------------------------------------- 便捷入口
def run_once(
    *,
    use_db: bool = True,
    limit: int | None = None,
    database_url: str | None = None,
    now: int | None = None,
    round_id: int | None = None,
) -> WindowSample:
    """CLI 便捷入口：真机 ``BiliSource`` + 可选目标上限。"""
    plan = WindowSamplePlan(comment_targets=limit or COMMENT_TARGETS)
    if limit:
        plan.danmaku_targets = min(limit, DANMAKU_TARGETS)
    return run_window_sample(
        None,
        plan,
        now=now,
        use_db=use_db,
        database_url=database_url,
        round_id=round_id,
    )
