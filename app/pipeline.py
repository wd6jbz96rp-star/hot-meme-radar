"""采集 → 抽取 → 判定 → 打分 → 落库 的完整流水线。"""
from __future__ import annotations

import math
import random
import time
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import config, store
from .bili import BiliClient, parse_danmaku
from .extract import TextDoc, discover, merge_families, sample_text
from .judge import Judger, passes_meme_gate
from .normalize import normalize_surface
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
    danmaku_targets = _dedupe_videos(search_danmaku + danmaku_targets)[
        : config.DANMAKU_VIDEOS
    ]
    comment_targets = _dedupe_videos(search_comments + comment_targets)[
        : config.COMMENT_VIDEOS
    ]
    return comment_targets, danmaku_targets


def collect_docs(client: BiliClient, conn, cycle_id: int,
                 videos: list[dict], comment_targets: list[dict],
                 danmaku_targets: list[dict]) -> tuple[list[TextDoc], dict]:
    rng = random.Random(20261114)
    stats = {"comments": 0, "comments_recent": 0, "comments_7d": 0,
             "comment_dupes": 0, "comment_filtered_old": 0,
             "comment_modes": {str(mode): 0 for mode in config.COMMENT_MODES},
             "danmaku": 0, "danmaku_docs": 0, "danmaku_cached": 0,
             "comment_requests": 0, "comment_cached": 0, "pagelist_requests": 0}
    danmaku_docs: list[TextDoc] = []
    comment_docs: list[TextDoc] = []
    now_ts = _now().timestamp()
    recent_seconds = config.COMMENT_RECENT_DAYS * 86400

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
            cursor = 0
            for page in range(1, config.COMMENT_PAGES + 1):
                cached = store.get_comment_cache(conn, int(aid), mode, page)
                if cached is not None:
                    # 命中缓存也按当前 cycle 落 text_sample，统计口径不变
                    items = cached
                    stats["comment_cached"] += 1
                else:
                    items, cursor = client.comment_page(
                        int(aid), page=page, mode=mode, cursor=cursor
                    )
                    stats["comment_requests"] += 1
                    store.put_comment_cache(conn, int(aid), mode, page, items)
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
                if len(items) < config.COMMENT_PAGE_SIZE:
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


def run_cycle(conn=None, client: BiliClient | None = None) -> dict:
    own_connection = conn is None
    conn = conn or store.connect()
    store.init_db(conn)
    started = time.time()
    cycle_id: int | None = None
    stats: dict = {}
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

        candidates = discover(docs)
        stats["candidates"] = len(candidates)
        if not candidates:
            raise RuntimeError("候选为空，语料不足或抽取失败")

        judger = Judger()
        families = merge_families(candidates, judger.seed_map, judger.alias_map)

        video_meta = {v["bvid"]: v for v in videos}
        usage = compute_usage(families, docs, video_meta)
        # P0 评论区优先：评论证据健康时才启用「无近 30 天评论证据」门控
        evidence_ready = (
            int(collect_stats.get("comments_recent", 0)) >= config.COMMENT_GATE_MIN_DOCS
        )
        stats["comment_evidence_ready"] = evidence_ready
        stats["comment_gate_checked"] = 0
        stats["comment_gate_rejected"] = 0
        stats["meme_gate_rejected"] = 0
        evidence_now = _now().timestamp()

        week_ago = (datetime.now().astimezone() - timedelta(days=config.WINDOW_DAYS)).isoformat(
            timespec="seconds"
        )
        owner_names = {normalize_surface(v.get("owner_name", "")) for v in videos if v.get("owner_name")}
        score_now = datetime.now().astimezone()
        first_seen = store.first_seen_map(conn, [f["family_key"] for f in families])

        for family in families:
            key = family["family_key"]
            bucket = usage.get(key) or {"videos": set(), "count": 0, "per_video": Counter()}
            family["comment"] = comment_evidence(usage.get(key), evidence_now)
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
            # 评论证据进 components.raw（API 原样透传），同时留一份顶层便于看板取用
            item.setdefault("raw_signals", {}).update(evidence)
            item["components"]["comment"] = evidence
            if not passes_meme_gate(item["components"].get("meme", 0.0), item.get("seed_hit")):
                item["status"] = "rejected"
                stats["meme_gate_rejected"] += 1
                continue
            stats["comment_gate_checked"] += 1
            gate_reason = comment_gate(item, evidence, evidence_ready)
            if gate_reason:
                item["status"] = "rejected"
                item["comment_gate"] = gate_reason
                stats["comment_gate_rejected"] += 1
                continue
            streak = store.hype_streak(conn, key, config.MIN_SCORE, rounds=confirm_rounds())
            item["status"] = status_for(item, family, streak)

        display = [item for item in items if item["status"] in ("hot", "observing")][
            : config.TOP_N
        ]
        for item in items:
            item["rank"] = 0
        for position, item in enumerate(display, start=1):
            item["rank"] = position
            family = by_key.get(item["family_key"], {})
            item["sample"] = sample_text(item["canonical"], docs)
            if not item["sample"] and family.get("members"):
                item["sample"] = sample_text(family["members"][0], docs)

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

        if client.errors or any(count == 0 for count in zone_counts.values()):
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
