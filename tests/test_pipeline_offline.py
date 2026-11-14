from app import pipeline, store
from app.bili import BiliClient


def test_offline_cycle_produces_snapshot(tmp_path):
    conn = store.connect(tmp_path / "pipeline.db")
    store.init_db(conn)
    client = BiliClient(offline=True)
    snapshot = pipeline.run_cycle(conn=conn, client=client)

    assert snapshot["status"] in ("done", "degraded")
    cycle = snapshot["cycle"]
    assert cycle is not None
    assert cycle["stats"]["videos"] > 0
    assert cycle["stats"]["candidates"] > 0
    assert cycle["stats"]["zones"]
    assert isinstance(snapshot["top"], list)
    for item in snapshot["top"]:
        assert item["rank"] >= 1
        assert 0 <= item["score"] <= 100
    conn.close()


def test_status_requires_two_videos_and_confirmed_rounds():
    """P0-2 回归：稳定榜要求 ≥MIN_VIDEOS_FOR_RANK 个视频且连续 N 轮达标。"""
    item = {"score": 70.0, "components": {"exposure": 0.5}, "is_new": False}
    assert pipeline.status_for(item, {"df_video": 1}, streak=True) == "observing"
    assert pipeline.status_for(item, {"df_video": 2}, streak=True) == "hot"
    # 只达标一轮（未连续）不能 hot
    assert pipeline.status_for(item, {"df_video": 2}, streak=False) == "observing"
    # 新晋路径要求曝光分位 >= NEWCOMER_PERCENTILE，种子不再旁路
    newcomer = {"score": 70.0, "components": {"exposure": 0.99}, "is_new": True}
    assert pipeline.status_for(newcomer, {"df_video": 2}, streak=False) == "hot"
    seed_low = {"score": 70.0, "components": {"exposure": 0.5}, "is_new": True,
                "seed_hit": True}
    assert pipeline.status_for(seed_low, {"df_video": 2}, streak=False) == "observing"


def test_offline_top_items_expose_is_new(tmp_path):
    conn = store.connect(tmp_path / "isnew.db")
    store.init_db(conn)
    snapshot = pipeline.run_cycle(conn=conn, client=BiliClient(offline=True))
    assert snapshot["top"], "离线 fixture 应产出可展示条目"
    for item in snapshot["top"]:
        assert isinstance(item["is_new"], bool)
    conn.close()


def test_confirm_rounds_offsets_current_round(monkeypatch):
    """PM review #3：当前轮尚未落库，rounds 应为 CONFIRM_ROUNDS-1。"""
    from app import config

    monkeypatch.setattr(config, "CONFIRM_ROUNDS", 2)
    assert pipeline.confirm_rounds() == 1
    monkeypatch.setattr(config, "CONFIRM_ROUNDS", 3)
    assert pipeline.confirm_rounds() == 2
    monkeypatch.setattr(config, "CONFIRM_ROUNDS", 1)
    assert pipeline.confirm_rounds() == 1


def test_confirm_rounds_semantics_two_consecutive_cycles(tmp_path):
    """连续两轮 >= 55：前一轮落库 + 本轮达标 => streak 成立（rounds=1）。"""
    conn = store.connect(tmp_path / "streak.db")
    store.init_db(conn)
    first = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, first, "done", {})
    mapping = store.upsert_memes(
        conn, [{"family_key": "甲", "canonical": "甲", "type": "词"}]
    )
    store.insert_signals(
        conn, first,
        [{"meme_id": mapping["甲"], "family_key": "甲", "score": 60.0, "rank": 1,
          "status": "hot", "components": {}, "sample": ""}],
    )
    # 当前轮还没插入信号，只能查到 1 条历史
    assert store.hype_streak(conn, "甲", 55.0, rounds=1) is True
    assert store.hype_streak(conn, "甲", 55.0, rounds=2) is False
    conn.close()
