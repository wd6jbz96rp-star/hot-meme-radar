from app import store


def test_cycle_and_signal_roundtrip(tmp_path):
    conn = store.connect(tmp_path / "test.db")
    store.init_db(conn)
    cycle_id = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, cycle_id, "done", {"videos": 3})

    assert store.latest_cycle(conn)["id"] == cycle_id

    store.upsert_videos(
        conn,
        [{"bvid": "BV1", "aid": 1, "cid": 2, "title": "t", "zone": "鬼畜", "view": 10}],
    )
    store.insert_metrics(conn, cycle_id, [{"bvid": "BV1", "view": 10, "zone": "鬼畜"}])

    mapping = store.upsert_memes(
        conn, [{"family_key": "尊嘟假嘟", "canonical": "尊嘟假嘟", "type": "谐音"}]
    )
    meme_id = mapping["尊嘟假嘟"]
    assert store.first_seen_map(conn, ["尊嘟假嘟"])

    store.insert_signals(
        conn,
        cycle_id,
        [{"meme_id": meme_id, "family_key": "尊嘟假嘟", "score": 72.5, "rank": 1,
          "status": "hot", "components": {"exposure": 1.0}, "sample": "例子"}],
    )
    top = store.top_items(conn, cycle_id, 10)
    assert len(top) == 1
    assert top[0]["canonical"] == "尊嘟假嘟"
    assert top[0]["rank"] == 1
    assert top[0]["sample"] == "例子"

    history = store.meme_history(conn, "尊嘟假嘟")
    assert history and history[-1]["score"] == 72.5
    conn.close()


def test_count_since_uses_cycle_timestamps(tmp_path):
    conn = store.connect(tmp_path / "count.db")
    store.init_db(conn)
    first = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, first, "done", {})
    store.upsert_candidates(
        conn, [{"surface": "测试梗", "norm": "测试梗", "count": 5, "df_video": 3, "df_zone": 2}],
        first,
    )
    assert store.count_since(conn, "测试梗", "2026-11-14T00:00:00+08:00") == 5
    assert store.count_since(conn, "测试梗", "2026-11-15T00:00:00+08:00") == 0
    conn.close()


def test_previous_scores_reads_last_cycle(tmp_path):
    conn = store.connect(tmp_path / "prev.db")
    store.init_db(conn)
    first = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, first, "done", {})
    mapping = store.upsert_memes(
        conn, [{"family_key": "甲", "canonical": "甲", "type": "词"}]
    )
    store.insert_signals(
        conn, first,
        [{"meme_id": mapping["甲"], "family_key": "甲", "score": 61.0, "rank": 1,
          "status": "hot", "components": {}, "sample": ""}],
    )
    second = store.start_cycle(conn, "2026-11-14T11:00:00+08:00")
    store.finish_cycle(conn, second, "done", {})
    assert store.previous_scores(conn, second)["甲"] == 61.0
    conn.close()


def test_is_new_persisted_and_exposed(tmp_path):
    conn = store.connect(tmp_path / "isnew.db")
    store.init_db(conn)
    cycle_id = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, cycle_id, "done", {})
    mapping = store.upsert_memes(
        conn, [{"family_key": "新梗", "canonical": "新梗", "type": "词"}]
    )
    store.insert_signals(
        conn, cycle_id,
        [{"meme_id": mapping["新梗"], "family_key": "新梗", "score": 66.0, "rank": 1,
          "status": "hot", "components": {}, "sample": "", "is_new": True}],
    )
    top = store.top_items(conn, cycle_id, 10)
    assert top[0]["is_new"] is True
    # 重启（新连接）后仍可读，不再依赖 pipeline 进程内变量
    conn.close()
    conn = store.connect(tmp_path / "isnew.db")
    assert store.top_items(conn, cycle_id, 10)[0]["is_new"] is True
    conn.close()


def test_init_db_migrates_old_signal_table(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE meme_signal(
               cycle_id INTEGER NOT NULL, meme_id INTEGER NOT NULL,
               family_key TEXT NOT NULL, score REAL NOT NULL, rank INTEGER NOT NULL,
               status TEXT NOT NULL, components_json TEXT NOT NULL DEFAULT '{}',
               sample TEXT DEFAULT '', updated_at TEXT NOT NULL,
               PRIMARY KEY(cycle_id, meme_id));"""
    )
    old.commit()
    old.close()

    conn = store.connect(path)
    store.init_db(conn)  # 不得因缺列打不开旧库
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(meme_signal)")}
    assert "is_new" in columns
    # 旧行默认 is_new=0，接口字段仍然存在
    cycle_id = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, cycle_id, "done", {})
    mapping = store.upsert_memes(
        conn, [{"family_key": "旧梗", "canonical": "旧梗", "type": "词"}]
    )
    store.insert_signals(
        conn, cycle_id,
        [{"meme_id": mapping["旧梗"], "family_key": "旧梗", "score": 55.0, "rank": 1,
          "status": "observing", "components": {}, "sample": ""}],
    )
    assert store.top_items(conn, cycle_id, 10)[0]["is_new"] is False
    conn.close()
