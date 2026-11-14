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
