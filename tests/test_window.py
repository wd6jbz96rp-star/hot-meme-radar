"""Phase C 测试：15 分钟窗口采样 / 去重 / 配额 / 证据分离 / CLI / Postgres。

无 ``TEST_DATABASE_URL`` 时只跑纯单元测试；有则额外跑 Postgres 集成测试
（在事务内执行并回滚，不污染生产数据）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from meme_info.bili import parse_comment_page, parse_danmaku_xml
from meme_info.models import Comment, Video, VideoMetric, VideoRouteResult, WindowTarget
from meme_info.text import normalize_surface
from meme_info.window import (
    WindowSamplePlan,
    per_video_cap,
    round_robin_truncate,
    run_window_sample,
    targets_from_payload,
)

DB_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not DB_URL, reason="需要 TEST_DATABASE_URL 才能跑 Postgres 集成测试"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
#: 固定基准时间（不依赖本地时区/当前时间，窗口可复算）
NOW = 1_800_000_000
WINDOW_START = NOW - 15 * 60
WINDOW_END = NOW
DAY = 86400


# ================================================================ 测试工具
def _comment(
    bvid: str,
    rpid: int,
    ctime: int,
    text: str,
    *,
    like: int = 0,
    mode: int = 2,
) -> Comment:
    return Comment(
        id=f"{bvid}:{rpid}",
        bvid=bvid,
        text=text,
        ctime=ctime,
        like=like,
        mode=mode,
    )


def _target(
    bvid: str,
    *,
    aid: int = 123,
    cid: int | None = 456,
    source: str = "injected",
) -> WindowTarget:
    return WindowTarget(
        bvid=bvid,
        aid=aid,
        cid=cid,
        title=f"标题 {bvid}",
        source=source,
    )


def _danmaku_xml(entries) -> str:
    """按真实 XML 格式生成弹幕：p 第 5 段 ctime、第 8 段 dmid。"""
    parts = []
    for ctime, dmid, progress, text in entries:
        parts.append(
            f'<d p="{progress:.2f},1,25,16777215,{ctime},0,abc123,{dmid},10">{text}</d>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><i>'
        "<chatserver>chat.bilibili.com</chatserver>" + "".join(parts) + "</i>"
    )


class _Http:
    def __init__(self) -> None:
        self.requests = 0
        self.cache_hits = 0


class FakeSource:
    """最小 BiliSource 接口：评论按 cursor 偏移分页，弹幕按 cid 返回 XML。"""

    def __init__(self, comment_store=None, danmaku_store=None, view_store=None):
        self.http = _Http()
        self.comment_store: dict[tuple[str, int], list[Comment]] = comment_store or {}
        self.danmaku_store: dict[int, str] = danmaku_store or {}
        self.view_store: dict[str, Video] = view_store or {}
        self.comment_calls: list[tuple[str, int, int]] = []
        self.danmaku_calls: list[int] = []

    def view(self, bvid: str):
        self.http.requests += 1
        return self.view_store.get(bvid)

    def cid_for(self, bvid: str):
        self.http.requests += 1
        video = self.view_store.get(bvid)
        return video.cid if video else None

    def comments(
        self,
        aid: int,
        *,
        bvid: str = "",
        mode: int = 3,
        cursor: int = 0,
        page_size: int = 20,
        zone: str = "",
        video_title: str = "",
        include_top: bool = True,
    ):
        self.http.requests += 1
        self.comment_calls.append((bvid, mode, int(cursor)))
        items = self.comment_store.get((bvid, mode), [])
        start = max(0, int(cursor))
        page = items[start : start + page_size]
        next_cursor = start + page_size if start + page_size < len(items) else 0
        return page, next_cursor, next_cursor == 0

    def danmaku(self, cid: int) -> str:
        self.http.requests += 1
        self.danmaku_calls.append(int(cid))
        return self.danmaku_store.get(int(cid), "")


# ================================================================ 评论窗口
def test_comment_window_filter_and_page_stop() -> None:
    """窗口外评论过滤；翻到 window_start 之前即停，不再消耗请求。"""
    bvid = "BVW1"
    comments = [
        _comment(bvid, i, NOW - i, f"新评论{i}") for i in range(1, 26)
    ]  # 25 条窗口内
    comments += [
        _comment(bvid, 100 + i, NOW - 3600 - i * 60, f"旧评论{i}") for i in range(10)
    ]
    fake = FakeSource(comment_store={(bvid, 2): comments})
    sample = run_window_sample(
        fake,
        WindowSamplePlan(),
        now=NOW,
        comment_targets=[_target(bvid)],
        danmaku_targets=[],
        use_db=False,
    )
    assert len(sample.comments) == 25
    assert all(WINDOW_START <= item.ctime <= WINDOW_END for item in sample.comments)
    assert sample.stats["comment_out_of_window"] == 10
    # 只取 15 分钟窗口阶段（证据阶段在 mode3 之后，会再次翻 mode2）
    window_calls = []
    for call in fake.comment_calls:
        if call[1] == 3:
            break
        window_calls.append(call)
    assert [call[2] for call in window_calls] == [0, 20], "第二页已越过窗口起点，必须停止"
    assert sample.stats["comment_stop"][bvid] == "reached_window_start"


def test_comment_cursor_end_and_no_progress_stop() -> None:
    """cursor 为 0 / 不再前进时停止，不会死循环。"""
    bvid = "BVW2"
    fake = FakeSource(
        comment_store={(bvid, 2): [_comment(bvid, i, NOW - i, f"评论{i}") for i in range(1, 6)]}
    )
    sample = run_window_sample(
        fake,
        WindowSamplePlan(),
        now=NOW,
        comment_targets=[_target(bvid)],
        danmaku_targets=[],
        use_db=False,
    )
    assert len(sample.comments) == 5
    window_calls = []
    for call in fake.comment_calls:
        if call[1] == 3:
            break
        window_calls.append(call)
    assert len(window_calls) == 1
    assert sample.stats["comment_stop"][bvid] == "cursor_end"


def _dedupe_store() -> dict:
    bvid_a, bvid_b = "BVC1", "BVC2"
    comments_a = [
        _comment(bvid_a, 1, NOW - 10, "那咋了！！"),
        _comment(bvid_a, 2, NOW - 20, "那咋了"),        # 同视频归一化文本与 id1 重复
        _comment(bvid_a, 3, NOW - 30, "哈基米 哈基米"),
        _comment(bvid_a, 4, NOW - 40, "哈基米哈基米"),  # 同视频归一化后重复
    ] + [_comment(bvid_a, 100 + i, NOW - 50 - i, f"填充{i}") for i in range(30)]
    comments_a.insert(20, _comment(bvid_a, 1, NOW - 10, "那咋了！！"))  # 跨页重复 id
    return {
        (bvid_a, 2): comments_a,
        (bvid_b, 2): [
            _comment(bvid_b, 1, NOW - 15, "那咋了"),  # 跨视频同文本
            _comment(bvid_b, 2, NOW - 16, "另一个梗"),
        ],
    }


def test_comment_dedupe_id_and_normalized_text() -> None:
    """默认 per_video：comment_id 全局去重；归一化文本只在同一视频内去重。"""
    bvid_a, bvid_b = "BVC1", "BVC2"
    store = _dedupe_store()
    fake = FakeSource(comment_store=store)
    sample = run_window_sample(
        fake,
        WindowSamplePlan(),
        now=NOW,
        comment_targets=[_target(bvid_a), _target(bvid_b)],
        danmaku_targets=[],
        use_db=False,
    )
    texts = [item.text for item in sample.comments]
    norms = [normalize_surface(item.text) for item in sample.comments]
    # 同视频内重复：id2（文本重复）、id4（归一化重复）被丢弃
    assert "那咋了" not in [item.text for item in sample.comments if item.bvid == bvid_a]
    assert norms.count(normalize_surface("哈基米哈基米")) == 1
    # 跨视频同文本保留（避免误伤扩散计数），但 id 去重仍全局
    assert "那咋了" in [item.text for item in sample.comments if item.bvid == bvid_b]
    assert len({item.id for item in sample.comments}) == len(sample.comments)
    assert sample.stats["comment_dupes_id"] >= 1
    assert sample.stats["comment_dupes_text"] >= 2

    # 可选 global：跨视频同文本也去重
    fake2 = FakeSource(comment_store=store)
    sample2 = run_window_sample(
        fake2,
        WindowSamplePlan(text_dedupe_scope="global"),
        now=NOW,
        comment_targets=[_target(bvid_a), _target(bvid_b)],
        danmaku_targets=[],
        use_db=False,
    )
    texts2 = [item.text for item in sample2.comments]
    assert "那咋了" not in texts2
    assert sample2.stats["comment_dupes_text"] >= sample.stats["comment_dupes_text"]


def test_hot_mode3_comments_not_used_for_window_ratio() -> None:
    """比例计数只认 mode=2；mode=3 热评不能进 15 分钟计数。"""
    bvid = "BVM3"
    fake = FakeSource(
        comment_store={
            (bvid, 2): [_comment(bvid, i, NOW - i, f"时间序{i}") for i in range(1, 3)],
            (bvid, 3): [
                _comment(bvid, 100 + i, NOW - i, f"热评{i}", like=999, mode=3)
                for i in range(1, 101)
            ],
        }
    )
    sample = run_window_sample(
        fake,
        WindowSamplePlan(evidence=False),
        now=NOW,
        comment_targets=[_target(bvid)],
        danmaku_targets=[],
        use_db=False,
    )
    assert len(sample.comments) == 2
    assert all(item.mode == 2 for item in sample.comments)
    assert all(item.id.startswith(f"{bvid}:") for item in sample.comments)


# ================================================================ 弹幕窗口
def test_danmaku_ctime_dmid_dedupe_and_length_filter() -> None:
    bvid, cid = "BVD1", 777
    entries = [
        (NOW - 10, "111", 5.0, "好梗"),        # 保留
        (NOW - 20, "111", 6.0, "重复dmid"),    # dmid 重复
        (NOW - 30, "222", 7.0, "字"),          # 长度 <2
        (NOW - 40, "333", 8.0, "长" * 41),     # 长度 >40
        (NOW - 3600, "444", 9.0, "过窗"),      # 窗口外
        (NOW - 50, "555", 10.0, "正常弹幕"),   # 保留
    ]
    xml = _danmaku_xml(entries).replace(
        "</i>",
        f'<d p="11.00,1,25,16777215,{NOW - 60},0,abc">无dmid弹幕</d></i>',
    )
    fake = FakeSource(danmaku_store={cid: xml})
    sample = run_window_sample(
        fake,
        WindowSamplePlan(),
        now=NOW,
        comment_targets=[],
        danmaku_targets=[_target(bvid, cid=cid)],
        use_db=False,
    )
    assert len(sample.danmaku) == 3
    assert [item.dmid for item in sample.danmaku].count("111") == 1
    assert any(item.dmid == "555" for item in sample.danmaku)
    assert any(item.dmid == "" and item.text == "无dmid弹幕" for item in sample.danmaku)
    assert all(WINDOW_START <= item.ctime <= WINDOW_END for item in sample.danmaku)
    assert sample.stats["danmaku_dupes"] >= 1
    assert sample.stats["danmaku_bad_length"] >= 2
    assert sample.stats["danmaku_out_of_window"] >= 1
    assert sample.stats["danmaku_missing_dmid"] >= 1


# ================================================================ 配额/截断/degraded
def test_comment_goal_truncation_round_robin_deterministic() -> None:
    bvids = ["BVQ1", "BVQ2", "BVQ3"]
    store = {}
    for bvid in bvids:
        store[(bvid, 2)] = [
            _comment(bvid, i, NOW - i, f"{bvid}-第{i}条评论") for i in range(1, 401)
        ]
    plan = WindowSamplePlan(comment_max_pages=30)
    fake = FakeSource(comment_store=store)
    sample = run_window_sample(
        fake,
        plan,
        now=NOW,
        comment_targets=[_target(bvid) for bvid in bvids],
        danmaku_targets=[],
        use_db=False,
    )
    assert len(sample.comments) == 1000
    counts = Counter(item.bvid for item in sample.comments)
    assert sorted(counts.values()) == [333, 333, 334]
    assert sample.stats["comment_truncated"] is True
    assert sample.stats["comment_quota_per_video"] == 334

    # 同一输入重跑结果完全一致（可复算）
    fake2 = FakeSource(comment_store=store)
    sample2 = run_window_sample(
        fake2,
        plan,
        now=NOW,
        comment_targets=[_target(bvid) for bvid in bvids],
        danmaku_targets=[],
        use_db=False,
    )
    assert [item.id for item in sample.comments] == [item.id for item in sample2.comments]


def test_danmaku_goal_truncation() -> None:
    targets = []
    store = {}
    for index, bvid in enumerate(["BVDQ1", "BVDQ2", "BVDQ3"]):
        cid = 900 + index
        entries = [
            (NOW - (i % 800) - 1, f"{index}{i:06d}", float(i % 100), f"弹幕{i:04d}")
            for i in range(1500)
        ]
        store[cid] = _danmaku_xml(entries)
        targets.append(_target(bvid, cid=cid))
    fake = FakeSource(danmaku_store=store)
    sample = run_window_sample(
        fake,
        WindowSamplePlan(danmaku_goal=500),  # 每视频 ceil(500/3)=167 → 501 截断到 500
        now=NOW,
        comment_targets=[],
        danmaku_targets=targets,
        use_db=False,
    )
    assert len(sample.danmaku) == 500
    counts = Counter(item.bvid for item in sample.danmaku)
    assert sorted(counts.values()) == [166, 167, 167]
    assert sample.stats["danmaku_truncated"] is True

    # 默认目标 600：3 个视频各 200 配额，恰好命中目标（不截断）
    fake2 = FakeSource(danmaku_store=store)
    sample2 = run_window_sample(
        fake2,
        WindowSamplePlan(),
        now=NOW,
        comment_targets=[],
        danmaku_targets=targets,
        use_db=False,
    )
    assert len(sample2.danmaku) == 600
    assert sample2.stats["profile"]["danmaku_goal"] == 600
    assert sample2.stats["profile"]["danmaku_min"] == 300


def test_insufficient_samples_mark_degraded() -> None:
    bvid, cid = "BVSMALL", 5
    fake = FakeSource(
        comment_store={
            (bvid, 2): [_comment(bvid, i, NOW - i, f"少量评论{i}") for i in range(1, 11)]
        },
        danmaku_store={
            cid: _danmaku_xml(
                [(NOW - i, str(100 + i), 1.0, f"弹幕{i}") for i in range(1, 11)]
            )
        },
    )
    sample = run_window_sample(
        fake,
        WindowSamplePlan(),
        now=NOW,
        comment_targets=[_target(bvid)],
        danmaku_targets=[_target(bvid, cid=cid)],
        use_db=False,
    )
    assert sample.status == "degraded"
    assert "comments=10<600" in sample.stats["degraded_reason"]
    assert "danmaku=10<300" in sample.stats["degraded_reason"]
    assert sample.stats["db_written"] is False


def test_no_targets_marks_degraded_without_error() -> None:
    fake = FakeSource()
    sample = run_window_sample(
        fake, WindowSamplePlan(), now=NOW, use_db=False
    )
    assert sample.status == "degraded"
    assert sample.stats["degraded_reason"] == "no targets"
    assert sample.round_id is None


def test_request_budget_exhaustion_marks_degraded() -> None:
    bvid = "BVBUDGET"
    fake = FakeSource(
        comment_store={(bvid, 2): [_comment(bvid, i, NOW - i, f"评论{i}") for i in range(1, 50)]}
    )
    sample = run_window_sample(
        fake,
        WindowSamplePlan(
            comment_goal=1000,
            danmaku_goal=10,
            danmaku_min=0,
            comment_min=0,
            comment_max_pages=50,
            max_requests=1,
            evidence=False,
        ),
        now=NOW,
        comment_targets=[_target(bvid)],
        danmaku_targets=[],
        use_db=False,
    )
    assert sample.stats.get("request_budget_exhausted") or sample.status == "degraded"


def test_quota_helpers() -> None:
    assert per_video_cap(1000, 45) == 23
    assert per_video_cap(1000, 3) == 334
    assert per_video_cap(0, 0) == 0
    assert round_robin_truncate([[1, 2, 3], [4, 5], [6]], 4) == [1, 4, 6, 2]
    assert round_robin_truncate([[1, 2], [3]], 10) == [1, 3, 2]


# ================================================================ 异常窗口 clamp
def test_abnormal_window_clamped_to_15_minutes() -> None:
    """round 窗口跨度 >20 分钟（停机/漏跑）时只取最后 15 分钟，标 window_clamped。"""
    bvid, cid = "BVCLAMP", 66
    fake = FakeSource(
        comment_store={
            (bvid, 2): [
                _comment(bvid, i, NOW - i * 10, f"窗口内评论{i}") for i in range(1, 6)
            ]
            + [_comment(bvid, 50 + i, NOW - 3600 - i, f"窗口外评论{i}") for i in range(1, 5)]
        },
        danmaku_store={
            cid: _danmaku_xml(
                [
                    (NOW - 5, "1", 1.0, "窗口内弹幕"),
                    (NOW - 3 * 3600, "2", 2.0, "窗口外弹幕"),
                ]
            )
        },
        view_store={bvid: Video(bvid=bvid, aid=123)},
    )
    result = VideoRouteResult(
        started_at=NOW - 3 * 3600,
        window_start=NOW - 3 * 3600,  # 停机 3 小时
        window_end=NOW,
        selected_comment=[bvid],
        selected_danmaku=[bvid],
        metrics=[VideoMetric(bvid=bvid, cid=cid, title="标题")],
    )
    sample = run_window_sample(
        fake,
        WindowSamplePlan(),
        now=NOW + 1,
        video_route=result,
        use_db=False,
    )
    assert sample.window_end == NOW
    assert sample.window_start == NOW - 15 * 60, "计数窗口必须 clamp 到 15 分钟"
    assert sample.stats["window_clamped"] is True
    assert sample.stats["window_original_minutes"] == 180.0
    assert "window_clamped" in sample.stats["degraded_reason"]
    assert sample.status == "degraded"
    # 只统计 clamp 后窗口内的样本
    assert len(sample.comments) == 5
    assert len(sample.danmaku) == 1

    # 正常 15 分钟窗口不 clamp
    fake2 = FakeSource(
        comment_store={(bvid, 2): []},
        danmaku_store={},
        view_store={bvid: Video(bvid=bvid, aid=123)},
    )
    result2 = VideoRouteResult(
        started_at=NOW,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        selected_comment=[bvid],
        selected_danmaku=[],
        metrics=[VideoMetric(bvid=bvid, cid=cid, title="标题")],
    )
    sample2 = run_window_sample(
        fake2, WindowSamplePlan(), now=NOW + 1, video_route=result2, use_db=False
    )
    assert sample2.stats["window_clamped"] is False
    assert "window_clamped" not in sample2.stats["degraded_reason"]


# ================================================================ 30 天证据分离
def test_evidence_30d_separated_from_window_counts() -> None:
    bvid, cid = "BVEV", 88
    mode2 = [_comment(bvid, i, NOW - i, f"窗口评论{i}") for i in range(1, 21)]
    mode2 += [
        _comment(bvid, 100 + i, NOW - 10 * DAY - i, f"30天评论{i}") for i in range(1, 21)
    ]
    mode3 = [
        _comment(bvid, 200 + i, NOW - 3 * DAY - i, f"热评{i}", like=100 - i, mode=3)
        for i in range(1, 31)
    ]
    fake = FakeSource(
        comment_store={(bvid, 2): mode2, (bvid, 3): mode3},
        danmaku_store={
            cid: _danmaku_xml(
                [
                    (NOW - 5, "1001", 1.0, "窗口弹幕一"),
                    (NOW - 6, "1002", 2.0, "窗口弹幕二"),
                    (NOW - 5 * DAY, "1003", 3.0, "证据弹幕"),
                    (NOW - 6 * DAY, "1004", 4.0, "证据弹幕二"),
                ]
            )
        },
    )
    sample = run_window_sample(
        fake,
        WindowSamplePlan(evidence_comment_pages=2),
        now=NOW,
        comment_targets=[_target(bvid)],
        danmaku_targets=[_target(bvid, cid=cid)],
        use_db=False,
    )
    # 15 分钟计数：只有 mode=2 窗口内
    assert len(sample.comments) == 20
    assert all(item.mode == 2 for item in sample.comments)
    assert all(WINDOW_START <= item.ctime <= WINDOW_END for item in sample.comments)
    # 30 天证据：字段分开、可含窗口外/mode3、每视频 ≤40
    assert sample.evidence_comments
    assert len(sample.evidence_comments) <= 40
    assert any(item.ctime < WINDOW_START for item in sample.evidence_comments)
    assert any(item.mode == 3 for item in sample.evidence_comments)
    assert all(item.evidence for item in sample.evidence_comments)
    assert all(not item.evidence for item in sample.comments)
    # 弹幕：窗口计数值 2，证据含 5/6 天前
    assert len(sample.danmaku) == 2
    assert {item.dmid for item in sample.danmaku} == {"1001", "1002"}
    assert {"1003", "1004"} <= {item.dmid for item in sample.evidence_danmaku}
    # docs() 分开输出
    window_docs = sample.docs()
    evidence_docs = sample.docs(evidence=True)
    assert len(window_docs) == len(sample.comments) + len(sample.danmaku)
    assert len(evidence_docs) == len(sample.evidence_comments) + len(sample.evidence_danmaku)

    # --no-evidence：完全跳过 30 天证据
    fake2 = FakeSource(
        comment_store={(bvid, 2): mode2, (bvid, 3): mode3},
        danmaku_store={cid: _danmaku_xml([(NOW - 5, "1001", 1.0, "窗口弹幕一")])},
    )
    sample2 = run_window_sample(
        fake2,
        WindowSamplePlan(evidence=False),
        now=NOW,
        comment_targets=[_target(bvid)],
        danmaku_targets=[_target(bvid, cid=cid)],
        use_db=False,
    )
    assert sample2.evidence_comments == []
    assert sample2.evidence_danmaku == []
    assert len(sample2.comments) == 20


# ================================================================ 目标输入
def test_video_route_result_input_and_window() -> None:
    bvid, cid = "BVROUTE", 4321
    result = VideoRouteResult(
        generated_at="",
        started_at=NOW,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        selected_comment=[bvid],
        selected_danmaku=[bvid],
        metrics=[VideoMetric(bvid=bvid, cid=cid, title="标题", zone="鬼畜")],
    )
    fake = FakeSource(
        comment_store={
            (bvid, 2): [_comment(bvid, i, NOW - i, f"评论{i}") for i in range(1, 4)]
        },
        danmaku_store={cid: _danmaku_xml([(NOW - 1, "1", 1.0, "弹幕一")])},
        view_store={bvid: Video(bvid=bvid, aid=999)},
    )
    sample = run_window_sample(
        fake,
        WindowSamplePlan(),
        now=NOW + 5,
        video_route=result,
        use_db=False,
    )
    assert sample.window_start == WINDOW_START
    assert sample.window_end == WINDOW_END
    assert sample.stats["window_clamped"] is False
    assert sample.stats["target_source"] == "explicit"
    assert len(sample.comments) == 3
    assert len(sample.danmaku) == 1
    # 目标不带 aid：通过 /x/web-interface/view 反查（测试注入 view）
    assert sample.stats["aid_view_resolved"] == 1
    assert sample.comment_targets[0].aid == 999


def test_targets_from_payload_formats() -> None:
    generic = {
        "comment_targets": [{"bvid": "BV1", "aid": 1}, {"bvid": "BV2"}],
        "danmaku_targets": ["BV3"],
    }
    comment_targets, danmaku_targets = targets_from_payload(generic)
    assert [item.bvid for item in comment_targets] == ["BV1", "BV2"]
    assert danmaku_targets[0].bvid == "BV3"
    route = {
        "selected_comment": ["BV9"],
        "selected_danmaku": [],
        "metrics": [{"bvid": "BV9", "cid": 5, "title": "t", "hot_score": 1.5}],
    }
    comment_targets, _ = targets_from_payload(route)
    assert comment_targets[0].cid == 5
    assert comment_targets[0].hot_score == pytest.approx(1.5)


# ================================================================ bili 解析
def test_parse_danmaku_xml_extracts_dmid_and_ctime() -> None:
    xml = _danmaku_xml([(1234567890, "987654321", 12.5, "测试弹幕")])
    items = parse_danmaku_xml(xml, bvid="BVX")
    assert items[0].ctime == 1234567890
    assert items[0].dmid == "987654321"
    assert items[0].progress == pytest.approx(12.5)
    # 老格式没有第 8 段：dmid 留空，ctime 仍可解析
    legacy = (
        '<i><d p="1.00,1,25,16777215,1234567890,0,abc">老格式弹幕</d></i>'
    )
    item = parse_danmaku_xml(legacy, bvid="BVX")[0]
    assert item.dmid == ""
    assert item.ctime == 1234567890
    assert item.id  # id 仍保持历史 sha1 兼容


def test_parse_comment_page_include_top_toggle() -> None:
    payload = {
        "data": {
            "cursor": {"next": 2, "is_end": False},
            "top_replies": [
                {"rpid": 999, "ctime": 100, "content": {"message": "置顶旧评论"}}
            ],
            "replies": [
                {"rpid": 1, "ctime": 200, "content": {"message": "时间序评论"}}
            ],
        }
    }
    with_top, _, _ = parse_comment_page(payload, bvid="BVX")
    without_top, _, _ = parse_comment_page(payload, bvid="BVX", include_top=False)
    assert [item.id for item in with_top] == ["BVX:999", "BVX:1"]
    assert [item.id for item in without_top] == ["BVX:1"]
    # 真实 fixture 保持兼容：模式 3 默认仍合并置顶/普通评论并去重
    real = json.loads((FIXTURES / "reply_main.json").read_text(encoding="utf-8"))
    merged, _, _ = parse_comment_page(real, bvid="BVX")
    assert len({item.id for item in merged}) == len(merged)


# ================================================================ CLI（--no-db）
def _write_cli_fixtures(tmp_path: Path) -> dict[str, Path]:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "reply_main_mode2.json").write_text(
        json.dumps(
            {
                "code": 0,
                "data": {
                    "cursor": {"next": 0, "is_end": True, "mode": 2},
                    "top_replies": [],
                    "replies": [
                        {"rpid": 1, "ctime": NOW - 10, "content": {"message": "窗口评论一"}},
                        {"rpid": 2, "ctime": NOW - 20, "content": {"message": "窗口评论二"}},
                        {"rpid": 3, "ctime": NOW - 3600, "content": {"message": "窗口外评论"}},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (fixture_dir / "reply_main.json").write_text(
        json.dumps(
            {
                "code": 0,
                "data": {
                    "cursor": {"next": 0, "is_end": True, "mode": 3},
                    "top_replies": [],
                    "replies": [
                        {
                            "rpid": 11,
                            "ctime": NOW - 2 * DAY,
                            "like": 99,
                            "content": {"message": "证据热评"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (fixture_dir / "danmaku.xml").write_text(
        _danmaku_xml(
            [
                (NOW - 5, "1", 1.0, "窗口弹幕一"),
                (NOW - 6, "2", 2.0, "窗口弹幕二"),
                (NOW - 2 * DAY, "3", 3.0, "证据弹幕"),
            ]
        ),
        encoding="utf-8",
    )
    targets = tmp_path / "targets.json"
    targets.write_text(
        json.dumps(
            {
                "comment_targets": [{"bvid": "BVCLI", "aid": 111}],
                "danmaku_targets": [{"bvid": "BVCLI", "cid": 222}],
            }
        ),
        encoding="utf-8",
    )
    return {"fixture_dir": fixture_dir, "targets": targets}


def test_cli_no_db_with_targets_json(tmp_path: Path) -> None:
    files = _write_cli_fixtures(tmp_path)
    output = tmp_path / "window.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.window_sample",
            "--round",
            "latest",
            "--no-db",
            "--offline",
            "--fixture-dir",
            str(files["fixture_dir"]),
            "--targets-json",
            str(files["targets"]),
            "--now",
            str(NOW),
            "--json",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "degraded"
    assert payload["round_id"] is None
    assert payload["window_start"] == WINDOW_START
    assert payload["window_end"] == WINDOW_END
    assert payload["stats"]["db_written"] is False
    assert payload["stats"]["target_source"] == "explicit"
    assert len(payload["comments"]) == 2
    assert len(payload["danmaku"]) == 2
    assert payload["evidence_comments"]
    assert payload["evidence_danmaku"]
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["window_start"] == WINDOW_START


def test_cli_no_db_without_targets_is_degraded_not_error(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.window_sample",
            "--round",
            "latest",
            "--no-db",
            "--offline",
            "--fixture-dir",
            str(FIXTURES),
            "--now",
            str(NOW),
            "--json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "degraded"
    assert payload["stats"]["degraded_reason"] == "no targets"
    assert payload["stats"]["db_written"] is False


def test_cli_rejects_missing_targets_json() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.window_sample",
            "--no-db",
            "--targets-json",
            "/no/such/targets.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "targets JSON" in completed.stderr


# ================================================================ Postgres 集成（事务回滚）
class _Rollback(Exception):
    """用异常让 state.transaction 回滚，保证测试不污染生产数据。"""


@requires_db
def test_persist_sample_round_update_rollback() -> None:
    from meme_info import state

    bvid, cid = "BVPC", 555
    fake = FakeSource(
        comment_store={
            (bvid, 2): [_comment(bvid, i, NOW - i, f"评论{i}") for i in range(1, 4)]
        },
        danmaku_store={cid: _danmaku_xml([(NOW - 1, "1", 1.0, "弹幕一")])},
        # video_metric 不存 aid：评论目标需经 /x/web-interface/view 反查
        view_store={bvid: Video(bvid=bvid, aid=321)},
    )
    before_round = before_metric = 0
    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            before_round = conn.execute("SELECT count(*) FROM round").fetchone()[0]
            before_metric = conn.execute("SELECT count(*) FROM video_metric").fetchone()[0]
            round_id = conn.execute(
                "INSERT INTO round (started_at, finished_at, window_start, window_end, "
                "status, videos_sampled, stats) VALUES "
                "(to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), "
                "'ok', 7, %s::jsonb) RETURNING id",
                (
                    NOW,
                    NOW,
                    WINDOW_START,
                    WINDOW_END,
                    json.dumps({"phase_b": {"candidates": 300}, "keep": 1}),
                ),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO video_metric (round_id, bvid, cid, title, zone, hot_score, "
                "selected_comment, selected_danmaku) VALUES (%s, %s, %s, %s, %s, %s, true, true)",
                (round_id, bvid, cid, "测试标题", "鬼畜", 2.0),
            )
            sample = run_window_sample(
                fake,
                WindowSamplePlan(),
                now=NOW + 10,
                use_db=True,
                database_url=DB_URL,
                db_conn=conn,
                round_id=round_id,
            )
            assert sample.round_id == round_id
            assert sample.window_start == WINDOW_START
            assert len(sample.comments) == 3 and len(sample.danmaku) == 1
            row = conn.execute(
                "SELECT comments_sampled, danmaku_sampled, status, degraded_reason, "
                "stats, videos_sampled FROM round WHERE id = %s",
                (round_id,),
            ).fetchone()
            assert row[0] == 3 and row[1] == 1
            assert row[2] == "degraded", "样本低于 60% 必须标 degraded"
            assert "comments=3<600" in (row[3] or "")
            stats = row[4]
            if isinstance(stats, (str, bytes, bytearray)):
                stats = json.loads(stats)
            assert stats["phase_b"] == {"candidates": 300}, "Phase B 报告不得被覆盖"
            assert stats["keep"] == 1
            assert stats["phase_c"]["comments_sampled"] == 3
            assert stats["phase_c"]["danmaku_sampled"] == 1
            assert row[5] == 7, "videos_sampled 不允许改动"

            # 再跑一次：degraded_reason 追加、stats 合并，不清空旧报告
            sample2 = run_window_sample(
                fake,
                WindowSamplePlan(),
                now=NOW + 20,
                use_db=True,
                database_url=DB_URL,
                db_conn=conn,
                round_id=round_id,
            )
            row2 = conn.execute(
                "SELECT degraded_reason, stats FROM round WHERE id = %s", (round_id,)
            ).fetchone()
            assert (row2[0] or "").count("comments=") == 2
            stats2 = row2[1]
            if isinstance(stats2, (str, bytes, bytearray)):
                stats2 = json.loads(stats2)
            assert stats2["phase_b"] == {"candidates": 300}
            assert sample2.round_id == round_id
            raise _Rollback

    with state.connection(DB_URL) as conn:
        assert conn.execute("SELECT count(*) FROM round").fetchone()[0] == before_round
        assert (
            conn.execute("SELECT count(*) FROM video_metric").fetchone()[0]
            == before_metric
        ), "测试行必须已回滚"


@requires_db
def test_hot_score_fallback_targets_rollback() -> None:
    """本轮没有 selected 标记时，按 hot_score 取前 45/10 兜底（事务回滚）。"""
    from meme_info import state

    bvids = ["BVFB1", "BVFB2", "BVFB3"]
    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            before = conn.execute("SELECT count(*) FROM round").fetchone()[0]
            round_id = conn.execute(
                "INSERT INTO round (started_at, window_start, window_end, status) "
                "VALUES (to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), 'ok') "
                "RETURNING id",
                (NOW, WINDOW_START, WINDOW_END),
            ).fetchone()[0]
            for index, bvid in enumerate(bvids):
                conn.execute(
                    "INSERT INTO video_metric (round_id, bvid, cid, title, hot_score, "
                    "selected_comment, selected_danmaku) VALUES (%s, %s, %s, %s, %s, false, false)",
                    (round_id, bvid, 700 + index, f"兜底{index}", 9.0 - index * 4),
                )
            fake = FakeSource(
                comment_store={
                    bvid: [
                        _comment(bvid, i, NOW - i, f"{bvid}评论{i}") for i in range(1, 3)
                    ]
                    for bvid in bvids
                },
                danmaku_store={
                    700 + index: _danmaku_xml([(NOW - 1, str(1000 + index), 1.0, "弹幕一")])
                    for index in range(3)
                },
                view_store={
                    bvid: Video(bvid=bvid, aid=100 + index)
                    for index, bvid in enumerate(bvids)
                },
            )
            sample = run_window_sample(
                fake,
                WindowSamplePlan(comment_targets=2, danmaku_targets=2),
                now=NOW + 10,
                use_db=True,
                database_url=DB_URL,
                db_conn=conn,
                round_id=round_id,
            )
            assert sample.stats["comment_fallback_hot"] is True
            assert sample.stats["danmaku_fallback_hot"] is True
            assert [item.bvid for item in sample.comment_targets] == ["BVFB1", "BVFB2"]
            assert [item.bvid for item in sample.danmaku_targets] == ["BVFB1", "BVFB2"]
            assert sample.stats["comment_target_count"] == 2
            assert conn.execute("SELECT count(*) FROM round").fetchone()[0] == before + 1
            raise _Rollback

    with state.connection(DB_URL) as conn:
        assert conn.execute("SELECT count(*) FROM round").fetchone()[0] == before
