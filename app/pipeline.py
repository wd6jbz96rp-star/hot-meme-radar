"""采集 → 抽取 → 判定 → 打分 → 落库 的旧流水线（DEPRECATED）。

Phase 0（2026-11-15）：
- 新梗榜已由独立 ``meme_info`` worker 替代；
- scheduler 默认关闭，``/api/refresh`` 已返回 410，不再触发本模块的 ``run_cycle``；
- 本文件仅保留 ``snapshot`` 等只读兼容能力，rank 引擎视为 retired。
"""
from __future__ import annotations

import hashlib
import math
import random
import threading
import time
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import config, store
from .bili import BiliClient, parse_danmaku
from .extract import Candidate, TextDoc, discover, merge_families, sample_text
from .judge import Judger, parse_origin_year, passes_meme_gate
from .llm_extract import run_extraction
from .normalize import normalize_surface, normalize_text
from .review import PROMPT_VERSION, review_candidates
from .score import score_families
from .zones import ZONES


def _now() -> datetime:
    """当前本地时间；测试/离线回放可 monkeypatch 冻结时间。"""
    return datetime.now().astimezone()


def _dedupe_videos(videos: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for video in videos:
        bvid = video.get("bvid")
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        result.append(video)
    return result


def fetch_videos(client: BiliClient) -> tuple[list[dict], dict]:
    videos: list[dict] = []
    zone_counts: dict[str, int] = {}
    for zone in ZONES:
        items = client.ranking(zone["rid"])[: config.VIDEOS_PER_ZONE]
        for position, video in enumerate(items, start=1):
            video["rank_pos"] = position
        zone_counts[zone["name"]] = len(items)
        videos.extend(items)
    popular = client.popular(50)
    for position, video in enumerate(popular, start=1):
        video["rank_pos"] = 1000 + position
    videos.extend(popular)

    for query in config.SEARCH_QUERIES:
        found = client.search_videos(query, config.SEARCH_RESULTS_PER_QUERY)
        for position, video in enumerate(found, start=1):
            video["rank_pos"] = 2000 + position
        zone_counts["梗搜索"] = zone_counts.get("梗搜索", 0) + len(found)
        videos.extend(found)
    return _dedupe_videos(videos), zone_counts


def select_targets(videos: list[dict]) -> tuple[list[dict], list[dict]]:
    by_zone: dict[str, list[dict]] = defaultdict(list)
    for video in videos:
        by_zone[video.get("zone") or "其他"].append(video)

    per_zone_comments = max(1, config.COMMENT_VIDEOS // max(1, len(by_zone)))
    per_zone_danmaku = max(1, config.DANMAKU_VIDEOS // max(1, len(by_zone)))

    search_videos = sorted(
        [video for video in videos if video.get("source") == "search"],
        key=lambda v: v.get("view", 0), reverse=True,
    )

    comment_targets: list[dict] = []
    danmaku_targets: list[dict] = []
    for items in by_zone.values():
        comment_targets.extend(
            sorted(items, key=lambda v: v.get("reply", 0), reverse=True)[:per_zone_comments]
        )
        danmaku_targets.extend(
            sorted(items, key=lambda v: v.get("danmaku", 0), reverse=True)[:per_zone_danmaku]
        )

    search_danmaku = search_videos[: config.SEARCH_DANMAKU_VIDEOS]
    search_comments = search_videos[: config.SEARCH_COMMENT_VIDEOS]
    danmaku_targets = _dedupe_videos(search_danmaku + danmaku_targets)
    comment_targets = _dedupe_videos(search_comments + comment_targets)

    # P4：分区配额是「下限」，按 reply/view 从剩余视频补齐到 45/10。
    # 视频池不足时保持现有数量（不重复选同一个 bvid）。
    def _fill(targets: list[dict], limit: int) -> list[dict]:
        if len(targets) >= limit:
            return targets[:limit]
        chosen = {video.get("bvid") for video in targets}
        for video in sorted(
            videos,
            key=lambda v: (v.get("reply", 0), v.get("view", 0)),
            reverse=True,
        ):
            if len(targets) >= limit:
                break
            bvid = video.get("bvid")
            if not bvid or bvid in chosen:
                continue
            chosen.add(bvid)
            targets.append(video)
        return targets[:limit]

    return (_fill(comment_targets, config.COMMENT_VIDEOS),
            _fill(danmaku_targets, config.DANMAKU_VIDEOS))


def _fetch_comment_page(client, aid: int, page: int, mode: int,
                        cursor: int) -> tuple[list[dict], int, bool]:
    """兼容实现 `comment_page_ex` 的客户端；旧客户端/fake 视为有效页。"""
    fetch = getattr(client, "comment_page_ex", None)
    if fetch is not None:
        return fetch(int(aid), page=page, mode=mode, cursor=cursor)
    items, next_cursor = client.comment_page(
        int(aid), page=page, mode=mode, cursor=cursor
    )
    return items, next_cursor, True


def collect_docs(client: BiliClient, conn, cycle_id: int,
                 videos: list[dict], comment_targets: list[dict],
                 danmaku_targets: list[dict]) -> tuple[list[TextDoc], dict]:
    rng = random.Random(20261114)
    stats = {"comments": 0, "comments_recent": 0, "comments_7d": 0,
             "comment_dupes": 0, "comment_filtered_old": 0,
             "comment_modes": {str(mode): 0 for mode in config.COMMENT_MODES},
             "danmaku": 0, "danmaku_docs": 0, "danmaku_cached": 0,
             "comment_requests": 0, "comment_cached": 0, "pagelist_requests": 0,
             # P4：评论页可观测性。失败页/重试次数在 collect_docs 内按客户端增量统计；
             # 失败率 > COMMENT_FAIL_RATE_MAX 才让 cycle degraded。
             "comment_page_fail": 0, "comment_page_retry": 0,
             "comment_page_fail_kinds": {}, "comment_page_fail_rate": 0.0,
             "comment_degraded": False}
    danmaku_docs: list[TextDoc] = []
    comment_docs: list[TextDoc] = []
    now_ts = _now().timestamp()
    recent_seconds = config.COMMENT_RECENT_DAYS * 86400
    retry_base = int(getattr(client, "comment_page_retry", 0) or 0)
    fail_kinds_base = Counter(getattr(client, "comment_page_fail_kinds", {}) or {})

    # 一次批量取 cid：搜索视频的 cid 在上一轮已由 collect_docs 回写，
    # 这里命中即可完全跳过 pagelist（warm 轮 25 → 0）。
    cid_lookup = store.cid_map(conn, [video["bvid"] for video in danmaku_targets])
    for video in danmaku_targets:
        cid = video.get("cid") or cid_lookup.get(video["bvid"])
        if not cid:
            stats["pagelist_requests"] += 1
            cid = client.cid_for(video["bvid"])
            if cid:
                store.update_video_cid(conn, video["bvid"], cid)
        video["cid"] = cid
        if not cid:
            continue
        payload = store.get_danmaku_cache(conn, int(cid))
        if payload:
            try:
                text = zlib.decompress(payload).decode("utf-8", errors="replace")
            except zlib.error:
                text = ""
            stats["danmaku_cached"] += 1
        else:
            text = client.danmaku(int(cid))
            if text:
                store.put_danmaku_cache(conn, int(cid), zlib.compress(text.encode("utf-8")))
        parsed = parse_danmaku(text)
        parsed = [item for item in parsed if 2 <= len(item["text"]) <= 40]
        if len(parsed) > config.MAX_DANMAKU_PER_VIDEO:
            parsed = rng.sample(parsed, config.MAX_DANMAKU_PER_VIDEO)
        stats["danmaku"] += len(parsed)
        stats["danmaku_docs"] += len(parsed)
        for item in parsed:
            danmaku_docs.append(
                TextDoc(text=item["text"], bvid=video["bvid"], zone=video.get("zone", ""),
                        kind="danmaku", owner=video.get("owner_name", ""))
            )

    window_seconds = config.COMMENT_WINDOW_DAYS * 86400
    for video in comment_targets:
        aid = video.get("aid")
        if not aid:
            continue
        per_mode: dict[int, list[dict]] = {mode: [] for mode in config.COMMENT_MODES}
        seen_rpid: set = set()
        seen_text: set = set()
        for mode in config.COMMENT_MODES:
            # P1：按 mode 配页数（mode3 1 页 / mode2 3 页）。
            # 游标必须用上一页返回的 cursor.next（实测不是 20/40 偏移量：
            # 传 20 会翻到 2024 年旧页并 is_end，正确值是如 1682 的 opaque 游标）。
            pages = int(config.COMMENT_PAGES.get(mode, 1))
            cursor = 0
            for page in range(1, pages + 1):
                cached = store.get_comment_page_cache(conn, int(aid), mode, page)
                if cached is not None:
                    # 命中缓存也按当前 cycle 落 text_sample，统计口径不变
                    items, cursor = cached
                    stats["comment_cached"] += 1
                else:
                    items, cursor, ok = _fetch_comment_page(
                        client, int(aid), page=page, mode=mode, cursor=cursor
                    )
                    stats["comment_requests"] += 1
                    if ok:
                        # 只缓存有效 JSON 页；失败页不写缓存，下一轮重新请求。
                        store.put_comment_cache(conn, int(aid), mode, page, items,
                                                next_cursor=cursor)
                    else:
                        stats["comment_page_fail"] += 1
                for item in items:
                    text = (item.get("text") or "").strip()
                    if not text:
                        continue
                    rpid = item.get("rpid")
                    if rpid is not None:
                        if rpid in seen_rpid:
                            stats["comment_dupes"] += 1
                            continue
                    norm = normalize_surface(text)
                    if norm in seen_text:
                        stats["comment_dupes"] += 1
                        continue
                    try:
                        ctime = int(item.get("ctime") or 0)
                    except (TypeError, ValueError):
                        ctime = 0
                    if ctime <= 0 or (now_ts - ctime) > window_seconds:
                        stats["comment_filtered_old"] += 1
                        continue
                    if rpid is not None:
                        seen_rpid.add(rpid)
                    seen_text.add(norm)
                    per_mode[mode].append(
                        {"text": text, "like": int(item.get("like") or 0),
                         "ctime": ctime, "rpid": rpid, "_mode": mode,
                         "_recent": (now_ts - ctime) <= recent_seconds}
                    )
                # 翻页停止只认 is_end（cursor=0）；短页不代表没有下一页
                # （实测 mode=2 首页条数少，但 cursor.next 仍可继续）。
                if cursor == 0:
                    break

        # 热度评论按赞、时间评论按时间各取一半，再合并补齐，保留两种证据。
        limit = config.COMMENT_MAX_PER_VIDEO
        per_mode_cap = max(1, limit // max(1, len(config.COMMENT_MODES)))
        picked: list[dict] = []
        leftovers: list[dict] = []
        for mode in config.COMMENT_MODES:
            bucket = per_mode[mode]
            if mode == 3:
                bucket.sort(key=lambda c: c["like"], reverse=True)
            else:
                bucket.sort(key=lambda c: c["ctime"], reverse=True)
            picked.extend(bucket[:per_mode_cap])
            leftovers.extend(bucket[per_mode_cap:])
        if len(picked) < limit:
            leftovers.sort(key=lambda c: (c["like"], c["ctime"]), reverse=True)
            picked.extend(leftovers[: limit - len(picked)])
        picked.sort(key=lambda c: (c["like"], c["ctime"]), reverse=True)
        picked = picked[:limit]

        stats["comments"] += len(picked)
        stats["comments_recent"] += len(picked)
        for entry in picked:
            if entry["_recent"]:
                stats["comments_7d"] += 1
            stats["comment_modes"][str(entry["_mode"])] += 1
        if picked:
            store.insert_text_samples(conn, cycle_id, video["bvid"], "comment", picked)
        for entry in picked:
            doc = TextDoc(text=entry["text"], bvid=video["bvid"],
                          zone=video.get("zone", ""), kind="comment",
                          likes=entry["like"], owner=video.get("owner_name", ""))
            # ctime/mode 作为动态属性附加：extract.TextDoc 由其他会话维护，不在此改字段。
            doc.ctime = entry["ctime"]
            doc.mode = entry["_mode"]
            doc.rpid = entry["rpid"]
            comment_docs.append(doc)

    header_docs: list[TextDoc] = []
    for video in videos:
        if video.get("title"):
            header_docs.append(
                TextDoc(text=video["title"], bvid=video["bvid"], zone=video.get("zone", ""),
                        kind="title", owner=video.get("owner_name", ""))
            )
        if video.get("desc"):
            header_docs.append(
                TextDoc(text=video["desc"], bvid=video["bvid"], zone=video.get("zone", ""),
                        kind="desc", owner=video.get("owner_name", ""))
            )
        if video.get("tags"):
            header_docs.append(
                TextDoc(text=video["tags"].replace(",", " "), bvid=video["bvid"],
                        zone=video.get("zone", ""), kind="tag",
                        owner=video.get("owner_name", ""))
            )

    stats["comment_page_retry"] = max(
        0, int(getattr(client, "comment_page_retry", 0) or 0) - retry_base
    )
    kinds = Counter(getattr(client, "comment_page_fail_kinds", {}) or {}) - fail_kinds_base
    stats["comment_page_fail_kinds"] = dict(kinds)
    attempted = stats["comment_requests"]
    fail_rate = (stats["comment_page_fail"] / attempted) if attempted else 0.0
    stats["comment_page_fail_rate"] = round(fail_rate, 4)
    # 单页/少量失败不降级整轮；失败页 >20% 才标 degraded（仍用已有语料产出）。
    stats["comment_degraded"] = bool(
        attempted and fail_rate > config.COMMENT_FAIL_RATE_MAX
    )

    rng.shuffle(danmaku_docs)
    return danmaku_docs + comment_docs + header_docs, stats


def compute_usage(families: list[dict], docs: list[TextDoc],
                  video_meta: dict[str, dict]) -> dict[str, dict]:
    """统计每个梗族出现在哪些视频、出现频次与集中度。"""
    surface_index: dict[str, str] = {}
    long_surfaces: list[str] = []
    for family in families:
        for surface in family.get("members", [])[:20]:
            if len(surface) < 2:
                continue
            surface_index.setdefault(surface, family["family_key"])
            if len(surface) > config.MAX_NGRAM:
                long_surfaces.append(surface)
    long_surfaces = long_surfaces[:100]

    usage: dict[str, dict] = {
        family["family_key"]: {
            "videos": set(), "search_videos": set(),
            "count": 0, "per_video": Counter(),
            # 评论区证据（P0）：文档数、点赞/时间样本、覆盖视频、弹幕命中
            "comment_likes": [], "comment_videos": set(), "danmaku_docs": 0,
        }
        for family in families
    }

    for doc in docs:
        bvid = doc.bvid
        if not bvid or bvid not in video_meta:
            continue
        hits: dict[str, int] = {}

        for seg in doc.segs:
            length = len(seg)
            if length > 60:
                continue
            for i in range(length):
                limit = min(config.MAX_NGRAM, length - i)
                for n in range(2, limit + 1):
                    key = surface_index.get(seg[i : i + n])
                    if key:
                        hits[key] = hits.get(key, 0) + 1

        for surface in long_surfaces:
            if surface in doc.text:
                key = surface_index.get(surface)
                if key:
                    hits[key] = hits.get(key, 0) + 1

        ctime = int(getattr(doc, "ctime", 0) or 0)
        likes = max(0, int(doc.likes or 0))
        for key, count in hits.items():
            bucket = usage.get(key)
            if bucket is None:
                continue
            bucket["videos"].add(bvid)
            bucket["count"] += count
            bucket["per_video"][bvid] += count
            if video_meta[bvid].get("source") == "search":
                bucket["search_videos"].add(bvid)
            if doc.kind == "comment":
                bucket["comment_likes"].append((ctime, likes))
                bucket["comment_videos"].add(bvid)
            elif doc.kind == "danmaku":
                bucket["danmaku_docs"] += 1

    return usage


def comment_evidence(bucket: dict | None, now_ts: float) -> dict:
    """评论证据（P0）：窗口计数、年龄归一赞、7 天增速、弹幕刷屏标记。

    - `comment_count_recent`：≤COMMENT_WINDOW_DAYS 的评论文档数；
    - `comment_7d_count`：≤COMMENT_RECENT_DAYS 的评论文档数；
    - `comment_like_weighted`：Σ log1p(like) × 0.5^(age_days / 半衰期)；
    - `comment_growth_7d`：(近 7 天 + 1) / (7–30 天 + 1)，>1 表示在加速；
    - `danmaku_only`：弹幕有命中、评论近 7 天与视频覆盖都低 → 老梗刷屏旁证。
    """
    bucket = bucket or {}
    window = config.COMMENT_WINDOW_DAYS * 86400
    recent_window = config.COMMENT_RECENT_DAYS * 86400
    pairs = [(int(t), int(like)) for t, like in bucket.get("comment_likes", []) if t > 0]
    recent = [(t, like) for t, like in pairs if 0 <= now_ts - t <= window]
    week = [(t, like) for t, like in pairs if 0 <= now_ts - t <= recent_window]
    mid = len(recent) - len(week)
    weighted = 0.0
    for t, like in recent:
        age_days = max(0.0, (now_ts - t) / 86400.0)
        weighted += math.log1p(like) * math.pow(
            0.5, age_days / max(0.1, config.COMMENT_LIKE_HALF_LIFE_DAYS))
    earliest = min((t for t, _ in recent), default=0)
    df_video = len(bucket.get("comment_videos") or ())
    danmaku_docs = int(bucket.get("danmaku_docs", 0))
    return {
        "comment_count_recent": len(recent),
        "comment_7d_count": len(week),
        "comment_mid_count": mid,
        "comment_like_weighted": round(weighted, 2),
        "comment_growth_7d": round((len(week) + 1.0) / (mid + 1.0), 3),
        "comment_df_video": df_video,
        "danmaku_docs": danmaku_docs,
        "earliest_comment_at": (
            datetime.fromtimestamp(earliest).astimezone().isoformat(timespec="seconds")
            if earliest else ""
        ),
        "age_days": (round((now_ts - earliest) / 86400.0, 1) if earliest else None),
        "danmaku_only": bool(danmaku_docs >= 3 and df_video < 2 and len(week) < 2),
    }


def comment_gate(item: dict, evidence: dict, evidence_ready: bool) -> str:
    """P0 门控：评论证据健康时，非种子候选无任何 ≤30 天评论证据 → rejected。

    返回拒绝原因（空串 = 放行）。种子仍按原有规则，不受此门控影响；
    评论采集不健康（本轮评论文档 < COMMENT_GATE_MIN_DOCS）时整体跳过门控，
    避免单源失败把候选池清零。
    """
    if not evidence_ready or item.get("seed_hit"):
        return ""
    if int(evidence.get("comment_count_recent", 0)) > 0:
        return ""
    return "no_recent_comment"


def comment_backed_candidate(item: dict, evidence: dict) -> bool:
    """P1 评论证据通道：非种子候选满足近期评论门槛即可绕过 `MEME_MIN` 进池。

    门槛：≤30 天评论 ≥3 条、覆盖 ≥2 个视频、≤7 天评论 ≥1 条。
    种子不走该通道（仍按原梗性规则），避免种子再拿一条旁路。
    """
    if item.get("seed_hit"):
        return False
    return (
        int(evidence.get("comment_count_recent", 0)) >= config.COMMENT_BACKED_MIN_RECENT
        and int(evidence.get("comment_df_video", 0)) >= config.COMMENT_BACKED_MIN_VIDEOS
        and int(evidence.get("comment_7d_count", 0)) >= config.COMMENT_BACKED_MIN_7D
    )


def known_age_years(origin_year: int | None, now: datetime) -> float | None:
    """种子 origin 年份 → 已知梗年龄（年，按 7/1 中点；未来年份归 0）。"""
    if not origin_year:
        return None
    midpoint = datetime(origin_year, 7, 1, tzinfo=now.tzinfo)
    return round(max(0.0, (now - midpoint).total_seconds() / (365.25 * 86400)), 2)


def novelty_score(evidence: dict, origin_year: int | None, now: datetime) -> float:
    """0-1 novelty：年龄新鲜度 × 评论活跃度。

    - 年龄：种子取种子表 `origin` 年份（已知梗年龄），非种子取最早评论 `age_days`，
      两者都有时取较大值（老梗不能靠窗口内最早的近期评论装年轻）；
    - 新鲜度 `0.5^(年龄年数 / 2)`：2 年半衰，2024 梗 ≈0.44、2022 梗 ≈0.22；
    - 活跃度 `0.5×量 + 0.3×扩散 + 0.2×加速度`：
      `量 = min(1, log1p(c7)/log1p(20))`、`扩散 = min(1, df_video/5)`、
      `加速度 = clip((growth_7d-1)/4, 0, 1)`；
    - 无评论时活跃度为 0 → novelty=0，不奖励只有弹幕/曝光的老梗。
    """
    c7 = max(0, int(evidence.get("comment_7d_count", 0) or 0))
    growth = float(evidence.get("comment_growth_7d", 1.0) or 1.0)
    df_video = max(0, int(evidence.get("comment_df_video", 0) or 0))
    age_days = evidence.get("age_days")
    age_days = float(age_days) if age_days is not None else 0.0
    if origin_year:
        age_days = max(age_days, (known_age_years(origin_year, now) or 0.0) * 365.25)
    elif evidence.get("age_days") is None:
        # 没有评论证据时按窗口上限计算（活跃度为 0，最终 novelty 仍为 0）
        age_days = float(config.COMMENT_WINDOW_DAYS)
    age_days = max(0.0, age_days)
    freshness = math.pow(0.5, (age_days / 365.25) / config.NOVELTY_HALF_LIFE_YEARS)
    volume = min(1.0, math.log1p(c7) / math.log1p(config.NOVELTY_VOLUME_CAP))
    spread = min(1.0, df_video / float(config.NOVELTY_SPREAD_CAP))
    accel = min(1.0, max(0.0, (growth - 1.0) / 4.0))
    novelty = freshness * (0.5 * volume + 0.3 * spread + 0.2 * accel)
    return round(max(0.0, min(1.0, novelty)), 3)


def classify_list(seed_hit: bool, seed_origin_year: int | None,
                  age_days: float | None, comment_7d_count: int) -> str:
    """新旧榜：种子按 origin 年（>=2024 新）；非种子按最早评论年龄（<=90 天且 7 天有量）。

    不为了凑数把老梗标 new：种子无 origin 年份、非种子无 7 天评论一律 classic。
    """
    if seed_hit and seed_origin_year:
        return "new" if seed_origin_year >= config.SEED_ORIGIN_NEW_YEAR else "classic"
    if (
        not seed_hit
        and age_days is not None
        and float(age_days) <= config.NON_SEED_NEW_MAX_AGE_DAYS
        and int(comment_7d_count or 0) >= 1
    ):
        return "new"
    return "classic"


def confirm_rounds() -> int:
    """`hype_streak` 只查已落库的历史轮次（当前轮尚未写入），故减 1。"""
    return max(1, config.CONFIRM_ROUNDS - 1)


def status_for(item: dict, family: dict, streak: bool) -> str:
    """0-100 综合分的上榜状态；稳定性口径见 docs/02 §4。"""
    if item["score"] >= config.MIN_SCORE:
        newcomer = bool(item.get("is_new")) and (
            item["components"].get("exposure", 0.0) >= config.NEWCOMER_PERCENTILE
        )
        enough_videos = family.get("df_video", 0) >= config.MIN_VIDEOS_FOR_RANK
        return "hot" if (enough_videos and (streak or newcomer)) else "observing"
    if item["score"] >= config.OBSERVE_MIN or item.get("seed_hit"):
        return "observing"
    return "rejected"


# ---------------------------------------------------------------------------
# P3：LLM 审核发布门（只作用于 list=new）
# ---------------------------------------------------------------------------

_DEFAULT_REVIEWER = object()   # 区分「未注入」与显式 reviewer=None（模拟无 key）


class ReviewBudgetExceeded(TimeoutError):
    """审核阶段超过 `HOTMEME_LLM_BUDGET_SECONDS`；由 pipeline 降级为规则榜。"""


class _DeadlineLLM:
    """给真实客户端套总耗时闸门：超过 deadline 的下一次调用直接抛 budget。

    外层 `review_with_budget` 负责硬停；这个代理让 P2 循环在两次调用之间尽快
    收口，避免被放弃的线程继续烧调用预算。
    """

    def __init__(self, llm, deadline: float) -> None:
        self._llm = llm
        self._deadline = deadline
        self.model = getattr(llm, "model", "") or ""
        self.config = getattr(llm, "config", None)

    def chat_json(self, *args, **kwargs):
        if time.monotonic() > self._deadline:
            from .llm import LlmError

            raise LlmError("budget", "审核阶段总耗时预算已用尽")
        return self._llm.chat_json(*args, **kwargs)


def llm_mode() -> str:
    mode = str(getattr(config, "LLM_MODE", "off") or "off").strip().lower()
    return mode if mode in config.LLM_MODES else "off"


def llm_stats_defaults() -> dict:
    """每轮 stats 固定带 llm_* 字段（模式矩阵与监控依赖这些键存在）。"""
    return {
        "llm_mode": llm_mode(),
        "llm_calls": 0,
        "llm_rounds": 0,
        "llm_approved": 0,
        "llm_rejected": 0,
        "llm_reviewed": 0,
        "llm_gate_applied": False,
        "llm_fallback": False,
        "llm_fallback_reason": "",
    }


def build_reviewer():
    """按 env 构造真实 LLM 客户端；off / 无 key 返回 None（绝不发起网络请求）。"""
    if llm_mode() == "off":
        return None
    try:
        from .llm import ChatClient, LlmConfig

        cfg = LlmConfig.from_env()
        if not cfg.configured:
            return None
        return ChatClient(cfg)
    except Exception:  # noqa: BLE001 - 配置错误不能阻塞采集
        return None


def _review_item_extract_meta(item: dict) -> dict:
    """读取 P5 写进 item 的 `llm_extract` metadata（三种形状兼容）。"""
    for holder in (item, item.get("components"), item.get("raw_signals")):
        if not isinstance(holder, dict):
            continue
        meta = holder.get("llm_extract")
        if isinstance(meta, dict) and meta:
            return meta
        source = holder.get("source")
        if isinstance(source, str) and "llm_extract" in source:
            return {"source": source}
    return {}


def _review_item_comment_backed(item: dict) -> bool:
    """P1 评论证据候选标记：item / components / raw_signals 任一为真。"""
    if bool(item.get("comment_backed")):
        return True
    for holder in (item.get("components"), item.get("raw_signals")):
        if isinstance(holder, dict) and bool(holder.get("comment_backed")):
            return True
    return False


def review_pool_kind(item: dict) -> str:
    """审核池优先级分类：extract > comment_backed > score。"""
    if _review_item_extract_meta(item):
        return "extract"
    if _review_item_comment_backed(item):
        return "comment"
    return "score"


def review_pool(items: list[dict]) -> list[dict]:
    """`list=new` 审核池：抽取候选 > 评论证据候选 > 规则分补足。

    P6 根因：真实 shadow 里规则垃圾（ai/co/ge…）按 score 排在前面，抽取出的
    「给我整不会了/油腻老登」等候选分低，被 top50 挤掉，LLM 一次都没审到。
    这里改为三级优先级，组内仍按 score 降序；池上限与 `LLM_REVIEW_LIMIT`
    （默认 24）对齐，配合 `LLM_MAX_CALLS`=24 保证池内每条都能审到。
    只选 `hot/observing`（可发布候选），不为凑数把 rejected 拉进审核。
    """
    eligible = [
        item for item in items
        if item.get("list") == "new" and item.get("status") in ("hot", "observing")
    ]
    groups: dict[str, list[dict]] = {"extract": [], "comment": [], "score": []}
    for item in eligible:
        groups[review_pool_kind(item)].append(item)
    def _rank_key(item: dict) -> tuple[float, str]:
        return (-float(item.get("score") or 0.0), str(item.get("canonical") or ""))
    ordered = (
        sorted(groups["extract"], key=_rank_key)
        + sorted(groups["comment"], key=_rank_key)
        + sorted(groups["score"], key=_rank_key)
    )
    return ordered[: max(0, int(config.LLM_REVIEW_LIMIT))]


def review_pool_stats(pool: list[dict]) -> dict:
    """审核池构成（截断后）+ 明细，供 stats / shadow 报告核对。"""
    kinds = [review_pool_kind(item) for item in pool]
    detail = []
    for item, kind in zip(pool, kinds):
        meta = _review_item_extract_meta(item)
        detail.append({
            "family_key": item.get("family_key"),
            "canonical": item.get("canonical"),
            "score": round(float(item.get("score") or 0.0), 2),
            "pool_kind": kind,
            "known": bool(item.get("llm_known") or meta.get("known")),
        })
    return {
        "llm_pool_extract": kinds.count("extract"),
        "llm_pool_comment": kinds.count("comment"),
        "llm_pool_score": kinds.count("score"),
        "llm_pool_details": detail,
    }


def review_candidate(item: dict, family: dict | None = None,
                     usage: dict | None = None,
                     video_meta: dict | None = None,
                     docs: list | None = None) -> dict:
    """pipeline item → `EvidencePacket.from_candidate` 需要的扁平候选。

    P1 证据在 `components.comment` / `components.raw` 里；这里映射成 P2 契约字段
    （缺字段留空，EvidencePacket 会按 `unclear` 兜底，不抛异常）。
    """
    family = family or {}
    components = item.get("components") if isinstance(item.get("components"), dict) else {}
    evidence = components.get("comment")
    if not isinstance(evidence, dict) or not evidence:
        # P1 契约：evidence 同时以扁平字段写入 components.raw / raw_signals；
        # 兼容「从 DB 读回的 signal」与「pipeline 内存 item」两种形状。
        raw = components.get("raw")
        if not isinstance(raw, dict):
            raw = item.get("raw_signals")
        if not isinstance(raw, dict):
            raw = {}
        evidence = raw.get("comment_evidence")
        if not isinstance(evidence, dict) or not evidence:
            evidence = raw
    if not isinstance(evidence, dict):
        evidence = {}
    canonical = str(item.get("canonical") or "")
    recent_comments: list[dict] = []
    danmaku: list[str] = []
    for doc in docs or ():
        text = getattr(doc, "text", "") or ""
        if not canonical or canonical not in text:
            continue
        kind = getattr(doc, "kind", "")
        if kind == "comment":
            recent_comments.append({
                "text": text,
                "ctime": int(getattr(doc, "ctime", 0) or 0),
                "like": int(doc.likes or 0),
            })
        elif kind == "danmaku" and len(danmaku) < 3:
            danmaku.append(text)
    recent_comments.sort(key=lambda c: (c["like"], c["ctime"], c["text"]), reverse=True)

    videos: list[dict] = []
    bucket = (usage or {}).get(item.get("family_key")) or {}
    for bvid in sorted(bucket.get("videos") or ())[:5]:
        meta = (video_meta or {}).get(bvid) or {}
        videos.append({"title": meta.get("title", ""), "zone": meta.get("zone", "")})

    numeric_components = {
        str(key): value for key, value in components.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return {
        "candidate_id": item.get("family_key") or f"cand:{canonical}",
        "canonical": canonical,
        "aliases": list(family.get("members") or [])[:6],
        "type": family.get("type", ""),
        "score": float(item.get("score") or 0.0),
        "components": numeric_components,
        "age_days": evidence.get("age_days"),
        "seed_origin_year": evidence.get("seed_origin_year"),
        "comment_7d_count": int(evidence.get("comment_7d_count") or 0),
        "comment_df_video": int(evidence.get("comment_df_video") or 0),
        "comment_like_weighted": float(evidence.get("comment_like_weighted") or 0.0),
        "recent_comments": recent_comments[:10],
        "videos": videos,
        "danmaku": danmaku[:3],
    }


def invoke_reviewer(reviewer, candidates: list[dict], **kwargs) -> dict:
    """允许注入 P2 客户端（有 chat_json）或整段假审核器（callable）。

    测试用 callable 可完全绕过网络与 prompt；真实路径走 `review_candidates`。
    """
    if hasattr(reviewer, "chat_json"):
        return review_candidates(candidates, reviewer, **kwargs)
    if callable(reviewer):
        return reviewer(candidates, **kwargs)
    raise TypeError("reviewer 必须是 chat_json 客户端或 callable")


def review_with_budget(fn, budget_seconds: float):
    """在线程里跑审核并 join 总预算；超时抛 ReviewBudgetExceeded，不阻塞采集。

    审核阶段不访问 DB，被放弃的 daemon 线程最多再耗一次单请求超时；
    结果已被丢弃，不会改动任何发布状态。
    """
    if budget_seconds <= 0:
        return fn()
    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样抛回主线程
            box["error"] = exc

    thread = threading.Thread(target=runner, name="llm-review", daemon=True)
    thread.start()
    thread.join(budget_seconds)
    if thread.is_alive():
        raise ReviewBudgetExceeded(f"审核超过 {budget_seconds:.1f}s 预算")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def review_entries(items: list[dict], result: dict) -> dict[str, dict]:
    """把 P2 的 approved/rejected/merged/log 收敛成 family_key → verdict。"""
    by_key = {item["family_key"]: item for item in items}
    by_canonical = {str(item.get("canonical") or ""): item for item in items}
    entries: dict[str, dict] = {}

    def add(entry: dict) -> None:
        if not isinstance(entry, dict):
            return
        item = by_key.get(entry.get("candidate_id")) or by_key.get(entry.get("family_key"))
        if item is None:
            item = by_canonical.get(str(entry.get("canonical") or ""))
        if item is None:
            return
        verdict = str(entry.get("verdict") or "").strip().lower()
        if verdict not in ("approve", "reject", "merge", "unclear"):
            return
        entries[item["family_key"]] = {
            "verdict": verdict,
            "meme_prob": _float(entry.get("meme_prob")),
            "novelty": str(entry.get("novelty") or ""),
            "origin": str(entry.get("origin") or ""),
            "reason": str(entry.get("reason") or ""),
            "merge_into": str(entry.get("merge_into") or ""),
            "evidence_hash": str(entry.get("evidence_hash") or ""),
            "model": str(entry.get("model") or ""),
            "prompt_version": str(entry.get("prompt_version") or ""),
        }

    for key in ("approved", "rejected", "merged"):
        for entry in result.get(key) or ():
            add(entry)
    for entry in result.get("log") or ():   # log 字段最全，后写覆盖
        add(entry)
    return entries


def apply_review_gate(items: list[dict], entries: dict[str, dict],
                      mode: str) -> tuple[set[str], set[str]]:
    """把审核结论落到新梗榜可见性；不改 score/components，也不动 classic/all。

    - shadow：只挂 `llm_review`，发布集合与不审一致；
    - demote：reject/merge 撤下新梗榜；approve/unclear/未审保留；
    - require_approve：只有 approve 保留；未审（超出 top50）一律不上榜。
    rank=0 的项被撤下时直接 `status=rejected`（本来就不落库），
    rank>0 的项保留规则 status 以免改变 all/classic 的主榜，由 list_items 过滤。
    """
    published: set[str] = set()
    withheld: set[str] = set()
    for item in items:
        if item.get("list") != "new" or item.get("status") not in ("hot", "observing"):
            continue
        key = item["family_key"]
        entry = entries.get(key)
        if entry:
            item["llm_review"] = {
                "verdict": entry["verdict"],
                "reason": entry["reason"],
                "meme_prob": entry["meme_prob"],
                "novelty": entry["novelty"],
                "merge_into": entry["merge_into"],
            }
        if mode == "shadow":
            item["new_list_status"] = "shadow"
            published.add(key)
            continue
        if mode == "demote":
            keep = not entry or entry["verdict"] not in ("reject", "merge")
        elif mode == "require_approve":
            keep = bool(entry and entry["verdict"] == "approve")
        else:
            keep = True
        if keep:
            item["new_list_status"] = "published"
            published.add(key)
        else:
            item["new_list_status"] = "withheld"
            withheld.add(key)
            if int(item.get("rank") or 0) == 0:
                item["status"] = "rejected"
    return published, withheld


def run_llm_gate(conn, cycle_id: int, items: list[dict], families_by_key: dict,
                 docs: list, video_meta: dict, usage: dict,
                 reviewer=_DEFAULT_REVIEWER) -> tuple[dict, str]:
    """审核 `list=new` top N 并应用发布门；返回 (stats, status_override)。

    外层兜底：候选构造、审计、应用结果等任何未预期异常都收敛为规则榜，
    绝不向上抛、不阻塞采集。
    """
    stats = llm_stats_defaults()
    try:
        return _run_llm_gate(
            conn, cycle_id, items, families_by_key, docs, video_meta, usage,
            reviewer, stats,
        )
    except Exception as exc:  # noqa: BLE001 - 审核阶段任何异常都不能拖垮采集
        stats["llm_fallback"] = True
        stats["llm_fallback_reason"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return stats, ""


def _run_llm_gate(conn, cycle_id: int, items: list[dict], families_by_key: dict,
                  docs: list, video_meta: dict, usage: dict, reviewer, stats: dict
                  ) -> tuple[dict, str]:
    mode = llm_mode()
    if mode == "off":
        return stats, ""

    pool = review_pool(items)
    stats["llm_pool"] = len(pool)
    stats.update(review_pool_stats(pool))
    # 可观测性：抽取候选在 list=new 的总数 vs 过了规则闸门可送审的数量。
    # 两者差距大 = 抽取候选证据不足（多数只有 1 条评论），不是审核池排序问题。
    new_extract = [
        item for item in items
        if item.get("list") == "new" and review_pool_kind(item) == "extract"
    ]
    stats["llm_extract_new_total"] = len(new_extract)
    stats["llm_extract_admitted_total"] = sum(
        1 for item in new_extract if item.get("status") in ("hot", "observing")
    )
    instance = build_reviewer() if reviewer is _DEFAULT_REVIEWER else reviewer
    if instance is None:
        stats["llm_fallback"] = True
        stats["llm_fallback_reason"] = "not_configured"
        return stats, ""
    if not pool:
        # 没有可审的新梗项也是 approved=0：严格模式按「沿用上一轮/空榜」处理。
        if mode == "require_approve":
            previous = store.previous_approved_cycle(conn, cycle_id)
            stats["llm_fallback"] = True
            if previous:
                stats["llm_fallback_reason"] = "no_approved_previous_cycle"
                stats["llm_new_list_cycle"] = previous
                stats["llm_gate_applied"] = True
                return stats, ""
            stats["llm_fallback_reason"] = "no_approved: 审核未通过（0 条 approve）"
            stats["llm_new_list_cycle"] = 0
            stats["llm_gate_applied"] = True
            return stats, "degraded"
        return stats, ""

    candidates = [
        review_candidate(
            item, families_by_key.get(item["family_key"]), usage, video_meta, docs
        )
        for item in pool
    ]
    target = max(1, int(config.LLM_TARGET))
    kwargs = {
        "max_rounds": max(1, int(config.LLM_MAX_ROUNDS)),
        "target": target,
        "max_calls": max(0, int(config.LLM_MAX_CALLS)),
        "limit": max(1, int(config.LLM_REVIEW_LIMIT)),
    }
    budget = float(config.LLM_BUDGET_SECONDS)
    target_llm = instance
    if budget > 0 and hasattr(instance, "chat_json"):
        # 双重闸门：代理让 P2 循环在调用间隙看到预算，外层线程硬停兜底。
        target_llm = _DeadlineLLM(instance, time.monotonic() + budget)
    try:
        result = review_with_budget(
            lambda: invoke_reviewer(target_llm, candidates, **kwargs),
            budget,
        )
    except ReviewBudgetExceeded:
        stats["llm_fallback"] = True
        stats["llm_fallback_reason"] = "timeout_budget"
        return stats, ""
    except Exception as exc:  # noqa: BLE001 - 审核失败必须降级而非中断采集
        stats["llm_fallback"] = True
        stats["llm_fallback_reason"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return stats, ""
    if not isinstance(result, dict):
        stats["llm_fallback"] = True
        stats["llm_fallback_reason"] = "empty_result"
        return stats, ""

    entries = review_entries(items, result)
    model = str(getattr(instance, "model", "") or "")
    prompt_version = str(result.get("prompt_version") or PROMPT_VERSION)
    approved = list(result.get("approved") or ())
    rejected = list(result.get("rejected") or ())
    merged = list(result.get("merged") or ())
    stats.update({
        "llm_calls": int(result.get("calls") or 0),
        "llm_rounds": int(result.get("rounds") or 0),
        "llm_approved": len(approved),
        "llm_rejected": len(rejected) + len(merged),
        "llm_merged": len(merged),
        "llm_reviewed": len(entries),
        "llm_pool": len(pool),
        "llm_shortfall": max(0, target - len(approved)),
        "llm_model": model,
        "llm_prompt_version": prompt_version,
    })

    audit = [
        {
            "family_key": key,
            "verdict": entry["verdict"],
            "meme_prob": entry["meme_prob"],
            "novelty": entry["novelty"],
            "origin": entry["origin"],
            "reason": entry["reason"],
            "evidence_hash": entry["evidence_hash"],
            "merge_into": entry["merge_into"],
            "mode": mode,
            "model": entry["model"] or model,
            "prompt_version": entry["prompt_version"] or prompt_version,
        }
        for key, entry in entries.items()
    ]
    try:
        if audit:
            store.insert_llm_reviews(conn, cycle_id, audit)
    except Exception as exc:  # noqa: BLE001 - 审计写失败也不能中断采集
        stats["llm_fallback"] = True
        stats["llm_fallback_reason"] = f"audit_error: {type(exc).__name__}"
        return stats, ""

    degraded = str(result.get("degraded_reason") or "")
    infra_failure = (
        degraded in ("budget", "llm_error_streak", "candidate_leak")
        or degraded.startswith("llm_")
    )
    if infra_failure:
        # 预算/鉴权/连续错误：本轮审核不可信，回到规则榜（审计仍保留已审部分）。
        stats["llm_fallback"] = True
        stats["llm_fallback_reason"] = degraded
        return stats, ""

    apply_review_gate(items, entries, mode)
    stats["llm_gate_applied"] = True

    if mode == "require_approve" and len(approved) == 0:
        previous = store.previous_approved_cycle(conn, cycle_id)
        stats["llm_fallback"] = True
        if previous:
            stats["llm_fallback_reason"] = "no_approved_previous_cycle"
            stats["llm_new_list_cycle"] = previous
            return stats, ""
        stats["llm_fallback_reason"] = "no_approved: 审核未通过（0 条 approve）"
        stats["llm_new_list_cycle"] = 0
        return stats, "degraded"
    return stats, ""


# ---------------------------------------------------------------------------
# P5：LLM 候选抽取（在 discover 之前补候选池，不替代 n-gram）
# ---------------------------------------------------------------------------

DEFAULT_EXTRACTOR = object()   # 区分「未注入（按 env 构建）」与显式 None（禁用）

LLM_EXTRACT_DETAIL_LIMIT = 30


def llm_extract_stats_defaults() -> dict:
    """每轮 stats 固定带 llm_extract_* 字段（关闭时也要有键，便于监控）。"""
    return {
        "llm_extract_enabled": bool(getattr(config, "LLM_EXTRACT_ENABLED", False)),
        "llm_extract_reason": "disabled",
        "llm_extract_input": 0,
        "llm_extract_sampled": 0,
        "llm_extract_calls": 0,
        "llm_extract_batches": 0,
        "llm_extract_cache_hits": 0,
        "llm_extract_candidates": 0,
        "llm_extract_merged": 0,
        "llm_extract_added": 0,
        "llm_extract_known": 0,
        "llm_extract_known_families": 0,
        "llm_extract_known_blocked": 0,
        "llm_extract_skipped": 0,
        "llm_extract_prompt_tokens": 0,
        "llm_extract_completion_tokens": 0,
        "llm_extract_total_tokens": 0,
        "llm_extract_details": [],
    }


def _comment_doc_id(doc: TextDoc) -> str:
    """与 llm_extract.select_comments 的稳定 id 口径一致：rpid，否则 h:<sha12>。"""
    rpid = getattr(doc, "rpid", None)
    if rpid not in (None, ""):
        return str(rpid)
    text = normalize_text(str(getattr(doc, "text", "") or ""))
    return "h:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _collect_llm_comments(
    docs: list[TextDoc], video_meta: dict | None = None
) -> tuple[list[dict], dict[str, dict]]:
    """只取 `kind=="comment"` 的文档；title 从视频元数据按 bvid 关联。"""
    meta = video_meta or {}
    comments: list[dict] = []
    for doc in docs or ():
        if getattr(doc, "kind", "") != "comment":
            continue
        text = str(getattr(doc, "text", "") or "").strip()
        if not text:
            continue
        bvid = str(getattr(doc, "bvid", "") or "")
        video = meta.get(bvid) or {}
        comments.append({
            "id": _comment_doc_id(doc),
            "text": text,
            "ctime": int(getattr(doc, "ctime", 0) or 0),
            "like": int(getattr(doc, "likes", 0) or 0),
            "bvid": bvid,
            "zone": str(getattr(doc, "zone", "") or ""),
            "title": str(video.get("title") or ""),
        })
    return comments, {comment["id"]: comment for comment in comments}


def _coerce_extract_report(result) -> dict:
    """注入的 extractor 可返回 report dict 或 candidate list，统一成 report。"""
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)):
        candidates = [item for item in result if isinstance(item, dict)]
        return {
            "candidates": candidates,
            "reason": "ok" if candidates else "no_candidates",
            "calls": 0,
            "sampled": len(candidates),
        }
    raise TypeError("extractor 必须返回 report dict 或 candidate list")


def _evidence_id_list(value) -> list[str]:
    """evidence_ids 容错：正常是 list；单值/字符串也收，坏类型返回空。"""
    if isinstance(value, str):
        return [value][:5] if value else []
    if isinstance(value, (list, tuple)):
        return [str(eid) for eid in value if str(eid)][:5]
    return []


def _llm_detail(item: dict) -> dict:
    return {
        "surface": str(item.get("surface") or ""),
        "canonical": str(item.get("canonical") or ""),
        "type": str(item.get("type") or "词"),
        "confidence": round(_float(item.get("confidence"), 0.0), 3),
        "known": bool(item.get("known")),
        "meaning": str(item.get("meaning") or "")[:80],
        "origin_quote": str(item.get("origin_quote") or "")[:80],
        "evidence_ids": _evidence_id_list(item.get("evidence_ids")),
    }


def run_llm_extraction(docs: list[TextDoc], video_meta: dict | None = None,
                       extractor=DEFAULT_EXTRACTOR
                       ) -> tuple[list[dict], dict[str, dict], dict]:
    """从 docs 的评论跑 LLM 抽取；返回 (candidates, comment_index, stats)。

    任何失败（无 key/超时/坏 JSON/注入实现抛异常）都收敛为空列表 + reason，
    绝不向上抛、绝不阻塞采集。
    """
    stats = llm_extract_stats_defaults()
    stats["llm_extract_enabled"] = bool(getattr(config, "LLM_EXTRACT_ENABLED", False))
    if not stats["llm_extract_enabled"]:
        stats["llm_extract_reason"] = "disabled"
        return [], {}, stats

    comments, comment_index = _collect_llm_comments(docs, video_meta)
    stats["llm_extract_input"] = len(comments)
    if not comments:
        stats["llm_extract_reason"] = "empty_input"
        return [], comment_index, stats
    if extractor is None:
        stats["llm_extract_reason"] = "no_extractor"
        return [], comment_index, stats

    max_calls = int(getattr(config, "LLM_EXTRACT_MAX_CALLS", 3))
    max_comments = int(getattr(config, "LLM_EXTRACT_MAX_COMMENTS", 450))
    try:
        if extractor is DEFAULT_EXTRACTOR:
            report = run_extraction(comments, None, max_calls=max_calls,
                                    max_comments=max_comments)
        elif hasattr(extractor, "chat_json"):
            report = run_extraction(comments, extractor, max_calls=max_calls,
                                    max_comments=max_comments)
        elif callable(extractor):
            report = _coerce_extract_report(extractor(comments))
        else:
            raise TypeError("extractor 必须是 chat_json 客户端或 callable")
    except Exception as exc:  # noqa: BLE001 - 抽取失败只降级，不影响流水线
        stats["llm_extract_reason"] = "error"
        stats["llm_extract_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return [], comment_index, stats

    if not isinstance(report, dict):
        stats["llm_extract_reason"] = "error"
        stats["llm_extract_error"] = "extractor 返回的不是 dict"
        return [], comment_index, stats

    raw_candidates = report.get("candidates")
    if not isinstance(raw_candidates, (list, tuple)):
        raw_candidates = []
    candidates: list[dict] = []
    skipped = 0
    for item in raw_candidates:
        if isinstance(item, dict):
            candidates.append(item)
        else:
            skipped += 1

    stats["llm_extract_reason"] = str(
        report.get("reason") or ("ok" if candidates else "no_candidates")
    )
    stats["llm_extract_sampled"] = int(report.get("sampled") or 0)
    stats["llm_extract_calls"] = int(report.get("calls") or 0)
    stats["llm_extract_batches"] = int(report.get("batches") or 0)
    stats["llm_extract_cache_hits"] = int(report.get("cache_hits") or 0)
    stats["llm_extract_candidates"] = len(candidates)
    stats["llm_extract_skipped"] = skipped
    stats["llm_extract_details"] = [
        _llm_detail(item) for item in candidates[:LLM_EXTRACT_DETAIL_LIMIT]
    ]
    audit = report.get("llm")
    if isinstance(audit, dict):
        stats["llm_extract_prompt_tokens"] = int(audit.get("prompt_tokens") or 0)
        stats["llm_extract_completion_tokens"] = int(audit.get("completion_tokens") or 0)
        stats["llm_extract_total_tokens"] = int(audit.get("total_tokens") or 0)
    return candidates, comment_index, stats


def _llm_metadata(item: dict) -> dict:
    return {
        "source": "llm_extract",
        "type": str(item.get("type") or "词"),
        "meaning": str(item.get("meaning") or ""),
        "origin_quote": str(item.get("origin_quote") or ""),
        "evidence_ids": _evidence_id_list(item.get("evidence_ids")),
        "confidence": round(_float(item.get("confidence"), 0.0), 3),
        "known": bool(item.get("known")),
        "prompt_version": str(item.get("prompt_version") or ""),
    }


def _merge_llm_metadata(metas: list[dict]) -> dict:
    ordered = sorted(metas, key=lambda meta: _float(meta.get("confidence"), 0.0),
                     reverse=True)
    base = dict(ordered[0])
    base["source"] = (
        "n-gram+llm_extract"
        if any(meta.get("source") == "n-gram+llm_extract" for meta in ordered)
        else "llm_extract"
    )
    base["known"] = any(bool(meta.get("known")) for meta in ordered)
    base["confidence"] = max(_float(meta.get("confidence"), 0.0) for meta in ordered)
    for key in ("meaning", "origin_quote"):
        if not base.get(key):
            base[key] = next(
                (str(meta.get(key) or "") for meta in ordered if meta.get(key)), ""
            )
    evidence_ids: list[str] = []
    for meta in ordered:
        for eid in meta.get("evidence_ids") or []:
            value = str(eid)
            if value and value not in evidence_ids:
                evidence_ids.append(value)
    base["evidence_ids"] = evidence_ids[:5]
    base["type"] = str(base.get("type") or "词")
    return base


def merge_llm_candidates(candidates: list[Candidate], llm_items: list[dict],
                         comment_index: dict[str, dict]
                         ) -> tuple[list[Candidate], dict]:
    """把 LLM 候选按 normalize_surface(canonical) 合并进 discover 候选池。

    - 已存在（n-gram/种子）：只补 metadata，**不重复计数**；
    - 不存在：新增 Candidate，`count/df_video/df_zone` 由 evidence_ids 回映射评论计算；
    - 缺字段用默认值，任何坏项只跳过、不抛。
    """
    stats = {"merged": 0, "added": 0, "known": 0, "skipped": 0}
    index: dict[str, Candidate] = {}
    for candidate in candidates:
        key = normalize_surface(candidate.norm or candidate.surface)
        if key:
            index.setdefault(key, candidate)

    seen: set[str] = set()
    for item in llm_items or ():
        if not isinstance(item, dict):
            stats["skipped"] += 1
            continue
        surface = str(item.get("surface") or "").strip()
        canonical = str(item.get("canonical") or surface).strip() or surface
        key = normalize_surface(canonical) or normalize_surface(surface)
        if not surface or not key or key in seen or len(surface) > 20:
            stats["skipped"] += 1
            continue
        seen.add(key)
        meta = _llm_metadata(item)
        if meta["known"]:
            stats["known"] += 1

        existing = index.get(key)
        if existing is not None:
            old = getattr(existing, "metadata", None)
            if isinstance(old, dict):
                old["source"] = "n-gram+llm_extract"
                for field, value in meta.items():
                    if field == "source":
                        continue
                    if value not in ("", None, [], 0.0, False) or field not in old:
                        old[field] = value
                existing.metadata = old
            else:
                meta["source"] = "n-gram+llm_extract"
                existing.metadata = meta
            stats["merged"] += 1
            continue

        evidence = [
            comment_index[eid]
            for eid in meta["evidence_ids"]
            if eid in comment_index
        ]
        videos = {comment["bvid"] for comment in evidence if comment.get("bvid")}
        zones = {comment["zone"] for comment in evidence if comment.get("zone")}
        candidate = Candidate(
            surface=surface,
            norm=key,
            count=len(evidence),
            df_video=len(videos),
            df_zone=len(zones),
            kinds={"llm_extract"},
            zones=zones,
        )
        candidate.metadata = meta
        candidates.append(candidate)
        index[key] = candidate
        stats["added"] += 1
    return candidates, stats


def attach_llm_metadata(families: list[dict],
                        candidates: list[Candidate]) -> dict:
    """把候选 metadata 聚合到梗族（family["llm_extract"] / llm_known）。"""
    meta_by_key: dict[str, dict] = {}
    for candidate in candidates:
        meta = getattr(candidate, "metadata", None)
        if not isinstance(meta, dict) or not meta.get("source"):
            continue
        for value in (candidate.norm, candidate.surface):
            key = normalize_surface(value or "")
            if key:
                meta_by_key[key] = meta

    attached = 0
    known = 0
    for family in families:
        metas: list[dict] = []
        for member in family.get("members") or []:
            meta = meta_by_key.get(normalize_surface(str(member)))
            if meta is not None and meta not in metas:
                metas.append(meta)
        if not metas:
            continue
        merged = _merge_llm_metadata(metas)
        family["llm_extract"] = merged
        family["llm_known"] = bool(merged.get("known"))
        attached += 1
        known += 1 if family["llm_known"] else 0
    return {"attached": attached, "known": known}


def run_cycle(conn=None, client: BiliClient | None = None,
              reviewer=_DEFAULT_REVIEWER,
              extractor=DEFAULT_EXTRACTOR) -> dict:
    own_connection = conn is None
    conn = conn or store.connect()
    store.init_db(conn)
    started = time.time()
    cycle_id: int | None = None
    stats: dict = {**llm_stats_defaults(), **llm_extract_stats_defaults()}
    status = "done"

    try:
        cycle_id = store.start_cycle(conn)
        client = client or BiliClient()
        videos, zone_counts = fetch_videos(client)
        stats["videos"] = len(videos)
        stats["zones"] = zone_counts

        if not videos:
            raise RuntimeError("视频池为空，所有分区采集失败")

        now = store.now_iso()
        store.upsert_videos(conn, videos, now)
        store.insert_metrics(conn, cycle_id, videos)

        comment_targets, danmaku_targets = select_targets(videos)
        docs, collect_stats = collect_docs(
            client, conn, cycle_id, videos, comment_targets, danmaku_targets
        )
        stats.update(collect_stats)
        video_meta = {v["bvid"]: v for v in videos}

        # P5：先抽取（discover 之前），再按 normalize_surface 合并进候选池。
        # 无 key / HOTMEME_LLM_EXTRACT=0 / 失败 → 空列表 + reason，规则榜照旧。
        llm_candidates, comment_index, extract_stats = run_llm_extraction(
            docs, video_meta, extractor=extractor
        )
        stats.update(extract_stats)

        candidates = discover(docs)
        if llm_candidates:
            candidates, merge_stats = merge_llm_candidates(
                candidates, llm_candidates, comment_index
            )
            stats["llm_extract_merged"] = int(merge_stats.get("merged") or 0)
            stats["llm_extract_added"] = int(merge_stats.get("added") or 0)
            stats["llm_extract_known"] = int(merge_stats.get("known") or 0)
            stats["llm_extract_skipped"] = int(stats.get("llm_extract_skipped") or 0) + int(
                merge_stats.get("skipped") or 0
            )
        stats["candidates"] = len(candidates)
        if not candidates:
            raise RuntimeError("候选为空，语料不足或抽取失败")

        judger = Judger()
        families = merge_families(candidates, judger.seed_map, judger.alias_map)
        attach_stats = attach_llm_metadata(families, candidates)
        stats["llm_extract_known_families"] = int(attach_stats.get("known") or 0)
        usage = compute_usage(families, docs, video_meta)
        # P0 评论区优先：评论证据健康时才启用「无近 30 天评论证据」门控
        evidence_ready = (
            int(collect_stats.get("comments_recent", 0)) >= config.COMMENT_GATE_MIN_DOCS
        )
        stats["comment_evidence_ready"] = evidence_ready
        stats["comment_gate_checked"] = 0
        stats["comment_gate_rejected"] = 0
        stats["meme_gate_rejected"] = 0
        stats["comment_backed_count"] = 0
        stats["comment_backed_admitted"] = 0
        evidence_now = _now().timestamp()
        evidence_dt = datetime.fromtimestamp(evidence_now).astimezone()

        week_ago = (datetime.now().astimezone() - timedelta(days=config.WINDOW_DAYS)).isoformat(
            timespec="seconds"
        )
        owner_names = {normalize_surface(v.get("owner_name", "")) for v in videos if v.get("owner_name")}
        score_now = datetime.now().astimezone()
        first_seen = store.first_seen_map(conn, [f["family_key"] for f in families])

        for family in families:
            key = family["family_key"]
            bucket = usage.get(key) or {"videos": set(), "count": 0, "per_video": Counter()}
            evidence = comment_evidence(usage.get(key), evidence_now)
            # P1 novelty 与年龄：种子优先用种子表 origin 年份（已知梗年龄），
            # 非种子用窗口内最早评论；两者都进 components.raw 供 API/看板取用。
            origin_year = (
                parse_origin_year(family.get("origin", ""))
                if family.get("seed_hit") else None
            )
            evidence["seed_origin_year"] = origin_year
            evidence["known_age_years"] = known_age_years(origin_year, evidence_dt)
            evidence["novelty"] = novelty_score(evidence, origin_year, evidence_dt)
            family["comment"] = evidence
            video_ids = bucket["videos"]
            exposure = sum(
                math.log1p(max(0, video_meta[b].get("view", 0)))
                * float(video_meta[b].get("zone_weight", 1.0))
                for b in video_ids
            )
            engagement = sum(
                math.log1p(
                    max(0, video_meta[b].get("danmaku", 0))
                    + 2 * max(0, video_meta[b].get("reply", 0))
                    + max(0, video_meta[b].get("like", 0)) / 20.0
                )
                for b in video_ids
            )
            total_hits = bucket["count"]
            concentration = (
                max(bucket["per_video"].values()) / total_hits if total_hits else 0.0
            )
            past = sum(
                store.count_since(conn, member, week_ago)
                for member in family.get("members", [])[:5]
            )
            velocity = (family["count"] + 1.0) / (past / float(config.WINDOW_DAYS) + 1.0)
            family["signals"] = {
                "exposure": exposure,
                "engagement": engagement,
                "velocity": velocity,
                "concentration": concentration,
                "meme": 0.0,
                "novelty": evidence["novelty"],
            }
            family["first_seen"] = first_seen.get(key)
            family["is_new"] = key not in first_seen
            family["df_search"] = len(bucket.get("search_videos", ()))
            meme_score, reasons = judger.judge(
                family, {"owner_names": owner_names}
            )
            family["signals"]["meme"] = meme_score
            family["judge_reasons"] = reasons

        previous = store.previous_scores(conn, cycle_id)
        items = score_families(families, previous, score_now)

        by_key = {family["family_key"]: family for family in families}
        for item in items:
            key = item["family_key"]
            family = by_key.get(key, {})
            item["first_seen"] = family.get("first_seen")
            item["is_new"] = bool(family.get("is_new"))
            item["judge_reasons"] = family.get("judge_reasons", {})
            evidence = family.get("comment") or {}
            llm_meta = family.get("llm_extract")
            llm_known = bool(family.get("llm_known"))
            backed = comment_backed_candidate(item, evidence)
            # known（种子表命中）不走「评论证据绕过梗性闸门」的新梗旁路：
            # 它已有种子通道，避免老梗借评论证据挤进 new/observing。
            bypass = backed and not llm_known
            list_type = classify_list(
                bool(item.get("seed_hit")), evidence.get("seed_origin_year"),
                evidence.get("age_days"), evidence.get("comment_7d_count", 0),
            )
            item["comment_backed"] = backed
            item["list"] = list_type
            if llm_meta:
                item["llm_extract"] = llm_meta
                item["llm_known"] = llm_known
            # 评论证据进 components.raw（API 原样透传）：保留 P0 扁平字段，
            # 另加 comment_backed / comment_evidence / list 供 P1 新旧榜使用。
            raw = dict(evidence)
            raw["comment_backed"] = backed
            raw["comment_evidence"] = evidence
            raw["list"] = list_type
            if llm_meta:
                raw["llm_extract"] = llm_meta
                raw["llm_known"] = llm_known
            item.setdefault("raw_signals", {}).update(raw)
            item["components"]["comment"] = evidence
            item["components"]["comment_backed"] = backed
            item["components"]["list"] = list_type
            if llm_meta:
                item["components"]["llm_extract"] = llm_meta
                item["components"]["llm_known"] = llm_known
            meme_ok = passes_meme_gate(
                item["components"].get("meme", 0.0), item.get("seed_hit")
            )
            # P1 评论证据通道：非种子满足门槛可绕过 MEME_MIN，但只进 observing。
            if not meme_ok and not bypass:
                if llm_known and backed:
                    stats["llm_extract_known_blocked"] += 1
                item["status"] = "rejected"
                stats["meme_gate_rejected"] += 1
                continue
            stats["comment_backed_count"] += 1 if backed else 0
            stats["comment_gate_checked"] += 1
            gate_reason = comment_gate(item, evidence, evidence_ready)
            if gate_reason:
                item["status"] = "rejected"
                item["comment_gate"] = gate_reason
                stats["comment_gate_rejected"] += 1
                continue
            if not meme_ok:
                # 评论证据通道只给 observing，且不占主榜名次（只在 new 榜曝光），
                # 避免高频子串/常见短语借评论证据挤掉已过梗性闸门的条目。
                item["status"] = "observing"
                item["comment_admitted"] = True
                stats["comment_backed_admitted"] += 1
                continue
            streak = store.hype_streak(conn, key, config.MIN_SCORE, rounds=confirm_rounds())
            item["status"] = status_for(item, family, streak)

        # 排行池落 RANK_N 条（Top10 展示不变），供 /api/top?list=new|classic 过滤。
        ranked = [
            item for item in items
            if item["status"] in ("hot", "observing") and not item.get("comment_admitted")
        ][: config.RANK_N]
        for item in items:
            item["rank"] = 0
        for position, item in enumerate(ranked, start=1):
            item["rank"] = position
            if position > config.TOP_N:
                continue
            family = by_key.get(item["family_key"], {})
            item["sample"] = sample_text(item["canonical"], docs)
            if not item["sample"] and family.get("members"):
                item["sample"] = sample_text(family["members"][0], docs)
        display = ranked[: config.TOP_N]

        # P3 发布门：只审 list=new 的 top N；classic/all 用上面的规则榜，不受影响。
        gate_stats, gate_status = run_llm_gate(
            conn, cycle_id, items, by_key, docs, video_meta, usage, reviewer
        )
        stats.update(gate_stats)
        if gate_status == "degraded":
            status = "degraded"

        usable = [item for item in items if item["status"] != "rejected"]
        store.upsert_candidates(
            conn,
            [
                {"surface": c.surface, "norm": c.norm, "count": c.count,
                 "df_video": c.df_video, "df_zone": c.df_zone}
                for c in candidates
            ],
            cycle_id,
        )
        meme_ids = store.upsert_memes(
            conn,
            [
                {"family_key": f["family_key"], "canonical": f["canonical"],
                 "type": f.get("type", "词"), "origin": f.get("origin", "")}
                for f in families
            ],
        )
        for family in families:
            meme_id = meme_ids.get(family["family_key"])
            if meme_id:
                store.add_aliases(conn, meme_id, family.get("members", []))
        store.insert_signals(
            conn,
            cycle_id,
            [
                {
                    "meme_id": meme_ids[item["family_key"]],
                    "family_key": item["family_key"],
                    "score": item["score"],
                    "rank": item["rank"],
                    "status": item["status"],
                    "components": {
                        **item.get("components", {}),
                        "reasons": item.get("judge_reasons", {}),
                        "raw": item.get("raw_signals", {}),
                    },
                    "sample": item.get("sample", ""),
                    "is_new": bool(item.get("is_new")),
                }
                for item in usable
                if item["family_key"] in meme_ids
            ],
        )

        stats["families"] = len(families)
        stats["usable"] = len(usable)
        stats["top"] = [
            {"rank": item["rank"], "meme": item["canonical"], "score": item["score"]}
            for item in display
        ]
        stats["requests"] = client.http.request_count()
        calls = getattr(getattr(client, "http", None), "calls", None)
        stats["endpoint_counts"] = dict(calls or {})
        stats["elapsed"] = round(time.time() - started, 1)
        stats["errors"] = list(client.errors)

        non_comment_errors = [
            item for item in client.errors
            if not str(item.get("source", "")).startswith("reply:")
        ]
        if non_comment_errors or any(count == 0 for count in zone_counts.values()):
            status = "degraded"
        # P4：评论页失败率 >20% 才整轮降级；单页失败只计数，不把 cycle 标 degraded。
        if collect_stats.get("comment_degraded"):
            status = "degraded"
        store.finish_cycle(conn, cycle_id, status, stats)
        store.prune(conn)
    except BaseException as exc:  # noqa: BLE001 - 含 KeyboardInterrupt：先收尾再抛出
        if cycle_id is not None:
            store.record_error(conn, cycle_id, "pipeline", str(exc))
            stats["elapsed"] = round(time.time() - started, 1)
            stats["requests"] = getattr(getattr(client, "http", None), "request_count", lambda: 0)()
            store.finish_cycle(conn, cycle_id, "failed", stats)
        raise
    finally:
        if own_connection:
            conn.close()

    return snapshot(conn=conn if not own_connection else None)


def snapshot(conn=None) -> dict:
    own = conn is None
    conn = conn or store.connect()
    try:
        cycle = store.latest_cycle(conn)
        if not cycle:
            return {"status": "empty", "cycle": None, "top": [], "health": store.health(conn)}
        return {
            "status": cycle["status"],
            "cycle": cycle,
            "top": store.top_items(conn, cycle["id"], config.TOP_N),
            "health": store.health(conn),
        }
    finally:
        if own:
            conn.close()
