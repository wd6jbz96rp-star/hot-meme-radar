from datetime import datetime, timedelta

from app.score import percentiles, score_families


def _family(name: str, exposure: float, engagement: float = 1.0, velocity: float = 1.0,
            meme: float = 0.6, first_seen: str | None = None, df_video: int = 3,
            df_zone: int = 2, df_up: int = 2) -> dict:
    return {
        "family_key": name,
        "canonical": name,
        "type": "词",
        "seed_hit": True,
        "members": [name],
        "first_seen": first_seen,
        "df_video": df_video,
        "df_zone": df_zone,
        "df_up": df_up,
        "signals": {
            "exposure": exposure,
            "engagement": engagement,
            "velocity": velocity,
            "meme": meme,
            "concentration": 0.2,
        },
    }


def test_percentiles_handle_ties():
    assert percentiles([1, 1, 1]) == [0.5, 0.5, 0.5]
    result = percentiles([10, 20, 30])
    assert result[2] == 1.0 and result[0] == 0.0


def test_higher_exposure_scores_higher():
    items = score_families(
        [_family("高曝光", 1000), _family("中曝光", 100), _family("低曝光", 10)],
        {},
        datetime.now().astimezone(),
    )
    assert items[0]["canonical"] == "高曝光"
    assert items[0]["score"] > items[-1]["score"]


def test_older_meme_decays():
    now = datetime.now().astimezone()
    fresh = _family("新梗", 100, first_seen=(now - timedelta(hours=2)).isoformat())
    old = _family("老梗", 100, first_seen=(now - timedelta(days=10)).isoformat())
    items = score_families([fresh, old], {}, now)
    by_name = {item["canonical"]: item for item in items}
    assert by_name["新梗"]["score"] > by_name["老梗"]["score"]


def test_ema_smooths_previous_score():
    now = datetime.now().astimezone()
    items = score_families([_family("平滑", 100)], {"平滑": 90.0}, now)
    assert items[0]["components"]["previous"] == 90.0
    assert items[0]["score"] > 80.0
