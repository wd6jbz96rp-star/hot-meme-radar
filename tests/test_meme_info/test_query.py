from __future__ import annotations

import json

from meme_info.models import Corpus
from meme_info.query import QueryConfig, query_from_corpus
from meme_info.text import normalize_surface

from conftest import DAY, NOW, make_comment, make_corpus, make_video


def build_corpus() -> Corpus:
    videos = [
        make_video("BVA", "【梗百科】测试梗是啥梗？", aid=1, pubdate=NOW - 2 * DAY,
                   view=3_000_000, like=100_000, reply=500, danmaku=1000,
                   owner="upA", source="search:梗百科"),
        make_video("BVB", "【梗指南】测试梗是什么梗？", aid=2, pubdate=NOW - 5 * DAY,
                   view=1_000_000, like=30_000, reply=200, danmaku=500,
                   owner="upB", source="search:梗指南"),
        make_video("BVC", "无关视频", aid=3, pubdate=NOW - 3 * DAY,
                   view=500_000, like=10_000, reply=100, danmaku=100,
                   owner="upC", source="ranking:119"),
        make_video("BVD", "另一个视频", aid=4, pubdate=NOW - 1 * DAY,
                   view=200_000, like=5_000, reply=50, danmaku=50,
                   owner="upD", source="ranking:160"),
    ]
    comments = [
        make_comment("BVC", "BVC:1", "测试梗太好笑了", ctime=NOW - 3 * DAY, like=100, title="无关视频"),
        make_comment("BVC", "BVC:2", "最近全是测试梗", ctime=NOW - 2 * DAY, like=50, title="无关视频"),
        make_comment("BVC", "BVC:3", "测试梗是什么东西", ctime=NOW - 1 * DAY, like=20, title="无关视频"),
        make_comment("BVD", "BVD:1", "测试梗 打卡", ctime=NOW - 4 * DAY, like=10, title="另一个视频"),
    ]
    return make_corpus(videos, comments)


def test_query_from_corpus_returns_current_meme_and_json():
    report = query_from_corpus(
        build_corpus(),
        now=NOW,
        config=QueryConfig(limit=10, expand_queries=False),
    )
    assert report.memes
    top = report.memes[0]
    assert top.canonical == "测试梗"
    assert top.score > 0
    assert top.status in ("hot", "rising", "observing")
    assert top.signals["is_current"] is True
    payload = report.to_dict()
    json.dumps(payload, ensure_ascii=False)
    assert payload["schema_version"]
    assert payload["memes"][0]["canonical"] == "测试梗"
    assert payload["stats"]["current_families"] >= 1


def test_query_merges_containment_variants():
    videos = [
        make_video("BVM", "【梗百科】吃了不烧心是什么梗", aid=1, pubdate=NOW - DAY,
                   view=3_000_000, source="search:梗百科"),
        make_video("BVC", "无关视频", aid=2, pubdate=NOW - DAY),
    ]
    comments = [
        make_comment("BVC", "BVC:1", "不烧心", ctime=NOW - DAY, like=10),
        make_comment("BVC", "BVC:2", "吃了不烧心", ctime=NOW - 2 * DAY, like=9),
        make_comment("BVC", "BVC:3", "不烧心哈哈", ctime=NOW - 3 * DAY, like=8),
    ]
    report = query_from_corpus(
        make_corpus(videos, comments),
        now=NOW,
        config=QueryConfig(limit=10, expand_queries=False),
    )
    families = [
        meme for meme in report.memes + report.candidates
        if "不烧心" in meme.canonical or any("不烧心" in member for member in meme.members)
    ]
    assert families
    assert len({meme.family_key for meme in families}) == 1


def test_old_corpus_is_not_current():
    videos = [make_video("BVA", "【梗百科】旧梗是啥梗？", aid=1,
                         pubdate=NOW - 200 * DAY, view=1_000_000,
                         source="search:梗百科")]
    comments = [make_comment("BVC", "BVC:1", "旧梗", ctime=NOW - 200 * DAY, like=1)]
    report = query_from_corpus(
        make_corpus(videos, comments),
        now=NOW,
        config=QueryConfig(limit=10, expand_queries=False),
    )
    assert all(meme.canonical != "旧梗" for meme in report.memes)
    assert any(meme.canonical == "旧梗" for meme in report.candidates)


def test_evidence_quotes_are_original_substrings():
    corpus = build_corpus()
    report = query_from_corpus(
        corpus, now=NOW, config=QueryConfig(limit=10, expand_queries=False)
    )
    by_id = {doc.id: doc.text for doc in corpus.docs}
    for meme in report.memes:
        for evidence in meme.evidence:
            assert evidence.quote in by_id[evidence.comment_id]


def test_meta_only_meme_is_kept_but_flagged_without_2x2():
    videos = [
        make_video("BVA", "【梗百科】只有标题的梗是啥梗？", aid=1, pubdate=NOW - DAY,
                   view=2_000_000, source="search:梗百科"),
    ]
    report = query_from_corpus(
        make_corpus(videos, []), now=NOW, config=QueryConfig(limit=10, expand_queries=False)
    )
    # 用户盲标证明 explainer-only 可能真；查询层保留，发布门用 2×2 徽章决定。
    assert any(meme.canonical == "只有标题的梗" for meme in report.memes)
    target = next(meme for meme in report.memes if meme.canonical == "只有标题的梗")
    assert target.signals["evidence_rate_2x2"] is False
