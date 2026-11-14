"""P0 评论区优先回归：reply/main 解析、ctime 落库、窗口过滤、缓存 mode、7 天增速、弹幕降配。"""
import http.cookiejar as cookiejar
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import config, http, pipeline, store
from app.bili import BiliClient, parse_comments, parse_comment_page

FIXTURES = Path(__file__).parent / "fixtures"
TZ = timezone(timedelta(hours=8))
FROZEN = datetime(2025, 4, 10, 12, 0, tzinfo=TZ)
NOW_TS = FROZEN.timestamp()


# ---------------- reply/main 解析 ----------------

def _reply(rpid, text, like=1, ctime=1744000000):
    return {"rpid": rpid, "like": like, "ctime": ctime,
            "content": {"message": text}}


def test_parse_comments_merges_top_replies_and_dedupes_rpid():
    payload = {
        "code": 0,
        "data": {
            "top_replies": [_reply(1, "置顶评论", like=999)],
            "replies": [_reply(1, "置顶评论", like=999), _reply(2, "普通评论", like=3)],
            "cursor": {"next": 2},
        },
    }
    comments = parse_comments(payload)
    assert [c["rpid"] for c in comments] == [1, 2]
    assert comments[0]["like"] == 999
    assert comments[1]["ctime"] == 1744000000

    page, cursor = parse_comment_page(payload)
    assert len(page) == 2 and cursor == 2


def test_parse_comments_accepts_legacy_reply_fixture():
    """旧 `/x/v2/reply` fixture（没有 top_replies/cursor）仍可解析。"""
    payload = json.loads((FIXTURES / "reply.json").read_text(encoding="utf-8"))
    comments = parse_comments(payload)
    assert comments
    for item in comments:
        assert item["text"] and item["ctime"] > 0 and "rpid" in item


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.urls: list[str] = []
        self.calls: Counter = Counter()
        self.requests = 0

    def get_json(self, url: str, referer: str = "") -> dict:
        self.urls.append(url)
        self.calls[http._endpoint_label(url)] += 1
        self.requests += 1
        return self.payload

    def request_count(self) -> int:
        return self.requests


def test_comments_uses_reply_main_with_mode_and_cursor():
    payload = {"code": 0, "data": {"replies": [_reply(7, "评论", like=2)]}}
    fake = FakeHttp(payload)
    client = BiliClient(http=fake, offline=False)
    comments = client.comments(123, mode=2, page=2)
    assert [c["rpid"] for c in comments] == [7]
    url = fake.urls[-1]
    assert "/x/v2/reply/main" in url
    assert "oid=123" in url and "mode=2" in url and "next=20" in url and "ps=20" in url
    assert fake.calls["comments"] == 1
    # 显式 cursor 优先于 page 推导
    client.comments(123, mode=3, cursor=40)
    assert "next=40" in fake.urls[-1]


def test_comment_request_drops_buvid3_only(tmp_path):
    """实测带 buvid3 时 reply/main 只回 3 条：评论请求期间摘掉、之后放回。"""

    class JarHttp(FakeHttp):
        def __init__(self, payload):
            super().__init__(payload)
            self.jar = cookiejar.CookieJar()
            self.saw_buvid3: list[bool] = []

        def get_json(self, url: str, referer: str = "") -> dict:
            self.saw_buvid3.append(any(c.name == "buvid3" for c in self.jar))
            return super().get_json(url, referer=referer)

    fake = JarHttp({"code": 0, "data": {"replies": [_reply(7, "评论")]}})
    fake.jar.set_cookie(cookiejar.Cookie(
        0, "buvid3", "abc", None, False, ".bilibili.com", True, False, "/",
        True, False, None, True, None, None, {}))
    fake.jar.set_cookie(cookiejar.Cookie(
        0, "buvid4", "xyz", None, False, ".bilibili.com", True, False, "/",
        True, False, None, True, None, None, {}))
    client = BiliClient(http=fake, offline=False)
    assert [c["rpid"] for c in client.comments(123, mode=3)] == [7]
    # 请求期间没有 buvid3（buvid4 保留），请求结束后 buvid3 复原
    assert fake.saw_buvid3[-1] is False
    assert {c.name for c in fake.jar} == {"buvid3", "buvid4"}


def test_comments_records_error_and_returns_empty(monkeypatch):
    fake = FakeHttp({"code": -500, "message": "boom"})
    client = BiliClient(http=fake, offline=False)
    assert client.comments(1, mode=3) == []
    assert client.errors and "code=-500" in client.errors[-1]["error"]

    # -404（视频不可见/无评论）属于业务空，不算采集故障
    fake = FakeHttp({"code": -404, "message": "啥都木有"})
    client = BiliClient(http=fake, offline=False)
    assert client.comments(2, mode=3) == []
    assert client.errors == []


# ---------------- ctime 落库 ----------------

def test_text_sample_raw_time_stores_ctime(tmp_path):
    conn = store.connect(tmp_path / "raw.db")
    store.init_db(conn)
    store.insert_text_samples(
        conn, 1, "BV1", "comment",
        [{"text": "有 ctime 的评论", "like": 3, "ctime": 1744000000}],
    )
    store.insert_text_samples(
        conn, 1, "BV1", "danmaku",
        [{"text": "弹幕回退 at", "like": 0, "at": 12.5}],
    )
    rows = {r["text"]: r["raw_time"] for r in
            conn.execute("SELECT text, raw_time FROM text_sample")}
    assert rows["有 ctime 的评论"] == 1744000000
    assert rows["弹幕回退 at"] == 12.5
    conn.close()


# ---------------- 缓存：mode 隔离 + 旧库迁移 ----------------

LEGACY_CACHE_DDL = """
CREATE TABLE comment_cache(
    aid INTEGER NOT NULL,
    page INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(aid, page)
);
"""


def test_comment_cache_mode_isolation_and_legacy_migration(tmp_path):
    conn = store.connect(tmp_path / "cache.db")
    conn.executescript(LEGACY_CACHE_DDL)
    conn.execute(
        "INSERT INTO comment_cache(aid, page, fetched_at, payload) VALUES(?,?,?,?)",
        (5, 1, store.now_iso(),
         json.dumps([{"text": "旧缓存热评", "like": 9, "ctime": 1744000000}],
                    ensure_ascii=False)),
    )
    conn.commit()
    store.init_db(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(comment_cache)")}
    assert "mode" in columns
    migrated = store.get_comment_cache(conn, 5, 3, 1)
    assert migrated and migrated[0]["text"] == "旧缓存热评"
    assert migrated[0]["ctime"] == 1744000000      # 缓存也要带 ctime

    store.put_comment_cache(conn, 5, 2, 1, [{"text": "新缓存时间序", "like": 0,
                                             "ctime": 1744000001}])
    assert store.get_comment_cache(conn, 5, 2, 1)[0]["text"] == "新缓存时间序"
    assert store.get_comment_cache(conn, 5, 3, 1)[0]["text"] == "旧缓存热评"
    assert conn.execute("SELECT COUNT(*) FROM comment_cache").fetchone()[0] == 2

    store.init_db(conn)                            # 幂等：二次迁移不重复、不丢行
    assert conn.execute("SELECT COUNT(*) FROM comment_cache").fetchone()[0] == 2
    conn.close()


# ---------------- 窗口过滤 / 去重 / 每视频 ≤40 ----------------

class FakeClient:
    def __init__(self, by_mode: dict[int, list[dict]]):
        self.by_mode = by_mode
        self.calls: list[tuple[int, int]] = []

    def comment_page(self, aid, page=1, mode=3, cursor=None, ps=20):
        self.calls.append((mode, page))
        return [dict(item) for item in self.by_mode.get(mode, [])], 0

    def comments(self, aid, page=1, mode=3, cursor=None, ps=20):
        return self.comment_page(aid, page=page, mode=mode, cursor=cursor, ps=ps)[0]

    def cid_for(self, bvid):
        return None

    def danmaku(self, cid):
        return ""


def _comment(rpid, text, like, ctime):
    return {"rpid": rpid, "text": text, "like": like, "ctime": int(ctime)}


def test_comment_window_dedupe_and_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_now", lambda: FROZEN)
    hot = [_comment(i, f"哈基米热评{i}", 100 - i, NOW_TS - 2 * 86400)
           for i in range(1, 26)]
    hot.append(_comment(90, "哈基米老评论", 500, NOW_TS - 40 * 86400))
    fresh = [_comment(i, f"哈基米热评{i}", 100 - i, NOW_TS - 2 * 86400)
             for i in range(1, 11)]                      # 与热度评论 rpid 重复
    fresh += [_comment(100 + i, f"哈基米新评{i}", 0, NOW_TS - 86400)
              for i in range(1, 21)]
    fresh.append(_comment(200, "哈基米热评1", 7, NOW_TS - 86400))  # 文本重复

    conn = store.connect(tmp_path / "window.db")
    store.init_db(conn)
    docs, stats = pipeline.collect_docs(
        FakeClient({3: hot, 2: fresh}), conn, 1, [],
        [{"bvid": "BV1", "aid": 42, "zone": "鬼畜"}], [],
    )
    comments = [d for d in docs if d.kind == "comment"]
    assert len(comments) == config.COMMENT_MAX_PER_VIDEO == 40
    assert stats["comments"] == 40
    assert stats["comment_filtered_old"] >= 1
    assert stats["comment_dupes"] >= 10
    assert stats["comment_modes"]["3"] > 0 and stats["comment_modes"]["2"] > 0
    assert all(0 < getattr(d, "ctime", 0) <= NOW_TS for d in comments)
    assert all(NOW_TS - d.ctime <= config.COMMENT_WINDOW_DAYS * 86400 for d in comments)
    raw_times = [r["raw_time"] for r in
                 conn.execute("SELECT raw_time FROM text_sample WHERE kind='comment'")]
    assert len(raw_times) == 40 and all(t > 0 for t in raw_times)
    conn.close()


# ---------------- 7 天增速 / 年龄归一 / 弹幕刷屏 ----------------

def test_comment_evidence_growth_weight_and_danmaku_only():
    bucket = {
        "comment_likes": [
            (NOW_TS - 1 * 86400, 9),
            (NOW_TS - 3 * 86400, 4),
            (NOW_TS - 15 * 86400, 100),
            (NOW_TS - 25 * 86400, 100),
        ],
        "comment_videos": {"BV1", "BV2"},
        "danmaku_docs": 12,
    }
    evidence = pipeline.comment_evidence(bucket, NOW_TS)
    assert evidence["comment_count_recent"] == 4
    assert evidence["comment_7d_count"] == 2
    assert evidence["comment_mid_count"] == 2
    assert evidence["comment_growth_7d"] == pytest.approx(1.0)   # (2+1)/(2+1)
    assert evidence["comment_df_video"] == 2
    assert evidence["age_days"] == pytest.approx(25.0)
    assert evidence["earliest_comment_at"].startswith("2025-03-16")
    # 年龄衰减：老评论 100 赞贡献远小于新评论 9 赞的 log1p
    weighted = evidence["comment_like_weighted"]
    assert weighted > 0
    assert weighted < sum(__import__("math").log1p([9, 4, 100, 100][i])
                          for i in range(4))
    assert evidence["danmaku_only"] is False

    # 弹幕刷屏但近 7 天无评论、视频覆盖低 → danmaku_only
    stale = pipeline.comment_evidence(
        {"comment_likes": [(NOW_TS - 20 * 86400, 50)],
         "comment_videos": {"BV9"}, "danmaku_docs": 30}, NOW_TS)
    assert stale["comment_7d_count"] == 0
    assert stale["danmaku_only"] is True
    assert pipeline.comment_evidence(None, NOW_TS)["comment_count_recent"] == 0


# ---------------- P0 门控 ----------------

def test_comment_gate_rejects_non_seed_without_recent_evidence():
    assert pipeline.comment_gate({"seed_hit": False}, {"comment_count_recent": 0},
                                 True) == "no_recent_comment"
    assert pipeline.comment_gate({"seed_hit": True}, {"comment_count_recent": 0}, True) == ""
    assert pipeline.comment_gate({"seed_hit": False}, {"comment_count_recent": 3}, True) == ""
    # 评论采集不健康时不惩罚候选池
    assert pipeline.comment_gate({"seed_hit": False}, {"comment_count_recent": 0},
                                 False) == ""


# ---------------- 采集配额 ----------------

def test_p0_quotas_comment_first():
    assert config.COMMENT_MODES == (3, 2)
    # P4：mode2 翻 3 页（next=0/20/40），mode3 1 页；评论 45 视频、弹幕 10 视频
    assert config.COMMENT_PAGES == {3: 1, 2: 3}
    assert config.COMMENT_WINDOW_DAYS == 30
    assert config.COMMENT_MAX_PER_VIDEO == 40
    assert config.DANMAKU_VIDEOS == 10
    assert config.MAX_DANMAKU_PER_VIDEO == 200
    assert config.COMMENT_VIDEOS == 45

    videos = []
    for zone in range(8):
        for index in range(10):
            videos.append({"bvid": f"BV{zone}{index}", "aid": 1, "zone": f"Z{zone}",
                           "reply": 100 - index, "danmaku": 1000 - index,
                           "view": 1000, "source": "zone"})
    comment_targets, danmaku_targets = pipeline.select_targets(videos)
    assert len(comment_targets) <= config.COMMENT_VIDEOS
    assert len(danmaku_targets) <= config.DANMAKU_VIDEOS
    assert len(danmaku_targets) < len(comment_targets)


# ---------------- 离线端到端（冻结时间命中 fixture 评论） ----------------

def test_offline_cycle_with_frozen_clock_has_comment_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_now", lambda: FROZEN)
    conn = store.connect(tmp_path / "p0.db")
    store.init_db(conn)
    snapshot = pipeline.run_cycle(conn=conn, client=BiliClient(offline=True))
    stats = snapshot["cycle"]["stats"]

    assert stats["comments_recent"] >= config.COMMENT_GATE_MIN_DOCS
    assert stats["comments_7d"] > 0
    assert stats["comment_modes"]["3"] > 0 and stats["comment_modes"]["2"] > 0
    assert stats["danmaku_docs"] <= config.DANMAKU_VIDEOS * config.MAX_DANMAKU_PER_VIDEO
    assert "comment_cached" in stats and "endpoint_counts" in stats
    assert stats["comment_evidence_ready"] is True

    assert conn.execute(
        "SELECT COUNT(*) FROM text_sample WHERE kind='comment' AND raw_time>0"
    ).fetchone()[0] == stats["comments"]
    top = snapshot["top"]
    assert top, "离线 + 近期评论应仍能产出 Top10"
    for item in top:
        assert "comment_count_recent" in item["components"]["raw"]
        assert "comment_7d_count" in item["components"]["raw"]
        assert "earliest_comment_at" in item["components"]["raw"]
    conn.close()


def test_offline_cycle_without_recent_comments_skips_gate(tmp_path):
    """真实时钟下旧 fixture 评论全在 30 天外 → 证据不健康 → 不启用门控。"""
    conn = store.connect(tmp_path / "stale.db")
    store.init_db(conn)
    snapshot = pipeline.run_cycle(conn=conn, client=BiliClient(offline=True))
    stats = snapshot["cycle"]["stats"]
    assert stats["comments_recent"] == 0
    assert stats["comment_evidence_ready"] is False
    assert stats["comment_gate_rejected"] == 0
    assert snapshot["top"], "评论证据不健康时不应把候选池清零"
    conn.close()
