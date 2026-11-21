"""B 线（Phase B）测试：候选发现 / 在线指标 / 45+10 选片 / 写库。

无 ``TEST_DATABASE_URL`` 时只跑纯单元测试；有则额外跑 Postgres 集成测试
（写库测试在事务内执行并回滚，不污染生产数据）。
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from meme_info.bili import (
    parse_newlist,
    parse_online_count,
    parse_online_total,
    parse_related,
)
from meme_info.models import Video, VideoCandidate, VideoMetric
from meme_info.video_route import (
    DEFAULT_ZONES,
    HOT_WEIGHTS,
    MIN_DELTA_MINUTES,
    PreviousMetric,
    VideoRoutePlan,
    age_minutes,
    compute_metrics,
    discover_candidates,
    expand_related,
    load_previous_metrics,
    measure_candidates,
    median_mad,
    persist_result,
    robust_z,
    run_video_route,
    select_targets,
)

DB_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not DB_URL, reason="需要 TEST_DATABASE_URL 才能跑 Postgres 集成测试"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
NOW = int(time.mktime(time.strptime("2026-11-21 12:00:00", "%Y-%m-%d %H:%M:%S")))
ZONE_NAMES = {
    119: "鬼畜", 160: "生活", 1: "动画", 4: "游戏",
    181: "影视", 3: "音乐", 5: "娱乐", 36: "知识",
}


# ================================================================ 测试工具
def _video(
    bvid: str,
    source: str,
    *,
    zone: str = "",
    age_minutes_value: float = 120.0,
    view: int = 10_000,
    reply: int = 100,
    danmaku: int = 50,
    cid: int | None = None,
    title: str = "",
) -> Video:
    return Video(
        bvid=bvid,
        aid=abs(hash(bvid)) % 1_000_000_000,
        cid=cid if cid is not None else abs(hash(bvid + "cid")) % 10_000_000 + 1,
        title=title or f"标题 {bvid}",
        pubdate=NOW - int(age_minutes_value * 60),
        view=view,
        reply=reply,
        danmaku=danmaku,
        zone=zone,
        source=source,
        source_rank=1,
    )


class FakeSource:
    """可注入的假 BiliSource：记录调用次数，便于断言来源覆盖。"""

    def __init__(self) -> None:
        self.rankings: dict[int, list[Video]] = {}
        self.popular_videos: list[Video] = []
        self.search_results: dict[str, list[Video]] = {}
        self.recent_results: dict[str, list[Video]] = {}
        self.newlists: dict[int, list[Video]] = {}
        self.related_results: dict[str, list[Video]] = {}
        self.views: dict[str, Video] = {}
        self.online: dict[str, int | None] = {}
        self.calls: dict[str, int] = {}

    def _call(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def ranking(self, rid: int) -> list[Video]:
        self._call(f"ranking:{rid}")
        return list(self.rankings.get(rid, []))

    def popular(self, size: int = 50) -> list[Video]:
        self._call("popular")
        return list(self.popular_videos)[:size]

    def search_videos(self, keyword: str, limit: int = 20, page: int = 1,
                      order: str = "totalrank") -> list[Video]:
        self._call(f"search:{order}:{keyword}")
        data = self.search_results if order == "totalrank" else self.recent_results
        return list(data.get(keyword, []))[:limit]

    def search_recent(self, keyword: str, limit: int = 20, page: int = 1) -> list[Video]:
        return self.search_videos(keyword, limit=limit, page=page, order="pubdate")

    def newlist(self, rid: int, size: int = 20, page: int = 1) -> list[Video]:
        self._call(f"newlist:{rid}")
        return list(self.newlists.get(rid, []))[:size]

    def related(self, bvid: str, limit: int = 40) -> list[Video]:
        self._call(f"related:{bvid}")
        return list(self.related_results.get(bvid, []))[:limit]

    def view(self, bvid: str) -> Video | None:
        self._call(f"view:{bvid}")
        return self.views.get(bvid)

    def online_total(self, bvid: str, cid: int | None) -> int | None:
        self._call("online")
        return self.online.get(bvid)


def build_full_source() -> FakeSource:
    """完整一轮的数据量：8 分区榜 + 热门 + 10 搜索 + newlist + 发布时间搜索。"""
    source = FakeSource()
    for rid in DEFAULT_ZONES:
        zone = ZONE_NAMES[rid]
        source.rankings[rid] = [
            _video(
                f"BVR{rid}X{i:03d}",
                f"ranking:{rid}",
                zone=zone,
                view=5_000 + i * 100,
                reply=100 + i * 3,
                danmaku=40 + i,
            )
            for i in range(30)
        ]
    # 热门：20 个与分区榜重叠 + 40 个新
    source.popular_videos = [
        _video(f"BVR119X{i:03d}", "popular", view=9_000 + i, reply=200 + i, danmaku=60 + i)
        for i in range(20)
    ] + [
        _video(f"BVPOP{i:03d}", "popular", view=8_000 + i, reply=150 + i, danmaku=70 + i)
        for i in range(40)
    ]
    # 搜索：10 组 × 20，组内唯一，前 10 条与分区榜重叠
    source.search_results = {}
    search_queries = tuple(
        query.replace("{year}", "2026") for query in VideoRoutePlan().search_queries
    )
    for qi, keyword in enumerate(search_queries):
        source.search_results[keyword] = [
            _video(
                f"BVR160X{i:03d}" if qi == 0 and i < 10 else f"BVS{qi:02d}{i:03d}",
                f"search:{keyword}",
                view=3_000 + i,
                reply=30 + i,
                danmaku=10 + i,
            )
            for i in range(20)
        ]
    # newlist：8 分区 × 15 个新视频（stat 为 0，需补 view）
    for rid in DEFAULT_ZONES:
        source.newlists[rid] = [
            _video(
                f"BVN{rid}Z{i:03d}",
                f"newlist:{rid}",
                zone=ZONE_NAMES[rid],
                age_minutes_value=20,
                view=0,
                reply=0,
                danmaku=0,
            )
            for i in range(15)
        ]
    # 按发布时间搜索：3 组 × 15 个新视频
    source.recent_results = {
        keyword: [
            _video(
                f"BVT{qi:02d}{i:03d}",
                f"recent-search:{keyword}",
                age_minutes_value=30,
                view=0,
                reply=0,
                danmaku=0,
            )
            for i in range(15)
        ]
        for qi, keyword in enumerate(("热梗", "网络热梗", "梗百科"))
    }
    return source


def build_small_source(count: int = 5, *, online_base: int = 500) -> FakeSource:
    source = FakeSource()
    source.rankings[119] = [
        _video(f"BVSMALL{i:03d}", "ranking:119", zone="鬼畜", view=2_000 + i * 300,
               reply=20 + i, danmaku=10 + i)
        for i in range(count)
    ]
    source.online = {f"BVSMALL{i:03d}": online_base + i * 100 for i in range(count)}
    return source


def _metric(
    bvid: str,
    hot: float,
    *,
    zone: str,
    exploration: bool = False,
    danmaku_z: float = 0.0,
) -> VideoMetric:
    return VideoMetric(
        bvid=bvid,
        zone=zone,
        hot_score=hot,
        danmaku_z=danmaku_z,
        exploration=exploration,
        exploration_source="newlist:119" if exploration else "",
    )


# ================================================================ B站解析
def test_parse_online_total_variants() -> None:
    assert parse_online_count("9000+") == 9000
    assert parse_online_count("1.2万") == 12000
    assert parse_online_count(321) == 321
    assert parse_online_count(None) is None
    assert parse_online_count("abc") is None
    assert parse_online_total({"data": {"count": "9000+", "total": "12000"}}) == 9000
    assert parse_online_total({"data": {"total": "777"}}) == 777
    assert parse_online_total({"data": {"count": "", "total": ""}}) is None
    assert parse_online_total({"code": -404}) is None


def test_parse_newlist_and_related() -> None:
    newlist_payload = {
        "code": 0,
        "data": {
            "archives": [
                {"bvid": "BVNEW1", "cid": 123, "title": "新视频", "pubdate": 100,
                 "stat": {"view": 0, "reply": 0, "danmaku": 0}},
                {"bvid": "", "title": "无 bvid 应丢弃"},
            ]
        },
    }
    videos = parse_newlist(newlist_payload, 119)
    assert len(videos) == 1
    assert videos[0].bvid == "BVNEW1"
    assert videos[0].cid == 123
    assert videos[0].source == "newlist:119"
    assert videos[0].zone == "鬼畜"

    related_payload = {
        "code": 0,
        "data": [
            {"bvid": "BVREL1", "cid": 9, "title": "相关", "pubdate": 50,
             "stat": {"view": 10, "reply": 2, "danmaku": 1}},
        ],
    }
    related = parse_related(related_payload, "BVSEED")
    assert len(related) == 1
    assert related[0].source == "related:BVSEED"
    assert related[0].reply == 2
    assert parse_related({"data": None}) == []


# ================================================================ 纯计算
def test_age_minutes_and_fallbacks() -> None:
    assert age_minutes(NOW - 3600, NOW) == pytest.approx(60.0)
    assert age_minutes(NOW, NOW) == 1.0  # 下限 1 分钟
    assert age_minutes(0, NOW) == 1380.0  # pubdate 缺失兜底


def test_median_mad_and_robust_z() -> None:
    med, mad = median_mad([1, 2, 3, 4, 5])
    assert med == 3
    assert mad == 1
    z = robust_z([1, 2, 3, 4, 5])
    assert z[2] == pytest.approx(0.0)
    assert z[0] == pytest.approx((1 - 3) / (1.4826 * 1))
    assert all(abs(value) <= 3.0 for value in robust_z([1, 1000]))
    assert robust_z([]) == []
    assert robust_z([5, 5, 5]) == [0.0, 0.0, 0.0]
    assert robust_z([None, None]) == [0.0, 0.0]
    mixed = robust_z([None, 10, 12, 14])
    assert mixed[0] == 0.0


def test_compute_metrics_first_seen_play_proxy() -> None:
    # age=60min → view/(60+60)=view/120
    candidate = VideoCandidate(
        bvid="BV1", pubdate=NOW - 3600, view=6000, reply=120, danmaku=60,
        online=900, stats_known=True, zone="鬼畜",
    )
    metrics = compute_metrics([candidate], previous={}, now=NOW)
    metric = metrics[0]
    assert metric.first_seen is True
    assert metric.new_view is None and metric.new_reply is None
    assert metric.play_rate == pytest.approx(6000 / 120)
    assert metric.play_proxy == pytest.approx(math.log1p(6000 / 120))
    assert metric.comment_rate == pytest.approx(120 / 120)
    assert metric.danmaku_rate == pytest.approx(60 / 120)
    assert metric.hot_score == 0.0  # 单候选无方差 → z 全 0


def test_compute_metrics_second_seen_and_dt_rule() -> None:
    candidate = VideoCandidate(
        bvid="BV1", pubdate=NOW - 3600, view=2500, reply=50, danmaku=20,
        online=500, stats_known=True,
    )
    previous = {"BV1": PreviousMetric(bvid="BV1", view=1000, reply=20, danmaku=5,
                                     measured_at=NOW - 900)}
    metric = compute_metrics([candidate], previous=previous, now=NOW)[0]
    assert metric.first_seen is False
    assert metric.dt_minutes == pytest.approx(15.0)
    assert metric.new_view == 1500
    assert metric.new_reply == 30
    assert metric.new_danmaku == 15
    assert metric.play_rate == pytest.approx(1500 / 15)
    assert metric.comment_rate == pytest.approx(30 / 15)

    # view 下降按 0 处理
    dropped = VideoCandidate(bvid="BV1", pubdate=NOW - 3600, view=500,
                            reply=10, danmaku=2, online=1, stats_known=True)
    metric2 = compute_metrics([dropped], previous=previous, now=NOW)[0]
    assert metric2.new_view == 0

    # dt < 5 分钟：退回首见年龄归一化公式
    short_previous = {"BV1": PreviousMetric(bvid="BV1", view=1000, reply=20,
                                            danmaku=5, measured_at=NOW - 120)}
    metric3 = compute_metrics([candidate], previous=short_previous, now=NOW)[0]
    assert metric3.dt_minutes == pytest.approx(2.0)
    assert MIN_DELTA_MINUTES == 5.0
    assert metric3.play_rate == pytest.approx(2500 / (age_minutes(NOW - 3600, NOW) + 60))


def test_compute_metrics_hot_weights_and_missing_online() -> None:
    candidates = [
        VideoCandidate(bvid=f"BV{i}", pubdate=NOW - 3600, view=1000 * (i + 1),
                       reply=10 * (i + 1), danmaku=5 * (i + 1),
                       online=(1000 * (i + 1) if i < 3 else None), stats_known=True,
                       zone="鬼畜")
        for i in range(4)
    ]
    metrics = compute_metrics(candidates, previous={}, now=NOW)
    assert metrics[3].online is None
    assert metrics[3].online_z == 0.0  # 缺失按中位数处理
    for metric in metrics:
        expected = (
            HOT_WEIGHTS["online"] * metric.online_z
            + HOT_WEIGHTS["new_comments"] * metric.comment_z
            + HOT_WEIGHTS["new_danmaku"] * metric.danmaku_z
            + HOT_WEIGHTS["play_proxy"] * metric.play_z
        )
        assert metric.hot_score == pytest.approx(expected, abs=1e-3)
    assert sum(HOT_WEIGHTS.values()) == pytest.approx(1.0)


# ================================================================ 候选发现
def test_dedupe_by_bvid_records_all_sources() -> None:
    source = FakeSource()
    source.rankings[119] = [
        _video(f"BV{i}", "ranking:119", zone="鬼畜") for i in range(10)
    ]
    source.popular_videos = [
        _video(f"BV{i}", "popular") for i in range(5)
    ] + [_video(f"BVP{i}", "popular") for i in range(5)]
    source.search_results = {"热梗": [_video("BV0", "search:热梗")] +
                             [_video(f"BVS{i}", "search:热梗") for i in range(5)]}
    plan = VideoRoutePlan(
        candidate_target=20, routine_target=16, exploration_target=4,
        enable_newlist=False, enable_recent_search=False, enable_related=False,
    )
    candidates, stats = discover_candidates(source, plan, now=NOW)
    bvids = [candidate.bvid for candidate in candidates]
    assert len(bvids) == len(set(bvids)), "候选必须按 bvid 去重"
    merged = next(candidate for candidate in candidates if candidate.bvid == "BV0")
    assert merged.sources[:3] == ["ranking:119", "popular", "search:热梗"]
    assert stats["candidates"] == len(candidates) <= 20


def test_discovery_full_pool_composition_240_60() -> None:
    source = build_full_source()
    candidates, stats = discover_candidates(source, VideoRoutePlan(), now=NOW)
    # 300 = 240 常规 + 40 探索发现 + 20 相关推荐预留（第二阶段补足）
    assert stats["candidates"] == 280
    assert stats["routine"] == 240
    assert stats["exploration"] == 40
    assert stats["discovery_target"] == 280
    assert stats["related_reserve"] == 20
    assert sum(stats["exploration_sources"].values()) == 40
    assert any(key.startswith("newlist:") for key in stats["exploration_sources"])
    assert any(key.startswith("recent-search:") for key in stats["exploration_sources"])
    bvids = [candidate.bvid for candidate in candidates]
    assert len(bvids) == len(set(bvids))
    # 覆盖 8 个分区榜 + 热门 + 搜索（计数至少覆盖来源种类）
    for rid in DEFAULT_ZONES:
        assert source.calls.get(f"ranking:{rid}", 0) >= 1
    assert source.calls.get("popular", 0) == 1
    assert source.calls.get("newlist:119", 0) == 1


def test_related_expansion_fills_candidate_pool_to_300() -> None:
    source = build_full_source()
    plan = VideoRoutePlan()
    candidates, stats = discover_candidates(source, plan, now=NOW)
    assert len(candidates) == 280
    # 只有带 online 的候选会被当作相关推荐种子
    for index, candidate in enumerate(candidates[:5]):
        candidate.online = 1000 - index
        source.related_results[candidate.bvid] = [
            _video(f"BVREL{index}{j}", f"related:{candidate.bvid}", view=500, reply=5, danmaku=2)
            for j in range(8)
        ]
    added = expand_related(source, candidates, plan, now=NOW)
    assert len(added) == 20
    assert len(candidates) == 300
    assert all(candidate.exploration for candidate in added)
    assert all(candidate.exploration_source.startswith("related:") for candidate in added)
    assert source.calls.get(f"related:{candidates[0].bvid}", 0) == 1


def test_zone_weights_affect_routine_quota() -> None:
    source = build_full_source()
    plan = VideoRoutePlan(zone_weights={rid: 1.0 for rid in DEFAULT_ZONES})
    plan.zone_weights[119] = 10.0
    candidates, stats = discover_candidates(source, plan, now=NOW)
    by_zone = stats["zone_counts"]
    assert by_zone["鬼畜"] > by_zone["音乐"]
    assert stats["routine"] == 240


def test_limited_mode_stops_discovery_early() -> None:
    source = build_full_source()
    plan = VideoRoutePlan(max_candidates=5)
    candidates, stats = discover_candidates(source, plan, now=NOW)
    assert len(candidates) == 5
    assert stats["mode"] == "limited"
    assert source.calls.get("ranking:119", 0) == 1
    assert source.calls.get("popular", 0) == 0  # 已够 5 个，提前停止
    assert source.calls.get("newlist:119", 0) == 0


# ================================================================ 测量
def test_measure_enriches_and_queries_online_once_per_candidate() -> None:
    source = FakeSource()
    candidates = [
        VideoCandidate(bvid=f"BV{i}", cid=None, stats_known=False, source="search:热梗")
        for i in range(3)
    ]
    source.views = {
        f"BV{i}": _video(f"BV{i}", "view", view=100 + i, reply=10, danmaku=5, cid=900 + i)
        for i in range(3)
    }
    source.online = {"BV0": 100, "BV1": None, "BV2": 300}
    plan = VideoRoutePlan(enrich_budget=10)
    stats = measure_candidates(source, candidates, plan, now=NOW)
    assert stats["enriched"] == 3
    assert source.calls.get("online", 0) == 3
    assert stats["online_ok"] == 2
    assert stats["online_failed"] == 1
    assert stats["online_coverage"] == pytest.approx(2 / 3, abs=1e-3)
    assert candidates[0].online == 100
    assert candidates[1].error == "online"
    assert all(candidate.cid is not None and candidate.stats_known for candidate in candidates)


def test_measure_respects_enrich_budget() -> None:
    source = FakeSource()
    candidates = [
        VideoCandidate(bvid=f"BV{i}", cid=None, stats_known=False) for i in range(3)
    ]
    source.online = {f"BV{i}": 10 for i in range(3)}
    source.views = {"BV0": _video("BV0", "view", cid=999)}
    stats = measure_candidates(source, candidates, VideoRoutePlan(enrich_budget=1), now=NOW)
    assert stats["enrich_attempts"] == 1
    assert stats["enriched"] == 1
    assert stats["no_cid"] == 2


# ================================================================ 选片
def test_select_comment_45_with_zone_cap_and_danmaku_10() -> None:
    metrics = [
        _metric(f"BV{i:03d}", hot=300 - i, zone=list(ZONE_NAMES.values())[i % 8],
                danmaku_z=float(i % 10))
        for i in range(300)
    ]
    comment, danmaku = select_targets(metrics, VideoRoutePlan())
    assert len(comment) == 45
    assert len(danmaku) == 10
    zone_counts: dict[str, int] = {}
    for metric in metrics:
        if metric.selected_comment:
            zone_counts[metric.zone] = zone_counts.get(metric.zone, 0) + 1
    assert max(zone_counts.values()) <= 18  # 45 * 40% = 18
    assert all(any(m.bvid == bvid for m in metrics) for bvid in comment + danmaku)


def test_select_comment_enforces_single_zone_cap() -> None:
    metrics = [_metric(f"BVA{i:03d}", hot=300 - i, zone="鬼畜") for i in range(60)]
    metrics += [_metric(f"BVB{i:03d}", hot=10 - i * 0.01, zone="生活") for i in range(20)]
    metrics += [_metric(f"BVC{i:03d}", hot=5 - i * 0.01, zone="游戏") for i in range(20)]
    stats: dict = {}
    comment, _ = select_targets(metrics, VideoRoutePlan(), stats=stats)
    assert len(comment) == 45
    zone_a = [m for m in metrics if m.selected_comment and m.zone == "鬼畜"]
    assert len(zone_a) == 18
    assert stats["comment_max_zone"] == 18


def test_select_comment_zone_cap_limited() -> None:
    metrics = [_metric(f"BVA{i:03d}", hot=300 - i, zone="鬼畜") for i in range(60)]
    stats: dict = {}
    comment, _ = select_targets(metrics, VideoRoutePlan(), stats=stats)
    assert len(comment) == 18
    assert stats["comment_zone_cap_limited"] is True


def test_select_uses_exploration_slots() -> None:
    metrics = [
        _metric(f"BVH{i:03d}", hot=300 - i, zone=list(ZONE_NAMES.values())[i % 8])
        for i in range(100)
    ]
    metrics += [
        _metric(f"BVE{i:02d}", hot=0.1 - i * 0.001,
                zone=list(ZONE_NAMES.values())[i % 8], exploration=True)
        for i in range(30)
    ]
    comment, danmaku = select_targets(metrics, VideoRoutePlan())
    explore_selected = [m for m in metrics if m.selected_comment and m.exploration]
    assert len(comment) == 45
    assert len(explore_selected) == 10  # 10 个探索配额
    assert len(danmaku) == 10
    # 弹幕 2 个探索配额
    assert sum(1 for m in metrics if m.selected_danmaku and m.exploration) >= 2


# ================================================================ 一轮编排
def test_no_db_run_returns_result_without_writing() -> None:
    source = build_small_source(5)
    plan = VideoRoutePlan(max_candidates=5)
    result = run_video_route(source, plan, now=NOW, use_db=False)
    assert result.round_id is None
    assert result.stats["db_written"] is False
    assert len(result.candidates) == 5
    assert len(result.metrics) == 5
    assert len(result.selected_comment) == 5
    assert len(result.selected_danmaku) == 5
    assert result.window_end == NOW
    assert result.window_start == NOW - 15 * 60
    assert all(metric.online is not None for metric in result.metrics)
    assert result.stats["online_coverage"] == 1.0
    # candidates > 0 且 online 覆盖 100% → ok
    assert result.status == "ok"


def test_run_video_route_uses_injected_previous_baseline() -> None:
    source = build_small_source(5)
    previous = {
        f"BVSMALL{i:03d}": PreviousMetric(bvid=f"BVSMALL{i:03d}", view=1_000,
                                         reply=10, danmaku=5, measured_at=NOW - 900)
        for i in range(5)
    }
    result = run_video_route(
        source, VideoRoutePlan(max_candidates=5), now=NOW, use_db=False, previous=previous
    )
    assert result.stats["baseline"]["count"] == 5
    assert result.stats["baseline"]["first_seen"] == 0
    assert all(not metric.first_seen for metric in result.metrics)
    assert all(metric.new_view is not None for metric in result.metrics)


def test_cli_no_db_offline() -> None:
    completed = subprocess.run(
        [
            sys.executable, "-m", "tools.video_route",
            "--limit", "5", "--no-db", "--offline", "--json",
            "--fixture-dir", str(FIXTURES),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["round_id"] is None
    assert len(payload["candidates"]) <= 5
    assert len(payload["metrics"]) == len(payload["candidates"])
    assert payload["stats"]["db_written"] is False


def test_cli_rejects_missing_fixture_dir() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "tools.video_route", "--no-db", "--offline",
         "--fixture-dir", str(REPO_ROOT / "no-such-dir")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "fixture" in completed.stderr


# ================================================================ Postgres 集成（事务回滚）
class _Rollback(Exception):
    """用异常让 state.transaction 回滚，保证测试不污染生产数据。"""


@requires_db
def test_persist_and_previous_metrics_rollback() -> None:
    from meme_info import state

    plan = VideoRoutePlan(max_candidates=5)
    source_round1 = build_small_source(5, online_base=500)
    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            before = conn.execute("SELECT count(*) FROM round").fetchone()[0]
            result1 = run_video_route(
                source_round1, plan, now=NOW, use_db=True,
                database_url=DB_URL, db_conn=conn,
            )
            assert result1.round_id is not None
            assert result1.stats["db_written"] is True
            stored = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE hot_score IS NOT NULL) "
                "FROM video_metric WHERE round_id = %s",
                (result1.round_id,),
            ).fetchone()
            assert stored[0] == len(result1.metrics) == 5
            assert stored[1] == 5  # hot_score 均已写入
            round_row = conn.execute(
                "SELECT status, videos_sampled, comments_sampled, danmaku_sampled, stats "
                "FROM round WHERE id = %s",
                (result1.round_id,),
            ).fetchone()
            assert round_row[0] == "ok"
            assert round_row[1] == 5
            assert round_row[2] == 0 and round_row[3] == 0
            round_stats = round_row[4]
            if isinstance(round_stats, (str, bytes, bytearray)):
                round_stats = json.loads(round_stats)
            assert round_stats["db_written"] is True

            # 第二轮：同一批 bvid 有上一轮基线 → 计算新增量
            source_round2 = build_small_source(5, online_base=900)
            for index in range(5):
                bvid = f"BVSMALL{index:03d}"
                source_round2.rankings[119][index].view += 1_000
                source_round2.rankings[119][index].reply += 10
                source_round2.rankings[119][index].danmaku += 5
            result2 = run_video_route(
                source_round2, plan, now=NOW + 900, use_db=True,
                database_url=DB_URL, db_conn=conn,
            )
            assert result2.stats["baseline"]["count"] == 5
            assert result2.stats["baseline"]["second_seen"] == 5
            assert all(metric.new_view is not None for metric in result2.metrics)

            # 7 天窗口：8 天前的基线必须被忽略
            old_round = conn.execute(
                "INSERT INTO round (started_at, window_start, window_end, status) "
                "VALUES (now() - interval '8 days', now() - interval '8 days' - interval '15 minutes', "
                "now() - interval '8 days', 'ok') RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO video_metric (round_id, bvid, view, reply, danmaku) "
                "VALUES (%s, 'BVOLD7', 100, 1, 1)",
                (old_round,),
            )
            assert "BVOLD7" not in load_previous_metrics(
                ["BVOLD7"], now=int(time.time()), lookback_days=7, conn=conn
            )
            assert "BVOLD7" in load_previous_metrics(
                ["BVOLD7"], now=int(time.time()), lookback_days=9, conn=conn
            )
            assert conn.execute("SELECT count(*) FROM round").fetchone()[0] > before
            raise _Rollback

    with state.connection(DB_URL) as conn:
        after = conn.execute("SELECT count(*) FROM round").fetchone()[0]
        residue = conn.execute(
            "SELECT count(*) FROM video_metric WHERE bvid = 'BVOLD7'"
        ).fetchone()[0]
    assert residue == 0, "测试行必须已回滚"
    assert after == before
