from __future__ import annotations

from meme_info.knowledge import Knowledge, parse_origin_year
from meme_info.text import normalize_surface


def test_seed_and_alias_lookup():
    knowledge = Knowledge()
    entry = knowledge.lookup("尊嘟假嘟")
    assert entry is not None
    assert entry.canonical == "尊嘟假嘟"
    assert knowledge.lookup("尊嘟") is not None
    assert knowledge.canonical("尊嘟") == "尊嘟假嘟"


def test_blocklist_marks_news_and_marketing():
    knowledge = Knowledge()
    assert knowledge.blocked_reason("外交部回应记者提问") == "news_like"
    assert knowledge.blocked_reason("关注主播领取优惠券") == "marketing"
    assert knowledge.is_evergreen("吃瓜")
    assert knowledge.blocked_reason("尊嘟假嘟") == ""


def test_origin_year_parsing():
    assert parse_origin_year("2023 谐音卖萌体") == 2023
    assert parse_origin_year("无年份") is None
    assert parse_origin_year("1800 古代") is None


def test_rework_generic_proper_and_common_phrase_filter():
    knowledge = Knowledge()
    assert knowledge.blocked_reason("音乐") == "lexicon"
    assert knowledge.blocked_reason("扎啤") == ""        # 用户盲标 ✅，不得误杀
    assert knowledge.blocked_reason("明码标价") == "common_phrase"
    assert knowledge.blocked_reason("闪身步") == "common_phrase"  # 2026-11-15 用户确认非梗
    # LLM 暂停时，用户 ❌ 锚点必须在规则层拦截
    for anchor in ("非凡之爱", "最怕BLG夺冠之人", "腋毛攻击"):
        assert knowledge.blocked_reason(anchor) == "common_phrase"
    assert knowledge.blocked_reason("吴家村") == ""      # 用户盲标 ✅，proper 只做审计
    assert knowledge.is_proper_like("吴家村")
    assert knowledge.blocked_reason("吃瓜") == ""   # 已知种子豁免
    assert knowledge.blocked_reason("抽象") == ""
