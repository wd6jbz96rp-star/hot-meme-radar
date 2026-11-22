from __future__ import annotations

import json
from pathlib import Path

from meme_info.bili import BiliSource, parse_comment_page, parse_danmaku_xml, parse_hot_search
from meme_info.bili import parse_popular, parse_ranking, parse_search
from meme_info.http import HttpClient

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_ranking_and_popular():
    ranking = parse_ranking(load("ranking_1.json"), 1)
    assert len(ranking) == 4
    assert ranking[0].bvid.startswith("BV")
    assert ranking[0].view > 0
    assert ranking[0].source == "ranking:1"
    popular = parse_popular(load("popular.json"))
    assert len(popular) == 4
    assert popular[0].source == "popular"


def test_parse_comments_dedupes_top_replies_and_returns_cursor():
    comments, cursor, is_end = parse_comment_page(
        load("reply_main.json"), bvid="BV1xx", aid=1, zone="鬼畜", video_title="t", mode=3
    )
    assert comments
    assert len({item.id for item in comments}) == len(comments)
    assert cursor > 0
    assert is_end is False
    assert all(item.ctime > 0 for item in comments)


def test_parse_danmaku_xml():
    xml = (FIXTURES / "danmaku.xml").read_text(encoding="utf-8")
    items = parse_danmaku_xml(xml, bvid="BV1xx", zone="鬼畜", video_title="t")
    assert items
    assert items[0].ctime > 0
    assert items[0].progress >= 0
    assert all(item.text for item in items)


def test_parse_search_strips_highlight_tags():
    videos = parse_search(load("search.json"), "热梗")
    assert videos
    assert videos[0].source == "search:热梗"
    assert "<em" not in videos[0].title


def test_parse_hot_search():
    payload = {"data": {"trending": {"list": [
        {"keyword": "A", "heat_score": 10},
        {"show_name": "B", "heat_score": 5},
        {"keyword": ""},
    ]}}}
    items = parse_hot_search(payload)
    assert [item["keyword"] for item in items] == ["A", "B"]


def test_offline_source_uses_fixtures_and_returns_videos():
    source = BiliSource(
        http=HttpClient(max_rps=1000),
        offline=True,
        fixture_dir=FIXTURES,
    )
    assert source.ranking(1)
    assert source.popular(10)
    assert source.search_videos("热梗", 10)
    assert source.hot_search() == []


def test_bootstrap_reads_wbi_keys_offline():
    source = BiliSource(http=HttpClient(max_rps=1000), offline=True, fixture_dir=FIXTURES)
    source.bootstrap()
    assert source._wbi_keys is not None
    query = source.sign({"search_type": "video", "keyword": "热梗", "page": 1})
    assert "w_rid=" in query and "wts=" in query


def test_auth_cookies_from_env(monkeypatch, fixture_dir):
    from meme_info.bili import BiliSource
    from meme_info.http import HttpClient

    monkeypatch.setenv("BILI_SESSDATA", "%2Ctest-sess")
    monkeypatch.setenv("BILI_JCT", "test-jct")
    monkeypatch.setenv("BILI_DEDEUSERID", "12345")
    source = BiliSource(
        http=HttpClient(max_rps=1000), offline=True, fixture_dir=fixture_dir
    )
    source.bootstrap()
    assert source.http.cookie_value("SESSDATA") == "%2Ctest-sess"
    assert source.http.cookie_value("bili_jct") == "test-jct"
    assert source.http.cookie_value("DedeUserID") == "12345"
