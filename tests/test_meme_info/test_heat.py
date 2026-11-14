from __future__ import annotations

from meme_info.heat import HeatConfig, HeatModel
from meme_info.knowledge import Knowledge
from meme_info.models import Meme, MetaMention, Usage
from meme_info.text import normalize_surface

from conftest import DAY, NOW


def make_meme(
    canonical: str,
    *,
    meta: int = 1,
    meta_views: int = 1_000_000,
    meta_ages: tuple[int, ...] | None = None,
    usage: int = 6,
    usage_videos: int = 3,
    usage_age_days: int = 2,
    known: bool = False,
    meme_prob: float = 0.8,
    kind: str = "dedicated",
) -> Meme:
    family_key = normalize_surface(canonical)
    meta_ages = meta_ages or tuple([2] * meta)
    meta_items = [
        MetaMention(
            surface=canonical, canonical=canonical, norm=family_key,
            bvid=f"BVM{index}", title=f"{canonical}是什么梗", pubdate=NOW - age * DAY,
            view=meta_views, like=int(meta_views * 0.04), reply=100,
            owner=f"metaup{index}", kind=kind, confidence=0.9,
        )
        for index, age in enumerate(meta_ages)
    ]
    usage_items = [
        Usage(
            doc_id=f"DOC{index}", bvid=f"BVC{index % max(1, usage_videos)}",
            zone="鬼畜", kind="comment", ctime=NOW - usage_age_days * DAY,
            like=10 + index, text=canonical, video_title="comments",
        )
        for index in range(usage)
    ]
    return Meme(
        family_key=family_key, canonical=canonical, norm=family_key,
        members=[canonical], meta=meta_items, usage=usage_items,
        known=known, meme_prob=meme_prob,
        first_seen=min([item.ctime for item in usage_items] + [item.pubdate for item in meta_items]),
        last_seen=max([item.ctime for item in usage_items] + [item.pubdate for item in meta_items]),
    )


def test_score_range_and_stronger_meme_ranks_higher():
    model = HeatModel(HeatConfig())
    strong = make_meme("强梗", meta=3, meta_views=3_000_000, usage=20, usage_videos=5)
    weak = make_meme("弱梗", meta=1, meta_views=20_000, usage=1, usage_videos=1)
    scored = model.score_memes([weak, strong], now=NOW, knowledge=Knowledge())
    assert 0 <= scored[0].score <= 100
    assert scored[0].canonical == "强梗"
    assert scored[0].score > scored[1].score
    assert scored[0].status in ("hot", "rising", "observing")


def test_stale_meme_is_rejected():
    model = HeatModel(HeatConfig())
    stale = make_meme("旧梗", meta=1, meta_views=1_000_000, meta_ages=(80,), usage=0)
    scored = model.score_memes([stale], now=NOW, knowledge=Knowledge(), current_only=True)
    assert scored == []
    full = model.score_memes([stale], now=NOW, knowledge=Knowledge())
    assert full[0].status == "rejected"
    assert full[0].rejection == "stale"


def test_single_video_usage_is_rejected_when_not_known_or_meta():
    model = HeatModel(HeatConfig())
    meme = make_meme("单视频梗", meta=0, usage=5, usage_videos=1, meme_prob=0.8)
    scored = model.score_memes([meme], now=NOW, knowledge=Knowledge())
    assert scored[0].status == "rejected"
    assert scored[0].rejection in ("weak_usage", "single_video", "stale")


def test_known_seed_can_survive_with_recent_usage():
    model = HeatModel(HeatConfig())
    meme = make_meme("吃瓜", meta=0, usage=8, usage_videos=2, known=True)
    scored = model.score_memes([meme], now=NOW, knowledge=Knowledge())
    assert scored[0].status != "rejected"
    # 常青词会打折，但不至于被清零
    assert scored[0].score > 0


def test_recent_evidence_beats_old_evidence():
    model = HeatModel(HeatConfig())
    fresh = make_meme("新证据", meta=1, usage=6, usage_age_days=1)
    old = make_meme("旧证据", meta=1, usage=6, usage_age_days=20)
    scored = model.score_memes([fresh, old], now=NOW, knowledge=Knowledge())
    by_name = {item.canonical: item for item in scored}
    assert by_name["新证据"].score > by_name["旧证据"].score


def test_history_smoothing_keeps_score_comparable():
    model = HeatModel(HeatConfig())
    meme = make_meme("平滑梗", meta=1, usage=6)
    scored = model.score_memes([meme], now=NOW, knowledge=Knowledge(), history={"平滑梗": 80.0})
    assert scored[0].smoothed_score is not None
    assert 0 <= scored[0].smoothed_score <= 100


def test_search_interest_is_discounted_relative_to_editorial_meta():
    model = HeatModel(HeatConfig())
    editorial = make_meme("编辑梗", meta=2, meta_views=1_000_000, usage=2)
    interest = make_meme("兴趣梗", meta=0, usage=2)
    interest.meta = [
        MetaMention(
            surface="兴趣梗", canonical="兴趣梗", norm=normalize_surface("兴趣梗"),
            bvid="BVX", title="兴趣梗 相关视频", pubdate=NOW - DAY,
            view=1_000_000, kind="search_interest", confidence=0.45,
        )
    ]
    scored = model.score_memes([editorial, interest], now=NOW, knowledge=Knowledge())
    by_name = {item.canonical: item for item in scored}
    assert by_name["编辑梗"].signals["meta_views"] > by_name["兴趣梗"].signals["meta_views"]


def test_components_explain_points():
    model = HeatModel(HeatConfig())
    meme = make_meme("解释梗", meta=2, usage=6)
    scored = model.score_memes([meme], now=NOW, knowledge=Knowledge())[0]
    for key in ("meta_reach", "meta_coverage", "organic", "velocity", "engagement", "breadth", "prior"):
        assert key in scored.components
        assert "points" in scored.components[key]
    assert scored.reasons


def test_meta_only_exposes_2x2_badge_but_is_not_hard_dropped():
    model = HeatModel(HeatConfig())
    meta_only = make_meme("只有梗视频", meta=3, meta_views=5_000_000, usage=0)
    scored = model.score_memes([meta_only], now=NOW, knowledge=Knowledge())[0]
    assert scored.signals["evidence_rate_2x2"] is False
    # 用户盲标确认部分 explainer-only 是真梗；2×2 只做证据质量徽章，
    # 是否发布由组装阶段的发布门决定，查询层不硬杀。
    assert scored.status in ("hot", "rising", "observing")


def test_two_comments_two_videos_is_evidence_2x2():
    model = HeatModel(HeatConfig())
    meme = make_meme("有使用", meta=1, usage=4, usage_videos=2)
    scored = model.score_memes([meme], now=NOW, knowledge=Knowledge())[0]
    assert scored.signals["evidence_docs"] >= 2
    assert scored.signals["evidence_videos"] >= 2
    assert scored.signals["evidence_rate_2x2"] is True
