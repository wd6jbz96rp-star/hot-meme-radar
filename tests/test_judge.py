from pathlib import Path

from app.judge import Judger, decide
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


def test_decide_thresholds():
    assert decide(80) == "hot"
    assert decide(50) == "observing"
    assert decide(10) == "rejected"
