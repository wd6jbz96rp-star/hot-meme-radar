"""Wave 2B 工程修复回归：限速/curl 计数、cid 复用、评论缓存、采集单飞、health。"""
import asyncio
import importlib
import os
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config, http, pipeline, store
from app.bili import BiliClient


# ---------------- 限速与 curl 兜底 ----------------

class FakeClock:
    def __init__(self, start: float = 100.0):
        self.t = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_default_max_rps_is_two(monkeypatch):
    if "HOTMEME_MAX_RPS" not in os.environ:
        assert config.MAX_RPS == 2.0


def test_rate_limiter_start_to_start_interval(monkeypatch):
    """start-to-start：耗时 < 间隔要补 sleep；耗时 > 间隔不额外 sleep（正常）。"""
    clock = FakeClock()
    monkeypatch.setattr(http, "time", types.SimpleNamespace(
        monotonic=clock.monotonic, sleep=clock.sleep))
    limiter = http.RateLimiter(max_rps=2.0)

    limiter.wait()                       # 首次无等待，开始于 t=100.0
    assert clock.sleeps == []
    assert clock.t == pytest.approx(100.0)

    clock.t += 0.1                       # 模拟请求耗时 100ms < 500ms
    limiter.wait()
    assert clock.sleeps == [pytest.approx(0.4)]
    assert clock.t == pytest.approx(100.5)   # 第二次开始 = 上次开始 + 1/rps

    clock.t += 0.8                       # 模拟请求耗时 800ms > 500ms
    limiter.wait()
    assert len(clock.sleeps) == 1        # 网络往返已超过间隔，不再补 sleep
    assert clock.t == pytest.approx(101.3)


def test_request_counter_is_thread_safe():
    client = http.HttpClient(max_rps=1000)

    def worker():
        for _ in range(100):
            client._count_request("https://api.bilibili.com/x/web-interface/popular")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert client.request_count() == 800
    assert client.calls["popular"] == 800


def test_curl_get_waits_before_request_and_counts(monkeypatch):
    client = http.HttpClient(max_rps=2.0)
    events: list[str] = []
    monkeypatch.setattr(http.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(client.limiter, "wait", lambda: events.append("wait"))

    def fake_run(command, capture_output=True, timeout=None):
        events.append("run")
        return types.SimpleNamespace(returncode=0, stdout=b"body")

    monkeypatch.setattr(http.subprocess, "run", fake_run)
    assert client._curl_get("https://api.bilibili.com/x/v2/reply", "") == b"body"
    assert events == ["wait", "run"]     # 先限速、再发请求
    assert client.request_count() == 1   # curl 兜底计入请求数
    assert client.calls["comments"] == 1


# ---------------- cid 持久化 ----------------

class FakeClient:
    """只实现 collect_docs 需要的接口，用于统计真实网络调用次数。"""

    def __init__(self, cid: int | None = 222, comments: list[dict] | None = None):
        self.cid_calls: list[str] = []
        self.comment_calls: list[tuple[int, int]] = []
        self._cid = cid
        self._comments = comments if comments is not None else [
            {"text": "这是什么梗", "like": 1, "at": 0}]

    def cid_for(self, bvid: str) -> int | None:
        self.cid_calls.append(bvid)
        return self._cid

    def danmaku(self, cid: int) -> str:
        return ""

    def comments(self, aid: int, page: int) -> list[dict]:
        self.comment_calls.append((aid, page))
        return [dict(item) for item in self._comments]


def test_upsert_videos_preserves_cid_on_null(tmp_path):
    conn = store.connect(tmp_path / "cid.db")
    store.init_db(conn)
    store.upsert_videos(conn, [{"bvid": "BV1", "cid": 999, "zone": "梗搜索"}])
    store.upsert_videos(conn, [{"bvid": "BV1", "cid": None, "zone": "梗搜索"}])
    row = conn.execute("SELECT cid FROM video WHERE bvid='BV1'").fetchone()
    assert row["cid"] == 999
    conn.close()


def _run_collect(conn, cycle_id, client, video, comments: bool = False):
    return pipeline.collect_docs(
        client, conn, cycle_id, [],
        [video] if comments else [],
        [] if comments else [video],
    )


def test_cid_reuse_skips_pagelist_after_backfill(tmp_path):
    conn = store.connect(tmp_path / "cidrun.db")
    store.init_db(conn)
    store.upsert_videos(conn, [{"bvid": "BV1", "cid": None, "zone": "梗搜索"}])
    client = FakeClient(cid=222)

    first = {"bvid": "BV1", "zone": "梗搜索", "cid": None}
    docs, stats = _run_collect(conn, 1, client, first)
    assert client.cid_calls == ["BV1"]
    assert stats["pagelist_requests"] == 1
    assert store.cid_map(conn, ["BV1"]) == {"BV1": 222}

    # 模拟下一轮：搜索接口仍返回 cid=None，但 DB 已有 cid
    store.upsert_videos(conn, [{"bvid": "BV1", "cid": None, "zone": "梗搜索"}])
    second = {"bvid": "BV1", "zone": "梗搜索", "cid": None}
    _, stats2 = _run_collect(conn, 2, client, second)
    assert client.cid_calls == ["BV1"]          # warm 轮不再补 cid
    assert stats2["pagelist_requests"] == 0
    conn.close()


def test_update_video_cid_is_idempotent(tmp_path):
    conn = store.connect(tmp_path / "cidupd.db")
    store.init_db(conn)
    store.upsert_videos(conn, [{"bvid": "BV2", "cid": None}])
    store.update_video_cid(conn, "BV2", 321)
    store.update_video_cid(conn, "BV2", 321)
    assert store.cid_map(conn, ["BV2"]) == {"BV2": 321}
    conn.close()


# ---------------- 评论缓存 ----------------

def test_comment_cache_hit_skips_requests(tmp_path):
    conn = store.connect(tmp_path / "comment.db")
    store.init_db(conn)
    client = FakeClient()
    video = {"bvid": "BV1", "aid": 1, "zone": "鬼畜"}

    _, first = _run_collect(conn, 1, client, video, comments=True)
    assert first["comment_requests"] == 1
    assert first["comment_cached"] == 0
    assert first["comments"] == 1
    assert client.comment_calls == [(1, 1)]

    _, second = _run_collect(conn, 2, client, video, comments=True)
    assert second["comment_requests"] == 0    # 命中缓存不请求
    assert second["comment_cached"] == 1      # stats 新增 comment_cached
    assert second["comments"] == 1            # 统计口径不变
    assert client.comment_calls == [(1, 1)]
    assert conn.execute("SELECT COUNT(*) FROM comment_cache").fetchone()[0] == 1
    conn.close()


def test_comment_cache_ttl_expired_refetches(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "commentttl.db")
    store.init_db(conn)
    client = FakeClient()
    video = {"bvid": "BV1", "aid": 1, "zone": "鬼畜"}
    _run_collect(conn, 1, client, video, comments=True)
    conn.execute("UPDATE comment_cache SET fetched_at='2020-01-01T00:00:00+08:00'")
    conn.commit()
    _, stats = _run_collect(conn, 2, client, video, comments=True)
    assert stats["comment_requests"] == 1
    assert stats["comment_cached"] == 0
    conn.close()


def test_comment_cache_disabled_refetches(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "commentoff.db")
    store.init_db(conn)
    client = FakeClient()
    video = {"bvid": "BV1", "aid": 1, "zone": "鬼畜"}
    monkeypatch.setattr(config, "COMMENT_CACHE_SECONDS", 0)
    _run_collect(conn, 1, client, video, comments=True)
    _, stats = _run_collect(conn, 2, client, video, comments=True)
    assert stats["comment_requests"] == 1
    assert stats["comment_cached"] == 0
    assert conn.execute("SELECT COUNT(*) FROM comment_cache").fetchone()[0] == 0
    conn.close()


# ---------------- 中断收尾与启动清理 ----------------

def test_run_cycle_keyboard_interrupt_marks_failed(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "ki.db")
    store.init_db(conn)

    def boom(client):
        raise KeyboardInterrupt("ctrl-c")

    monkeypatch.setattr(pipeline, "fetch_videos", boom)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run_cycle(conn=conn, client=BiliClient(offline=True))

    statuses = [row["status"] for row in conn.execute("SELECT status FROM cycle")]
    assert statuses == ["failed"]                      # 不残留 running
    assert conn.execute("SELECT COUNT(*) FROM source_error").fetchone()[0] == 1
    conn.close()


def test_abort_running_cycles_marks_aborted(tmp_path):
    conn = store.connect(tmp_path / "abort.db")
    store.init_db(conn)
    cycle_id = store.start_cycle(conn)
    assert store.abort_running_cycles(conn) == 1
    assert store.abort_running_cycles(conn) == 0        # 幂等
    row = conn.execute("SELECT status, finished_at FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    assert row["status"] == "aborted" and row["finished_at"]
    error = conn.execute("SELECT source FROM source_error WHERE cycle_id=?", (cycle_id,)).fetchone()
    assert error["source"] == "lifecycle"
    conn.close()


# ---------------- API：单飞、调度器互斥、health、启动清理 ----------------

def _fake_run_cycle(counter: dict):
    def run():
        counter["runs"] += 1
        time.sleep(0.05)
        return {"status": "done", "top": [], "cycle": None, "health": {}}
    return run


def test_refresh_single_flight_second_call_409(monkeypatch):
    from app import api as api_module

    counter = {"runs": 0}
    monkeypatch.setattr(pipeline, "run_cycle", _fake_run_cycle(counter))
    monkeypatch.setattr(api_module.config, "OFFLINE", False)
    monkeypatch.setattr(api_module.config, "REFRESH_TOKEN", "")

    async def main():
        api_module.RUN_LOCK = asyncio.Lock()
        api_module.STATE.update(running=False, snapshot=None, error="")
        api_module._COLLECT_TASK = None
        request = types.SimpleNamespace(headers={}, query_params={})
        assert await api_module.api_refresh(request) == {"started": True}
        with pytest.raises(HTTPException) as exc:
            await api_module.api_refresh(request)
        assert exc.value.status_code == 409
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert counter["runs"] == 1


def test_scheduler_and_manual_refresh_mutually_exclusive(monkeypatch):
    from app import api as api_module

    counter = {"runs": 0}
    monkeypatch.setattr(pipeline, "run_cycle", _fake_run_cycle(counter))
    monkeypatch.setattr(api_module.config, "OFFLINE", False)
    monkeypatch.setattr(api_module.config, "REFRESH_TOKEN", "")

    async def main():
        api_module.RUN_LOCK = asyncio.Lock()
        api_module.STATE.update(running=False, snapshot=None, error="")
        api_module._COLLECT_TASK = None
        assert api_module._start_collection("scheduler") is True
        request = types.SimpleNamespace(headers={}, query_params={})
        with pytest.raises(HTTPException) as exc:
            await api_module.api_refresh(request)
        assert exc.value.status_code == 409
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert counter["runs"] == 1


def test_health_scheduler_false_when_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "health.db")
    from app import api as api_module

    api_module = importlib.reload(api_module)
    with TestClient(api_module.app) as client:
        body = client.get("/api/health").json()
        assert body["offline"] is True
        assert body["scheduler"] is False       # 不再谎报 true


def test_lifespan_aborts_stale_running_cycle(tmp_path, monkeypatch):
    db = tmp_path / "stale.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = store.connect(db)
    store.init_db(conn)
    cycle_id = store.start_cycle(conn)
    conn.close()

    from app import api as api_module

    api_module = importlib.reload(api_module)
    with TestClient(api_module.app) as client:
        assert client.get("/api/health").status_code == 200

    conn = store.connect(db)
    row = conn.execute("SELECT status FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    assert row["status"] == "aborted"
    errors = conn.execute("SELECT error FROM source_error WHERE cycle_id=?", (cycle_id,)).fetchall()
    assert errors and "aborted" in errors[0]["error"]
    conn.close()
