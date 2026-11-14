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
