from pathlib import Path

from app import config
from app.judge import Judger, passes_meme_gate
from app.normalize import normalize_surface

DATA = Path(__file__).resolve().parent.parent / "data"


def family(canonical: str, **extra) -> dict:
    payload = {
        "family_key": normalize_surface(canonical),
        "canonical": canonical,
        "members": [canonical],
        "quoted": 0,
        "df_video": 0,
        "df_zone": 0,
        "danmaku_df": 0,
    }
    payload.update(extra)
    return payload


def test_seed_meme_scores_high():
    judger = Judger(DATA)
    score, reasons = judger.judge(family("尊嘟假嘟"))
    assert score >= 0.35
    assert "seed_hit" in reasons


def test_news_like_is_penalised():
    judger = Judger(DATA)
    score, reasons = judger.judge(family("某地暴雨预警"))
    assert score < 0.45
    assert "news_like" in reasons


def test_sensitive_word_is_rejected():
    judger = Judger(DATA)
    score, _ = judger.judge(family("赌博网站"))
    assert score == 0.0


def test_breadth_and_danmaku_raise_score():
    judger = Judger(DATA)
    plain, _ = judger.judge(family("纯爱战神"))
    rich, reasons = judger.judge(
        family("纯爱战神", df_video=5, df_zone=3, danmaku_df=6, quoted=2, is_line=True)
    )
    assert rich > plain
    assert "breadth" in reasons and "danmaku_repeat" in reasons


def test_gate_uses_unit_interval():
    """P0 量纲回归：0-1 梗性分只能与 MEME_MIN(0.45) 比，不能与 45/55 比。"""
    assert config.MEME_MIN == 0.45
    assert passes_meme_gate(0.5) is True
    assert passes_meme_gate(0.44) is False
    assert passes_meme_gate(0.0) is False
    # 种子是文档允许的阈值豁免
    assert passes_meme_gate(0.0, seed_hit=True) is True


def test_non_seed_reaches_gate_with_combined_evidence():
    """P0-2 回归：非种子靠「跨视频+跨分区+弹幕重复+引号/后缀+独立成词」可达 0.45。"""
    judger = Judger(DATA)
    combo = family(
        "蕉绿警告", df_video=4, df_zone=2, danmaku_df=8, quoted=1,
        is_line=True, cohesion=6.0, entropy=1.8,
    )
    score, reasons = judger.judge(combo)
    assert "seed_hit" not in reasons
    assert score >= config.MEME_MIN
    assert {"breadth", "danmaku_repeat", "quoted", "cohesion"} <= set(reasons)


def test_two_videos_plus_zone_is_breadth_evidence():
    judger = Judger(DATA)
    _, reasons = judger.judge(family("蕉绿警告", df_video=2, df_zone=1))
    assert "breadth_mid" in reasons


def test_seed_is_not_exempt_from_evergreen():
    """P0-2 回归：种子命中 evergreen 时仍要扣分（降权保留）。"""
    judger = Judger(DATA)
    score, reasons = judger.judge(family("吃瓜"))
    assert "seed_hit" in reasons
    assert reasons["evergreen"] == -0.05
    assert score < 0.35


def test_seed_exempt_from_lexicon_and_proper_noun():
    """PM review #2：已知梗很可能同时是通用词/带专名后缀，种子须豁免。"""
    judger = Judger(DATA)
    score, reasons = judger.judge(family("抽象"))
    assert "seed_hit" in reasons and "lexicon" not in reasons
    assert score >= 0.3
    score, reasons = judger.judge(family("打工人"))
    assert "seed_hit" in reasons and "proper_noun" not in reasons
    assert score >= 0.3
    # 非种子仍受限
    _, reasons = judger.judge(family("凡尔赛"))
    assert "lexicon" in reasons or "seed_hit" in reasons


def test_fraction_below_threshold_is_not_rounded_up():
    """0.45 边界不能被浮点误差卡住（0.15+0.10+0.10+0.10 浮点 < 0.45）。"""
    judger = Judger(DATA)
    score, reasons = judger.judge(family(
        "蕉绿警告", df_video=3, df_zone=2, quoted=1, danmaku_df=5,
        is_line=True, cohesion=6.0, entropy=1.8,
    ))
    assert {"breadth", "quoted", "danmaku_repeat", "cohesion"} <= set(reasons)
    assert score == 0.45
    assert passes_meme_gate(score) is True
