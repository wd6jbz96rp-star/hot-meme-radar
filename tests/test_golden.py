import json
from pathlib import Path

from app.judge import Judger
from app.normalize import normalize_surface

DATA = Path(__file__).resolve().parent.parent / "data"
GOLDEN = Path(__file__).resolve().parent / "golden" / "memes.json"


def _family(surface: str) -> dict:
    norm = normalize_surface(surface)
    return {
        "family_key": norm,
        "canonical": surface,
        "members": [surface],
        "quoted": 0,
        "df_video": 0,
        "df_zone": 0,
        "danmaku_df": 0,
    }


def test_golden_set_precision():
    judger = Judger(DATA)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    positives = [judger.judge(_family(s))[0] for s in golden["positives"]]
    negatives = [judger.judge(_family(s))[0] for s in golden["negatives"]]

    positive_mean = sum(positives) / len(positives)
    negative_mean = sum(negatives) / len(negatives)
    assert positive_mean >= 0.35
    assert negative_mean <= 0.15
    assert positive_mean - negative_mean >= 0.25
    assert all(score >= 0.3 for score in positives)
    assert all(score < 0.45 for score in negatives)
