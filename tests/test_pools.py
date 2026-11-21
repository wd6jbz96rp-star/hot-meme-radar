"""Phase D 测试：family 观察 / 可信度 / 比例 / 池迁移 / CLI / Postgres。

无 ``TEST_DATABASE_URL`` 时只跑纯单元测试；有则额外跑 Postgres 集成测试
（在事务内执行并回滚，不污染生产数据）。
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meme_info.knowledge import Knowledge
from meme_info.models import (
    FamilyObservation,
    Pool1Row,
    Pool2Row,
    WindowComment,
    WindowDanmaku,
    WindowSample,
    WindowTarget,
)
from meme_info.pools import (
    PoolConfig,
    _group_families,
    apply_round,
    build_observations,
    pool_state_from_result,
    run_pool_round,
    sample_from_json,
    sat,
)

DB_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not DB_URL, reason="需要 TEST_DATABASE_URL 才能跑 Postgres 集成测试"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000
WINDOW = 15 * 60
DAY = 86400


# ================================================================ 测试工具
def _target(bvid: str, title: str = "", zone: str = "鬼畜") -> WindowTarget:
    return WindowTarget(bvid=bvid, title=title, zone=zone, source="selected")


def _comment(doc_id: str, text: str, *, bvid: str = "BV1", zone: str = "鬼畜") -> WindowComment:
    return WindowComment(
        id=doc_id, bvid=bvid, text=text, ctime=NOW - 60, zone=zone, video_title=f"标题{bvid}"
    )


def _danmaku(
    doc_id: str, text: str, *, dmid: str = "", bvid: str = "BV2", zone: str = "鬼畜"
) -> WindowDanmaku:
    return WindowDanmaku(
        id=doc_id,
        dmid=dmid or doc_id,
        bvid=bvid,
        text=text,
        ctime=NOW - 30,
        zone=zone,
        video_title=f"标题{bvid}",
    )


def _sample(
    *,
    comments: list[WindowComment] | None = None,
    danmaku: list[WindowDanmaku] | None = None,
    evidence_comments: list[WindowComment] | None = None,
    evidence_danmaku: list[WindowDanmaku] | None = None,
    targets: list[WindowTarget] | None = None,
    status: str = "ok",
    window_start: int = NOW - WINDOW,
    window_end: int = NOW,
) -> WindowSample:
    targets = targets or []
    return WindowSample(
        window_start=window_start,
        window_end=window_end,
        status=status,
        comments=comments or [],
        danmaku=danmaku or [],
        evidence_comments=evidence_comments or [],
        evidence_danmaku=evidence_danmaku or [],
        comment_targets=targets,
        danmaku_targets=targets,
    )


def _obs(
    key: str,
    *,
    comment: int = 0,
    danmaku: int = 0,
    video: int = 0,
    evidence_video: int = 0,
    meta: int = 0,
    meta_strength: float = 0.0,
    cred: float = 0.0,
    known: bool = False,
    human: bool = False,
    meta_only: bool = False,
    explainer: bool = False,
    discovered: bool = True,
    zero_streak_hint: int = 0,
    badges: bool = False,
) -> FamilyObservation:
    count_total = comment + danmaku + video
    return FamilyObservation(
        family_key=key,
        canonical=key,
        norm=key,
        aliases=[key],
        present=discovered or count_total > 0,
        discovered=discovered,
        comment_count=comment,
        danmaku_count=danmaku,
        video_count=video,
        evidence_video_count=evidence_video,
        meta_count=meta,
        count_total=count_total,
        usage_count=comment + danmaku,
        meta_strength=meta_strength,
        meta_independent=meta if meta_strength >= 0.9 else 0,
        dedicated_meta=meta if meta_strength >= 0.9 else 0,
        cred_base=cred,
        known=known,
        human_confirmed=human,
        meta_only=meta_only,
        explainer_eligible=explainer,
        badge_2x2=badges,
        badge_docs=2 if badges else 0,
        badge_videos=2 if badges else 0,
        evidence=[{"doc_id": f"{key}-doc", "quote": key, "kind": "comment"}],
    )


def _row(
    key: str,
    *,
    status: str = "normal",
    score: float = 50.0,
    count_prev: int = 0,
    count_now: float | int = 0,
    zero_streak: int = 0,
    meta_only: bool = False,
    explainer_only: bool = False,
    audit_adjustment: float = 0.0,
    audit_state: str = "none",
    aliases: list[str] | None = None,
) -> Pool1Row:
    return Pool1Row(
        family_key=key,
        canonical=key,
        aliases=aliases or [key],
        status=status,
        score=score,
        count_prev=int(count_prev),
        count_now=int(count_now),
        zero_streak=zero_streak,
        meta_only=meta_only,
        explainer_only=explainer_only,
        audit_adjustment=audit_adjustment,
        audit_state=audit_state,
        first_seen_round=1,
        last_seen_round=1,
    )


def _apply(
    observations,
    *,
    prev1=None,
    prev2=None,
    round_id: int = 2,
    degraded: bool = False,
    config: PoolConfig | None = None,
):
    return apply_round(
        observations,
        prev_pool1=prev1 or {},
        prev_pool2=prev2 or {},
        round_id=round_id,
        degraded=degraded,
        config=config,
        now=NOW,
    )


# ================================================================ 1. family 计数
def test_counts_comments_danmaku_video_and_meta_not_in_total() -> None:
    """PM 口径：video_count 只取 15 分钟窗口独立 bvid；30 天证据视频只作 evidence_video_count。"""
    sample = _sample(
        comments=[
            _comment("c1", "尊嘟假嘟太好笑了", bvid="BV1"),
            _comment("c2", "又是尊嘟假嘟", bvid="BV1"),  # 同视频第二条评论：算 2 条评论
            _comment("c2", "重复 id 不应重复计数", bvid="BV2"),
        ],
        danmaku=[
            _danmaku("d1", "尊嘟假嘟", dmid="dm1", bvid="BV2"),
            _danmaku("d2", "尊嘟假嘟", dmid="dm1", bvid="BV2"),  # 同 dmid：去重
            _danmaku("d3", "尊嘟假嘟", dmid="dm3", bvid="BV3"),
        ],
        evidence_comments=[
            _comment("e1", "尊嘟假嘟", bvid="BV1"),
            _comment("e2", "尊嘟假嘟", bvid="BV4"),
            _comment("e3", "尊嘟假嘟", bvid="BV4"),
            _comment("e4", "尊嘟假嘟", bvid="BV5"),
            _comment("e5", "尊嘟假嘟", bvid="BV6"),
        ],
        targets=[
            _target("BV1", "尊嘟假嘟是什么梗", "鬼畜"),
            _target("BV2", "普通视频", "生活"),
            _target("BV3", "普通视频2", "生活"),
            _target("BV4", "普通视频4", "生活"),
            _target("BV5", "普通视频5", "生活"),
            _target("BV6", "普通视频6", "生活"),
        ],
    )
    observations = build_observations(sample, now=NOW)
    by_key = {item.family_key: item for item in observations}
    assert "尊嘟假嘟" in by_key
    observation = by_key["尊嘟假嘟"]
    assert observation.comment_count == 2, "评论按去重 id 计数"
    assert observation.danmaku_count == 2, "弹幕按 dmid 去重"
    assert observation.video_count == 3, "video_count = 窗口独立 bvid（BV1/BV2/BV3）"
    assert observation.evidence_video_count == 4, "30 天证据独立 bvid（BV1/BV4/BV5/BV6）"
    assert observation.meta_count == 1, "meta_count = 元话语视频数"
    assert observation.count_total == 2 + 2 + 3, "count_total 不含 meta/evidence_video_count"
    assert observation.usage_count == 4, "usage_count = 窗口评论 + 弹幕"
    assert observation.meta_kinds == ["dedicated"]
    assert observation.meta_strength == 1.0
    assert len(observation.evidence) <= 5
    assert observation.evidence and observation.evidence[0]["quote"] == "尊嘟假嘟"
    assert observation.known is True, "种子命中 knowledge"
    # evidence_json 带 evidence_video_count，且不改变比例计数
    result = _apply([observation])
    assert result.pool1[0].evidence_json["evidence_video_count"] == 4
    assert result.pool1[0].count_now == 7

    # 只有 30 天证据命中的视频/文档不得抬高窗口 video_count
    evidence_only = _sample(
        evidence_comments=[_comment("e9", "尊嘟假嘟", bvid="BV9")],
        targets=[_target("BV9", "普通视频9")],
    )
    only_evidence = {
        item.family_key: item
        for item in build_observations(evidence_only, now=NOW)
    }["尊嘟假嘟"]
    assert only_evidence.video_count == 0, "证据视频不计入窗口 video_count"
    assert only_evidence.evidence_video_count == 1
    assert only_evidence.count_total == 0


def test_same_alias_not_double_counted_per_doc() -> None:
    """同一条文档命中多个 alias 也只计一次。"""
    sample = _sample(
        comments=[_comment("c1", "尊嘟假嘟真的尊嘟假嘟")],
        targets=[_target("BV1", "尊嘟假嘟是什么梗")],
    )
    observations = build_observations(sample, now=NOW)
    observation = next(item for item in observations if item.family_key == "尊嘟假嘟")
    assert observation.comment_count == 1


def test_evidence_json_badge_only_display() -> None:
    """2×2 只写 evidence_json 徽标：2 条独立文档 + 2 个独立视频。"""
    sample = _sample(
        comments=[
            _comment("c1", "尊嘟假嘟", bvid="BV1"),
            _comment("c2", "尊嘟假嘟", bvid="BV2"),
        ],
        targets=[_target("BV1", "普通1"), _target("BV2", "普通2")],
    )
    observations = build_observations(sample, now=NOW)
    observation = next(item for item in observations if item.family_key == "尊嘟假嘟")
    assert observation.badge_2x2 is True
    assert observation.badge_docs == 2 and observation.badge_videos == 2
    result = _apply([observation])
    row = result.pool1[0]
    assert row.evidence_json["badge_2x2"] is True
    assert row.evidence_json["badge_docs"] == 2
    assert row.evidence_json["updated_round"] == 2


# ================================================================ 2. 初始分
def test_new_family_with_usage_sets_initial_score_capped_80() -> None:
    result = _apply([_obs("新梗", comment=10, danmaku=2, cred=95.0)])
    row = result.pool1[0]
    assert row.status == "normal"
    assert row.score == 80.0, "有使用：clamp(cred_base, 0, 80)"
    assert row.count_prev == 0 and row.count_now == 12
    assert row.meta_only is False
    assert result.transitions[0].from_pool is None
    assert result.transitions[0].to_pool == "pool1"


def test_new_family_usage_below_p1_in_stored_as_out() -> None:
    result = _apply([_obs("弱梗", comment=1, cred=25.0)])
    row = result.pool1[0]
    assert row.status == "out"
    assert row.score == 25.0
    assert result.transitions == [], "out 行不产生池成员迁移"


def test_new_family_meta_only_is_provisional() -> None:
    result = _apply(
        [
            _obs(
                "雪豹",
                meta=1,
                meta_strength=1.0,
                cred=41.5,
                meta_only=True,
                discovered=True,
                badges=True,
            )
        ]
    )
    row = result.pool1[0]
    assert row.status == "provisional"
    assert 30.0 <= row.score <= 35.0
    assert row.meta_only is True
    assert result.transitions[0].reason == "new:meta_only"


def test_new_family_seed_strong_meta_gets_max_40() -> None:
    result = _apply(
        [_obs("尊嘟假嘟", meta=1, meta_strength=1.0, cred=56.5, known=True, meta_only=True, explainer=True)]
    )
    row = result.pool1[0]
    assert row.status == "normal"
    assert row.score == pytest.approx(56.5), "种子+强元话语：max(40, cred_base)"
    low = _apply([_obs("种子梗", meta=1, meta_strength=1.0, cred=20.0, known=True, meta_only=True)])
    assert low.pool1[0].score == 40.0 and low.pool1[0].status == "normal"


def test_new_family_weak_meta_is_skipped() -> None:
    result = _apply([_obs("弱元话语", meta=1, meta_strength=0.30, cred=20.0, meta_only=True)])
    assert result.pool1 == []
    assert result.stats_rows == []
    assert result.stats["skipped_no_pool"] == 1


# ================================================================ 3. 比例与零惩罚
def test_ratio_positive_delta_formula() -> None:
    prev = {"梗A": _row("梗A", score=50.0, count_prev=0, count_now=11)}
    observation = _obs("梗A", comment=10, cred=60.0)  # count_now=10
    result = _apply([observation], prev1=prev, round_id=3)
    row = result.pool1[0]
    ratio = (10 + 5) / (11 + 5)
    delta = 5 * math.log2(ratio)
    expected = 0.85 * 50.0 + 0.15 * 60.0 + delta
    assert row.score == pytest.approx(round(expected, 3))
    assert row.count_prev == 11 and row.count_now == 10
    assert result.stats_rows[0].ratio == pytest.approx(round(ratio, 4))
    assert result.stats_rows[0].reason.startswith("ratio:")


def test_ratio_negative_and_capped() -> None:
    prev = {"梗A": _row("梗A", score=70.0, count_now=400)}
    observation = _obs("梗A", comment=10, cred=60.0)
    result = _apply([observation], prev1=prev)
    delta = max(-10.0, 5 * math.log2((10 + 5) / (400 + 5)))
    assert delta == pytest.approx(-10.0)
    expected = 0.85 * 70.0 + 0.15 * 60.0 - 10.0
    assert result.pool1[0].score == pytest.approx(round(expected, 3))


def test_zero_after_usage_penalty() -> None:
    prev = {"梗A": _row("梗A", score=50.0, count_now=8)}
    observation = _obs("梗A", discovered=False)  # 本轮未出现：count=0
    result = _apply([observation], prev1=prev)
    row = result.pool1[0]
    assert row.count_now == 0 and row.zero_streak == 1
    expected = 0.85 * 50.0 + 0.15 * 0.0 - 5.0
    assert row.score == pytest.approx(round(expected, 3))
    assert result.stats_rows[0].reason == "zero:after_usage"


def test_zero_absent_penalty_and_streak_extra() -> None:
    prev = {"梗A": _row("梗A", score=60.0, count_now=0, zero_streak=1)}
    observation = _obs("梗A", discovered=False)
    result = _apply([observation], prev1=prev)
    assert result.pool1[0].zero_streak == 2
    assert result.pool1[0].score == pytest.approx(round(0.85 * 60.0 - 2.0, 3))

    prev3 = {"梗A": _row("梗A", score=60.0, count_now=0, zero_streak=2)}
    result3 = _apply([_obs("梗A", discovered=False)], prev1=prev3)
    assert result3.pool1[0].zero_streak == 3
    assert result3.pool1[0].score == pytest.approx(round(0.85 * 60.0 - 2.0 - 2.0, 3))
    assert "zero:streak" in result3.stats_rows[0].reason


def test_audit_adjustment_uses_prev_pool_value() -> None:
    prev = {"梗A": _row("梗A", score=50.0, count_now=5, audit_adjustment=15.0)}
    observation = _obs("梗A", comment=5, cred=60.0)
    result = _apply([observation], prev1=prev)
    ratio = (5 + 5) / (5 + 5)  # delta = 0
    expected = 0.85 * 50.0 + 0.15 * 60.0 + 0.0 + 15.0
    assert result.pool1[0].score == pytest.approx(round(expected, 3))
    assert result.pool1[0].audit_adjustment == 15.0


# ================================================================ 4. P1/P2 迁移
def test_p1_hysteresis_and_reentry() -> None:
    # 32–40 之间保持原状态
    keep = _apply([_obs("梗A", comment=1, cred=0.0)], prev1={"梗A": _row("梗A", score=38.0)})
    assert keep.pool1[0].status == "normal"
    # 低于 P1_out 撤出
    out = _apply([_obs("梗B", comment=1, cred=0.0)], prev1={"梗B": _row("梗B", score=31.0)})
    assert out.pool1[0].status == "out"
    assert [(t.from_pool, t.to_pool) for t in out.transitions] == [("pool1", None)]
    # out 但分数回到 P1_in 以上 → 重新入池
    reentry = _apply(
        [_obs("梗C", comment=30, cred=95.0)],
        prev1={"梗C": _row("梗C", status="out", score=40.0, count_now=20)},
    )
    assert reentry.pool1[0].status == "normal"
    assert any(t.from_pool is None and t.to_pool == "pool1" for t in reentry.transitions)


def test_p2_normal_promotion_requires_score_usage_normal() -> None:
    # 三项都满足 → 进池2
    result = _apply([_obs("梗A", comment=10, cred=95.0)])
    assert result.pool1[0].status == "normal"
    assert result.pool1[0].score == 80.0
    assert [row.family_key for row in result.pool2] == ["梗A"]
    assert result.pool2[0].explainer_only is False
    # score < P2_in（65）：即使有 usage 也不进池2
    low_score = _apply([_obs("梗B", comment=10, cred=50.0)])
    assert low_score.pool1[0].status == "normal"
    assert low_score.pool1[0].score == 50.0
    assert low_score.pool2 == []


def test_p2_hysteresis_keep_and_demote() -> None:
    prev2 = {"梗A": Pool2Row(family_key="梗A")}
    keep = _apply(
        [_obs("梗A", comment=10, cred=95.0)],
        prev1={"梗A": _row("梗A", score=60.0, count_now=10)},
        prev2=prev2,
    )
    assert [row.family_key for row in keep.pool2] == ["梗A", "梗A"] or any(
        row.family_key == "梗A" for row in keep.pool2
    ), "55–65 滞回：已在池2且分数>=55 保持"
    assert not any(t.reason == "p2:out_low_score" for t in keep.transitions)

    demote = _apply(
        [_obs("梗B", comment=10, cred=20.0)],
        prev1={"梗B": _row("梗B", score=54.0, count_now=10)},
        prev2={"梗B": Pool2Row(family_key="梗B")},
    )
    assert demote.pool2 == []
    assert any(
        t.from_pool == "pool2" and t.to_pool == "pool1" and t.reason == "p2:out_low_score"
        for t in demote.transitions
    )
    assert demote.pool1[0].status == "normal"


def test_p1_exit_deletes_pool2() -> None:
    result = _apply(
        [_obs("梗A", comment=1, cred=0.0)],
        prev1={"梗A": _row("梗A", score=31.0, count_now=2)},
        prev2={"梗A": Pool2Row(family_key="梗A")},
    )
    assert result.pool1[0].status == "out"
    assert result.pool2 == []
    pairs = {(t.from_pool, t.to_pool) for t in result.transitions}
    assert ("pool1", None) in pairs
    assert ("pool2", None) in pairs


def test_zero_padded_rows_have_stats() -> None:
    prev = {"梗A": _row("梗A", score=60.0, count_now=5)}
    result = _apply([_obs("梗A", discovered=False)], prev1=prev)
    assert result.stats_rows[0].family_key == "梗A"
    assert result.stats_rows[0].count_total == 0
    assert result.stats_rows[0].score_after == result.pool1[0].score


def test_count_prev_updated_at_round_end() -> None:
    result1 = _apply([_obs("梗A", comment=7, cred=60.0)], round_id=1)
    prev1, prev2 = pool_state_from_result(result1)
    result2 = _apply([_obs("梗A", comment=9, cred=60.0)], prev1=prev1, prev2=prev2, round_id=2)
    row = result2.pool1[0]
    assert row.count_prev == 7, "轮末把上一轮 count_now 落到 count_prev"
    assert row.count_now == 9


# ================================================================ 5. degraded 冻结
def test_degraded_freezes_score_and_membership() -> None:
    prev = {"梗A": _row("梗A", score=50.0, count_prev=1, count_now=6, zero_streak=0)}
    prev2 = {"梗A": Pool2Row(family_key="梗A")}
    result = _apply(
        [_obs("梗A", comment=20, cred=95.0), _obs("新梗", comment=5, cred=90.0)],
        prev1=prev,
        prev2=prev2,
        degraded=True,
    )
    row = next(item for item in result.pool1 if item.family_key == "梗A")
    assert row.score == 50.0 and row.count_now == 6 and row.count_prev == 1
    assert row.status == "normal"
    assert [item.family_key for item in result.pool2] == ["梗A"]
    assert all(item.family_key != "新梗" for item in result.pool1)
    stat = next(item for item in result.stats_rows if item.family_key == "梗A")
    assert stat.reason == "degraded:frozen" and stat.delta == 0.0
    assert result.stats["frozen"] == 1
    assert result.stats["degraded_new_skipped"] == 1
    assert result.transitions == []


# ================================================================ 6. explainer-only
def test_explainer_max_two_per_round() -> None:
    observations = [
        _obs(
            f"种子梗{index}",
            meta=1,
            meta_strength=1.0,
            cred=50.0 + index,
            known=True,
            meta_only=True,
            explainer=True,
        )
        for index in range(3)
    ]
    result = _apply(observations)
    promoted = [row for row in result.pool2 if row.explainer_only]
    assert len(promoted) == 2, "explainer-only 每轮最多 2 条"
    assert result.stats["p2_in_explainer"] == 2
    # 未放行的仍在 pool1，且不标 explainer_only
    not_promoted = [
        row for row in result.pool1 if row.family_key not in {p.family_key for p in promoted}
    ]
    assert len(not_promoted) == 1
    assert not_promoted[0].explainer_only is False


def test_meta_only_provisional_cannot_use_explainer_path() -> None:
    """meta_only 且 provisional 不直接进池2。"""
    result = _apply(
        [_obs("雪豹", meta=1, meta_strength=1.0, cred=41.0, meta_only=True, explainer=True)]
    )
    assert result.pool1[0].status == "provisional"
    assert result.pool2 == []


def test_explainer_idle_demoted_after_three_rounds() -> None:
    prev1 = {
        "尊嘟假嘟": _row(
            "尊嘟假嘟",
            score=56.5,
            count_now=0,
            zero_streak=2,
            meta_only=True,
            explainer_only=True,
        )
    }
    prev2 = {"尊嘟假嘟": Pool2Row(family_key="尊嘟假嘟", explainer_only=True)}
    result = _apply(
        [_obs("尊嘟假嘟", discovered=False)],
        prev1=prev1,
        prev2=prev2,
    )
    assert result.pool1[0].zero_streak == 3
    assert result.pool2 == []
    assert any(t.reason == "p2:out_explainer_idle" for t in result.transitions)


def test_provisional_grace_two_rounds_then_evicted() -> None:
    start = _apply(
        [_obs("雪豹", meta=1, meta_strength=1.0, cred=41.5, meta_only=True, discovered=True)],
        round_id=1,
    )
    assert start.pool1[0].status == "provisional"
    assert start.pool1[0].zero_streak == 1
    prev1, prev2 = pool_state_from_result(start)
    round2 = _apply([_obs("雪豹", discovered=False)], prev1=prev1, prev2=prev2, round_id=2)
    assert round2.pool1[0].status == "provisional", "宽限期不按 P1_out 撤池"
    assert "zero:provisional_grace" in round2.stats_rows[0].reason
    assert round2.pool1[0].zero_streak == 2
    prev1, prev2 = pool_state_from_result(round2)
    round3 = _apply([_obs("雪豹", discovered=False)], prev1=prev1, prev2=prev2, round_id=3)
    assert round3.pool1[0].status == "out", "第 3 轮起按正常零惩罚淘汰"
    assert "zero:streak" in round3.stats_rows[0].reason


# ================================================================ 7. 粘性身份
def test_group_families_sticky_alias_keeps_prev_family_key() -> None:
    """canonical 抖动时，用上一轮 pool1 别名把 family 归回旧 key。"""
    fake_family = SimpleNamespace(
        family_key="雪豹二创",
        canonical="雪豹二创",
        norm="雪豹二创",
        members=["雪豹"],
        candidates=[],
        meta=[],
        known=False,
        seed_origin="",
        sources=["pattern"],
        meme_prob=0.3,
    )
    prev = {
        "雪豹": Pool1Row(
            family_key="雪豹",
            canonical="雪豹",
            aliases=["雪豹"],
            status="normal",
            score=45.0,
        )
    }
    groups = _group_families([fake_family], prev, Knowledge())
    assert list(groups) == ["雪豹"], "别名命中上一轮 family → sticky 回旧 key"
    assert groups["雪豹"].canonical == "雪豹"


# ================================================================ 8. 配置 / CLI
def test_sat_and_config_defaults_match_spec() -> None:
    assert sat(20, 40) == 0.5
    assert sat(80, 40) == 1.0
    config = PoolConfig()
    assert (config.p1_in, config.p1_out, config.p2_in, config.p2_out) == (40, 32, 65, 55)
    assert (config.ratio_alpha, config.ratio_k, config.ratio_cap) == (5, 5, 10)


def test_sample_from_json_roundtrip() -> None:
    sample = _sample(
        comments=[_comment("c1", "尊嘟假嘟")],
        danmaku=[_danmaku("d1", "尊嘟假嘟", dmid="dm1")],
        evidence_comments=[_comment("e1", "尊嘟假嘟", bvid="BV9")],
        targets=[_target("BV1", "尊嘟假嘟是什么梗")],
    )
    payload = sample.to_dict()
    path = Path(__file__).resolve().parent / "_tmp_window_phase_d.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        restored = sample_from_json(path)
    finally:
        path.unlink(missing_ok=True)
    assert len(restored.comments) == 1 and restored.comments[0].id == "c1"
    assert len(restored.danmaku) == 1 and restored.danmaku[0].dmid == "dm1"
    assert len(restored.comment_targets) == 1
    assert restored.comment_targets[0].title == "尊嘟假嘟是什么梗"


def test_cli_no_db_json(tmp_path: Path) -> None:
    sample = _sample(
        comments=[_comment(f"c{i}", "尊嘟假嘟") for i in range(3)],
        targets=[_target("BV1", "尊嘟假嘟是什么梗")],
    )
    window_path = tmp_path / "window.json"
    window_path.write_text(json.dumps(sample.to_dict(), ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.pool_round",
            "--window-json",
            str(window_path),
            "--no-db",
            "--json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "HOTMEME_OFFLINE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["stats"]["db_written"] is False
    assert any(row["family_key"] == "尊嘟假嘟" for row in payload["pool1"])


# ================================================================ 9. Postgres 集成
class _Rollback(Exception):
    """用异常让 state.transaction 回滚，保证测试不污染生产数据。"""


def _db_fixture_round(conn, *, status: str = "ok") -> int:
    return int(
        conn.execute(
            "INSERT INTO round (started_at, window_start, window_end, status, stats) "
            "VALUES (to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), %s, %s::jsonb) "
            "RETURNING id",
            (NOW, NOW - WINDOW, NOW, status, json.dumps({"phase_c": {"x": 1}})),
        ).fetchone()[0]
    )


def _db_sample() -> WindowSample:
    return _sample(
        comments=[_comment(f"c{i}", "尊嘟假嘟太好笑了") for i in range(3)],
        danmaku=[_danmaku("d1", "尊嘟假嘟", dmid="dm1")],
        evidence_comments=[_comment(f"e{i}", "尊嘟假嘟", bvid=f"BV{i}") for i in range(3)],
        targets=[_target("BV1", "尊嘟假嘟是什么梗")],
        status="ok",
    )


@requires_db
def test_persist_round_postgres_rollback() -> None:
    """写 pool1/pool2/stat/alias/transition；不写 audit_event、不改 round；回滚 0 残留。"""
    from meme_info import state

    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            before = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "round",
                    "pool1",
                    "pool2",
                    "family_round_stat",
                    "family_alias",
                    "pool_transition",
                    "audit_event",
                )
            }
            round_id = _db_fixture_round(conn)
            result = run_pool_round(
                _db_sample(),
                use_db=True,
                db_conn=conn,
                round_id=round_id,
                database_url=DB_URL,
                now=NOW,
            )
            assert result.round_id == round_id
            assert result.stats["db_written"] is True
            assert ("尊嘟假嘟",) in {
                (row[0],) for row in conn.execute("SELECT family_key FROM pool1").fetchall()
            }
            stat = conn.execute(
                "SELECT comment_count, danmaku_count, video_count, meta_count, "
                "count_total, score_after, reason FROM family_round_stat "
                "WHERE round_id = %s AND family_key = %s",
                (round_id, "尊嘟假嘟"),
            ).fetchone()
            assert stat is not None
            assert stat[0] == 3 and stat[1] == 1 and stat[2] == 2 and stat[3] == 1
            assert stat[4] == 6, "count_total = 窗口评论+弹幕+窗口独立视频"
            assert stat[6].startswith("new:")
            alias_rows = conn.execute(
                "SELECT alias, source FROM family_alias WHERE family_key = %s",
                ("尊嘟假嘟",),
            ).fetchall()
            assert alias_rows, "family_alias 必须写入"
            assert ("尊嘟假嘟", "seed") in alias_rows
            # 写库不改 round，也不写 audit_event
            row = conn.execute(
                "SELECT status, stats, comments_sampled FROM round WHERE id = %s", (round_id,)
            ).fetchone()
            assert row[0] == "ok"
            assert row[1]["phase_c"] == {"x": 1}
            assert row[2] == 0
            assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 0
            # 幂等：同一轮重跑不重复 stat（upsert）
            run_pool_round(
                _db_sample(),
                use_db=True,
                db_conn=conn,
                round_id=round_id,
                database_url=DB_URL,
                now=NOW,
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM family_round_stat WHERE round_id = %s",
                    (round_id,),
                ).fetchone()[0]
                == 1
            )
            raise _Rollback

    with state.connection(DB_URL) as conn:
        for table in (
            "round",
            "pool1",
            "pool2",
            "family_round_stat",
            "family_alias",
            "pool_transition",
            "audit_event",
        ):
            assert (
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                == before[table]
            ), f"{table} 测试行必须已回滚"


@requires_db
def test_degraded_round_persist_keeps_pool_membership() -> None:
    """degraded 轮：写 stat（frozen），但不降池、不扣比例。"""
    from meme_info import state

    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            round_id = _db_fixture_round(conn, status="degraded")
            sample = _db_sample()
            sample.status = "degraded"
            result = run_pool_round(
                sample,
                use_db=True,
                db_conn=conn,
                round_id=round_id,
                database_url=DB_URL,
                now=NOW,
            )
            assert result.degraded is True
            assert result.pool1 == [], "degraded 且无上一轮状态：不新进池"
            assert result.stats_rows == []
            assert result.transitions == []
            assert (
                conn.execute(
                    "SELECT count(*) FROM family_round_stat WHERE round_id = %s", (round_id,)
                ).fetchone()[0]
                == 0
            )
            raise _Rollback


def test_existing_p1_explainer_family_can_be_promoted_later() -> None:
    """已在池1但上轮名额未进的 explainer 候选，后续轮可补进池2。"""
    prev1 = {
        "种子梗": _row(
            "种子梗",
            status="normal",
            score=52.0,
            count_now=0,
            zero_streak=1,
            meta_only=True,
        )
    }
    observation = _obs(
        "种子梗",
        meta=1,
        meta_strength=1.0,
        cred=55.0,
        known=True,
        meta_only=True,
        explainer=True,
    )
    result = _apply([observation], prev1=prev1)
    assert [row.family_key for row in result.pool2] == ["种子梗"]
    assert result.pool2[0].explainer_only is True
    assert result.pool1[0].explainer_only is True


def test_existing_explainer_in_pool2_is_kept() -> None:
    """已在池2的 explainer-only：无使用/无新元话语但未满 3 轮 → 保持。"""
    prev1 = {
        "种子梗": _row(
            "种子梗",
            status="normal",
            score=56.0,
            count_now=0,
            zero_streak=1,
            meta_only=True,
            explainer_only=True,
        )
    }
    prev2 = {"种子梗": Pool2Row(family_key="种子梗", explainer_only=True)}
    observation = _obs(
        "种子梗",
        meta=1,
        meta_strength=1.0,
        cred=55.0,
        known=True,
        meta_only=True,
        explainer=True,
    )
    result = _apply([observation], prev1=prev1, prev2=prev2)
    assert [row.family_key for row in result.pool2] == ["种子梗"]
    assert not any(t.from_pool == "pool2" for t in result.transitions)
