from app.normalize import normalize_surface, normalize_text, segments


def test_normalize_text_removes_emoji_and_zero_width():
    raw = "尊嘟假嘟\u200b😀真香"
    cleaned = normalize_text(raw)
    assert "😀" not in cleaned
    assert "\u200b" not in cleaned
    assert "尊嘟假嘟" in cleaned


def test_normalize_text_collapses_repeats():
    assert normalize_text("哈哈哈哈") == "哈哈"
    assert normalize_text("23333") == "2333"


def test_segments_split_on_punctuation():
    assert segments("退退退！真的吗？") == ["退退", "真的吗"]


def test_normalize_surface_is_stable():
    assert normalize_surface("ＹＹＤＳ") == "yyds"
    assert normalize_surface("尊嘟假嘟！！") == "尊嘟假嘟"
