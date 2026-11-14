from __future__ import annotations

from meme_info.text import (
    find_quote,
    normalize_surface,
    normalize_text,
    segments,
    similar_surface,
    is_numeric_like,
)


def test_normalize_surface_removes_punctuation_and_case():
    assert normalize_surface("尊嘟假嘟！") == "尊嘟假嘟"
    assert normalize_surface("YYDS，永远的神") == "yyds永远的神"
    assert normalize_surface("23333") == "2333"
    assert normalize_surface("↑ ↓ ↑ → ←") == "↑↓↑→←"


def test_normalize_text_keeps_words_and_drops_mentions_urls():
    text = "来看 https://example.com @某人 尊嘟假嘟 ！！"
    normalized = normalize_text(text)
    assert "尊嘟假嘟" in normalized
    assert "http" not in normalized
    assert "@" not in normalized


def test_find_quote_returns_original_span_not_fabricated():
    assert find_quote("这个尊嘟假嘟太好笑了", "尊嘟假嘟") == "尊嘟假嘟"
    assert find_quote("Piu Piu Piu 小猫", "piupiupiu") == "Piu Piu Piu"
    assert find_quote("完全无关的一句话", "尊嘟假嘟") == ""


def test_segments_are_stable():
    assert segments("a，b！c") == ["a", "b", "c"]


def test_similar_surface_short_words_do_not_overmerge():
    assert similar_surface("不烧心", "吃了不烧心")
    assert similar_surface("灭建国", "灭建国是谁")
    assert not similar_surface("上头", "上课")
    assert not similar_surface("出圈", "出道")
    assert not similar_surface("人设", "人类")
    assert not similar_surface("野生", "野生狗奶")
    assert similar_surface("MJ", "MJ宇宙")


def test_numeric_like():
    assert is_numeric_like("2333")
    assert is_numeric_like("12万")
    assert not is_numeric_like("尊嘟假嘟")
