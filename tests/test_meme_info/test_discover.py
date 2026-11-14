from __future__ import annotations

from meme_info.discover import (
    DiscoveryConfig,
    extract_candidates,
    extract_meta_mentions,
    merge_families,
    parse_title_meta,
)
from meme_info.knowledge import Knowledge
from meme_info.text import normalize_surface

from conftest import DAY, NOW, make_comment, make_corpus, make_video


def build_corpus():
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
        make_comment("BVD", "BVD:2", "上课去了", ctime=NOW - 1 * DAY, like=1, title="另一个视频"),
    ]
    return make_corpus(videos, comments)


def test_parse_title_meta_patterns():
    knowledge = Knowledge()
    titles = {
        "【梗百科】吴家村是啥梗？": "吴家村",
        "【梗指南】NTR是什么梗": "NTR",
        "扎啤是什么梗？【梗学堂】": "扎啤",
        "我怀疑大禹治水是啥梗": "大禹治水",
    }
    for title, expected in titles.items():
        mentions = parse_title_meta(title, knowledge)
        assert any(item[0] == expected for item in mentions), (title, mentions)


def test_compilation_title_extracts_multiple_names():
    knowledge = Knowledge()
    mentions = parse_title_meta(
        "盘点近期网络热梗：PiuPiuPiu、请善待二旬老人、腋毛攻击", knowledge
    )
    names = {item[0] for item in mentions}
    assert {"PiuPiuPiu", "请善待二旬老人", "腋毛攻击"} <= names


def test_meta_titles_do_not_capture_video_topics():
    knowledge = Knowledge()
    assert parse_title_meta("【原神】2026年网络热梗合集", knowledge) == []
    assert not any(
        item[0] == "循环歌单"
        for item in parse_title_meta("2026网络热梗#循环歌单#中国也能飞", knowledge)
    )


def test_discovery_builds_meme_with_two_meta_videos_and_usage():
    corpus = build_corpus()
    knowledge = Knowledge()
    mentions = extract_meta_mentions(corpus.videos, knowledge, now=NOW)
    candidates = extract_candidates(
        corpus.docs, knowledge, now=NOW, meta_mentions=mentions,
        config=DiscoveryConfig(),
    )
    memes = merge_families(candidates, knowledge, now=NOW, meta_mentions=mentions)
    meme = next(item for item in memes if item.canonical == "测试梗")
    assert len(meme.meta) >= 2
    assert len({item.bvid for item in meme.usage}) >= 2
    assert meme.meme_prob >= 0.5
    assert meme.known is False
    for evidence in meme.evidence:
        assert evidence.quote
        # 证据必须来自原文，不是模型编的
        source = next(doc for doc in corpus.docs if doc.id == evidence.comment_id)
        assert evidence.quote in source.text


def test_short_words_are_not_overmerged():
    videos = [
        make_video("BVA", "视频A", aid=1, pubdate=NOW - DAY),
        make_video("BVB", "视频B", aid=2, pubdate=NOW - DAY),
    ]
    comments = [
        make_comment("BVA", "BVA:1", "上头", ctime=NOW - DAY, like=10),
        make_comment("BVA", "BVA:2", "上课", ctime=NOW - DAY, like=9),
        make_comment("BVB", "BVB:1", "出圈", ctime=NOW - DAY, like=8),
        make_comment("BVB", "BVB:2", "出道", ctime=NOW - DAY, like=7),
    ]
    corpus = make_corpus(videos, comments)
    knowledge = Knowledge()
    mentions = extract_meta_mentions(corpus.videos, knowledge, now=NOW)
    candidates = extract_candidates(corpus.docs, knowledge, now=NOW, meta_mentions=mentions)
    memes = merge_families(candidates, knowledge, now=NOW, meta_mentions=mentions)
    by_name = {item.canonical: item for item in memes}
    if "上头" in by_name:
        assert "上课" not in by_name["上头"].members
    if "出圈" in by_name:
        assert "出道" not in by_name["出圈"].members


def test_variants_merge_containment():
    videos = [
        make_video("BVA", "【梗百科】吃了不烧心是什么梗", aid=1, pubdate=NOW - DAY,
                   view=3_000_000, source="search:梗百科"),
    ]
    comments = [
        make_comment("BVC", "BVC:1", "不烧心", ctime=NOW - DAY, like=10),
        make_comment("BVC", "BVC:2", "吃了不烧心", ctime=NOW - DAY, like=9),
    ]
    corpus = make_corpus(videos + [make_video("BVC", "无关", aid=3, pubdate=NOW - DAY)], comments)
    knowledge = Knowledge()
    mentions = extract_meta_mentions(corpus.videos, knowledge, now=NOW)
    candidates = extract_candidates(corpus.docs, knowledge, now=NOW, meta_mentions=mentions)
    memes = merge_families(candidates, knowledge, now=NOW, meta_mentions=mentions)
    families = [item for item in memes if "不烧心" in item.canonical or "不烧心" in item.members]
    assert len(families) == 1
    assert len(families[0].usage) >= 2


def test_strong_usage_without_meta_still_passes_meme_prob_gate():
    videos = [
        make_video(f"BVU{index}", f"使用视频{index}", aid=index + 10,
                   pubdate=NOW - DAY, zone="鬼畜" if index < 3 else "生活")
        for index in range(6)
    ]
    comments = []
    for index in range(18):
        comments.append(
            make_comment(
                f"BVU{index % 6}", f"BVU{index}:{index}", f"「新表达」真好用{index}",
                ctime=NOW - (index % 5 + 1) * DAY, like=index,
                title=f"使用视频{index % 6}",
                zone="鬼畜" if index % 6 < 3 else "生活",
            )
        )
    # 让候选是 3 字以上、可被 n-gram / 模式抽到的表达
    corpus = make_corpus(videos, comments)
    knowledge = Knowledge()
    mentions = extract_meta_mentions(corpus.videos, knowledge, now=NOW)
    candidates = extract_candidates(corpus.docs, knowledge, now=NOW, meta_mentions=mentions)
    memes = merge_families(candidates, knowledge, now=NOW, meta_mentions=mentions)
    meme = next((item for item in memes if item.canonical == "新表达"), None)
    assert meme is not None
    assert meme.meme_prob >= 0.56
    assert len({item.bvid for item in meme.usage}) >= 3


def test_filter_audit_reports_reprojection_reasons():
    videos = [
        make_video("BVP", "【梗百科】吴家村是啥梗？", aid=1, pubdate=NOW - DAY,
                   view=1_000_000, source="search:梗百科"),
        make_video("BVQ", "音乐视频", aid=2, pubdate=NOW - DAY),
    ]
    comments = [
        make_comment("BVQ", "BVQ:1", "音乐", ctime=NOW - DAY, like=1),
        make_comment("BVQ", "BVQ:2", "音乐", ctime=NOW - 2 * DAY, like=2),
        make_comment("BVQ", "BVQ:3", "音乐", ctime=NOW - 3 * DAY, like=3),
    ]
    corpus = make_corpus(videos, comments)
    knowledge = Knowledge()
    mentions = extract_meta_mentions(corpus.videos, knowledge, now=NOW)
    audit: dict = {"names": ["吴家村", "音乐"]}
    candidates = extract_candidates(
        corpus.docs, knowledge, now=NOW, meta_mentions=mentions, audit=audit
    )
    assert any(item.norm == "吴家村" for item in candidates)   # 用户盲标 ✅
    named = audit["named"]
    assert named["吴家村"]["kept"] is True
    assert named["音乐"]["kept"] is False
    assert named["音乐"]["reason"] in ("lexicon", "common_word")
    assert named["吴家村"]["doc_mentions"] >= 1
    assert audit["blocked_by_reason"]
