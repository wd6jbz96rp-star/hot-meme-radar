"""P1 回归：评论翻页、comment_backed 通道、novelty/年龄、新旧榜与 API 过滤。"""
import importlib
from datetime import datetime, timedelta, timezone

import pytest

from app import config, pipeline, score, store
from app.judge import parse_origin_year

TZ = timezone(timedelta(hours=8))
FROZEN = datetime(2026, 11, 14, 12, 0, tzinfo=TZ)
NOW_TS = FROZEN.timestamp()


def _comment(rpid, text, like=1, ctime=None):
    return {"rpid": rpid, "text": text, "like": like,
            "ctime": int(ctime if ctime is not None else NOW_TS - 2 * 86400)}


class PagingClient:
    """按 (mode, page) 返回不同页，cursor 模拟 reply/main 的 next。"""

    def __init__(self, pages: dict[tuple[int, int], tuple[list[dict], int]]):
        self.pages = pages
        self.calls: list[tuple[int, int]] = []

    def comment_page(self, aid, page=1, mode=3, cursor=None, ps=20):
        self.calls.append((mode, page))
        items, cursor = self.pages.get((mode, page), ([], 0))
        return [dict(item) for item in items], cursor

    def comments(self, aid, page=1, mode=3, cursor=None, ps=20):
        return self.comment_page(aid, page=page, mode=mode, cursor=cursor, ps=ps)[0]

    def cid_for(self, bvid):
        return None

    def danmaku(self, cid):
        return ""


def _video():
    return {"bvid": "BV1", "aid": 42, "zone": "鬼畜"}


# ---------------- 评论翻页：mode2 3 页、mode3 1 页 ----------------

def test_comment_pages_per_mode_and_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_now", lambda: FROZEN)
    hot = [_comment(i, f"热评{i}", 100 - i) for i in range(1, 21)]
    time_pages = {
        (2, 1): ([_comment(100 + i, f"新评1-{i}") for i in range(1, 21)], 20),
        (2, 2): ([_comment(200 + i, f"新评2-{i}") for i in range(1, 21)], 40),
        (2, 3): ([_comment(300 + i, f"新评3-{i}") for i in range(1, 21)], 0),
    }
    client = PagingClient({(3, 1): (hot, 0), **time_pages})
    conn = store.connect(tmp_path / "pages.db")
    store.init_db(conn)
    docs, stats = pipeline.collect_docs(
        client, conn, 1, [], [_video()], [],
    )
    assert client.calls == [(3, 1), (2, 1), (2, 2), (2, 3)]
    assert stats["comment_requests"] == 4
    assert stats["comment_cached"] == 0
    # 80 条候选按每 mode 20 条截断：评论 40 条（配置上限）
    assert stats["comments"] == config.COMMENT_MAX_PER_VIDEO == 40
    assert stats["comment_modes"]["3"] == 20 and stats["comment_modes"]["2"] == 20
    assert len([d for d in docs if d.kind == "comment"]) == 40

    # warm 轮：4 页全部命中缓存，0 请求，评论口径不变
    _, second = pipeline.collect_docs(client, conn, 2, [], [_video()], [])
    assert second["comment_requests"] == 0
    assert second["comment_cached"] == 4
    assert second["comments"] == 40
    assert client.calls == [(3, 1), (2, 1), (2, 2), (2, 3)]
    conn.close()


def test_short_page_with_next_cursor_keeps_paging(tmp_path, monkeypatch):
    """实测 mode=2 首页只回 3–9 条但 next 非 0：不能按「短页」提前停。"""
    monkeypatch.setattr(pipeline, "_now", lambda: FROZEN)
    pages = {
        (3, 1): ([_comment(1, "热评")], 0),
        (2, 1): ([_comment(11, "时间页一")], 20),
        (2, 2): ([_comment(12, "时间页二")], 40),
        (2, 3): ([_comment(13, "时间页三")], 0),
    }
    client = PagingClient(pages)
    conn = store.connect(tmp_path / "short.db")
    store.init_db(conn)
    _, stats = pipeline.collect_docs(client, conn, 1, [], [_video()], [])
    assert client.calls == [(3, 1), (2, 1), (2, 2), (2, 3)]
    assert stats["comments"] == 4
    conn.close()


# ---------------- comment_backed 通道 ----------------

def test_comment_backed_thresholds():
    evidence = {"comment_count_recent": 3, "comment_df_video": 2, "comment_7d_count": 1}
    item = {"seed_hit": False}
    assert pipeline.comment_backed_candidate(item, evidence) is True
    # 种子不走旁路
    assert pipeline.comment_backed_candidate({"seed_hit": True}, evidence) is False
    # 三个门槛逐一不满足
    assert pipeline.comment_backed_candidate(
        item, {**evidence, "comment_count_recent": 2}) is False
    assert pipeline.comment_backed_candidate(
        item, {**evidence, "comment_df_video": 1}) is False
    assert pipeline.comment_backed_candidate(
        item, {**evidence, "comment_7d_count": 0}) is False
    # 门槛就是「绕过 MEME_MIN」的那条路：低梗性分本身过不了门
    assert pipeline.passes_meme_gate(0.1, False) is False


def test_offline_cycle_has_comment_backed_stats(tmp_path):
    from app.bili import BiliClient

    conn = store.connect(tmp_path / "stats.db")
    store.init_db(conn)
    snapshot = pipeline.run_cycle(conn=conn, client=BiliClient(offline=True))
    stats = snapshot["cycle"]["stats"]
    assert "comment_backed_count" in stats and "comment_backed_admitted" in stats
    assert stats["comment_backed_count"] >= stats["comment_backed_admitted"] >= 0
    for item in snapshot["top"]:
        assert item["list"] in ("new", "classic")
        assert "comment_backed" in item["components"]["raw"]
        assert "comment_evidence" in item["components"]["raw"]
        assert item["components"]["raw"]["comment_evidence"]["novelty"] >= 0
    conn.close()


# ---------------- 种子年份解析 ----------------

def test_parse_origin_year():
    assert parse_origin_year("2020 围观") == 2020
    assert parse_origin_year("2024 猫咪空耳") == 2024
    assert parse_origin_year("2019 某梗") == 2019
    assert parse_origin_year("春节 年度梗") is None
    assert parse_origin_year("") is None
    assert parse_origin_year("2026 新梗") == 2026
    assert parse_origin_year("12345 年") is None


def test_known_age_years_midpoint():
    now = datetime(2026, 11, 14, 12, 0, tzinfo=TZ)
    assert pipeline.known_age_years(2025, now) == pytest.approx(1.37, abs=0.01)
    assert pipeline.known_age_years(2024, now) == pytest.approx(2.37, abs=0.01)
    assert pipeline.known_age_years(2026, now) == pytest.approx(0.37, abs=0.01)
    assert pipeline.known_age_years(2027, now) == 0.0
    assert pipeline.known_age_years(None, now) is None


# ---------------- novelty 公式 ----------------

def _evidence(**overrides):
    evidence = {
        "comment_7d_count": 10,
        "comment_growth_7d": 3.0,
        "comment_df_video": 3,
        "age_days": 5.0,
    }
    evidence.update(overrides)
    return evidence


def test_novelty_formula_is_bounded_and_monotonic():
    now = datetime(2026, 11, 14, 12, 0, tzinfo=TZ)
    value = pipeline.novelty_score(_evidence(), None, now)
    expected = (0.5 ** ((5.0 / 365.25) / config.NOVELTY_HALF_LIFE_YEARS)) * (
        0.5 * (__import__("math").log1p(10) / __import__("math").log1p(20))
        + 0.3 * (3 / 5.0)
        + 0.2 * ((3.0 - 1.0) / 4.0)
    )
    assert value == pytest.approx(expected, abs=0.002)
    assert 0.0 <= value <= 1.0
    # 7 天评论越多越新；覆盖越广越新
    assert pipeline.novelty_score(_evidence(comment_7d_count=3), None, now) < value
    assert pipeline.novelty_score(_evidence(comment_df_video=1), None, now) < value
    # 年龄越大 novelty 越低（种子已知年龄优先）
    seed_2025 = pipeline.novelty_score(_evidence(), 2025, now)
    seed_2024 = pipeline.novelty_score(_evidence(), 2024, now)
    seed_2020 = pipeline.novelty_score(_evidence(), 2020, now)
    assert seed_2025 > seed_2024 > seed_2020
    assert seed_2025 < value  # 非种子按最早评论（5 天）更年轻
    # 无评论 → 0，不奖励只有曝光/弹幕的老梗
    empty = {"comment_7d_count": 0, "comment_growth_7d": 1.0,
             "comment_df_video": 0, "age_days": None}
    assert pipeline.novelty_score(empty, 2020, now) == 0.0
    assert pipeline.novelty_score(empty, None, now) == 0.0


# ---------------- 新旧榜分类 ----------------

def test_classify_list_rules():
    assert pipeline.classify_list(True, 2024, 3, 2) == "new"
    assert pipeline.classify_list(True, 2025, 3, 2) == "new"
    assert pipeline.classify_list(True, 2023, 3, 2) == "classic"
    assert pipeline.classify_list(True, None, 3, 2) == "classic"   # 不猜年份
    assert pipeline.classify_list(False, None, 10, 2) == "new"
    assert pipeline.classify_list(False, None, 10, 0) == "classic"  # 无 7 天评论
    assert pipeline.classify_list(False, None, 120, 5) == "classic"  # 超过 90 天
    assert pipeline.classify_list(False, None, None, 0) == "classic"


# ---------------- 打分权重与组件 ----------------

def test_score_weights_sum_one_and_novelty_component():
    assert sum(config.WEIGHTS.values()) == pytest.approx(1.0)
    assert config.WEIGHTS["novelty"] == pytest.approx(0.05)
    assert config.WEIGHTS["exposure"] == pytest.approx(0.25)

    def family(name, novelty):
        return {
            "family_key": name, "canonical": name, "seed_hit": True,
            "df_video": 3, "df_zone": 2, "df_up": 2,
            "signals": {"exposure": 100.0, "engagement": 10.0, "velocity": 1.0,
                        "meme": 0.6, "concentration": 0.2, "novelty": novelty},
        }

    now = datetime.now().astimezone()
    items = score.score_families([family("新", 0.8), family("旧", 0.0)], {}, now)
    by_name = {item["canonical"]: item for item in items}
    assert by_name["新"]["components"]["novelty"] == 0.8
    assert by_name["旧"]["components"]["novelty"] == 0.0
    assert by_name["新"]["score"] > by_name["旧"]["score"]


# ---------------- API list 过滤 ----------------

def _seed_signal_db(path):
    conn = store.connect(path)
    store.init_db(conn)
    cycle_id = store.start_cycle(conn)
    store.finish_cycle(conn, cycle_id, "done", {})
    names = [("新梗A", "new"), ("新梗B", "new"), ("老梗C", "classic")]
    meme_ids = store.upsert_memes(conn, [
        {"family_key": name, "canonical": name, "type": "词", "origin": ""}
        for name, _ in names
    ])
    signals = [
        {"meme_id": meme_ids[name], "family_key": name, "score": 80 - index,
         "rank": index + 1, "status": "hot",
         "components": {"list": list_type, "comment_backed": list_type == "new",
                        "raw": {"list": list_type}},
         "sample": "", "is_new": True}
        for index, (name, list_type) in enumerate(names)
    ]
    # rank=0 的 comment_backed 观察项：不占主榜，但应出现在 list=new
    extra = store.upsert_memes(conn, [
        {"family_key": "新梗D", "canonical": "新梗D", "type": "词", "origin": ""}
    ])
    signals.append(
        {"meme_id": extra["新梗D"], "family_key": "新梗D", "score": 60.0, "rank": 0,
         "status": "observing",
         "components": {"list": "new", "comment_backed": True,
                        "raw": {"list": "new", "comment_backed": True}},
         "sample": "", "is_new": True}
    )
    store.insert_signals(conn, cycle_id, signals)
    return conn


def test_api_top_list_filter(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    conn = _seed_signal_db(tmp_path / "api_p1.db")
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api_p1.db")
    from app import api as api_module

    api_module = importlib.reload(api_module)
    with TestClient(api_module.app) as client:
        everything = client.get("/api/top").json()
        assert {item["canonical"] for item in everything["top"]} == {
            "新梗A", "新梗B", "老梗C"}
        assert everything["list"] == "all"

        new = client.get("/api/top?list=new").json()
        # 含 rank=0 的 comment_backed 观察项：new 榜是曝光通道，不占主榜名次
        assert [item["canonical"] for item in new["top"]] == ["新梗A", "新梗B", "新梗D"]
        assert all(item["list"] == "new" for item in new["top"])

        classic = client.get("/api/top?list=classic").json()
        assert [item["canonical"] for item in classic["top"]] == ["老梗C"]
        assert classic["top"][0]["list"] == "classic"

        assert client.get("/api/top?list=bogus").status_code == 400
