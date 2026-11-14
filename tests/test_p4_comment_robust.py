"""P4：45 评论视频 + 评论页非 JSON/412 退避重试、失败统计与只缓存有效页。"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app import bili, config, pipeline, store
from app.bili import BiliClient
from app.http import HttpError

TZ = timezone(timedelta(hours=8))
FROZEN = datetime(2026, 11, 14, 12, 0, tzinfo=TZ)
NOW_TS = FROZEN.timestamp()
# 离线 reply fixture 的 ctime 落在 2025-04，离线端到端测试冻结在同一天。
FIXTURE_FROZEN = datetime(2025, 4, 10, 12, 0, tzinfo=TZ)


def _ok_payload(rpid=1, text="哈基米", cursor=0, ctime=None):
    return {
        "code": 0,
        "data": {
            "replies": [{
                "rpid": rpid, "like": 3,
                "ctime": int(ctime if ctime is not None else NOW_TS - 86400),
                "content": {"message": text},
            }],
            "cursor": {"next": cursor, "is_end": cursor == 0},
        },
    }


def _json(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()


class ScriptHttp:
    """按顺序回放原始响应（bytes）或抛 HttpError；用于分类/重试测试。"""

    def __init__(self, script):
        self.script = list(script)
        self.urls: list[str] = []
        self.calls: Counter = Counter()
        self.requests = 0
        self.bootstrap_calls = 0

    def get(self, url, referer="", headers=None, retries=2):
        self.urls.append(url)
        self.calls["comments" if "/x/v2/reply" in url else "other"] += 1
        self.requests += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get_json(self, url, referer=""):
        if "finger/spi" in url:
            self.bootstrap_calls += 1
            return {"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}
        return {"code": 0, "data": {}}

    def set_cookie(self, name, value, domain=".bilibili.com"):
        pass

    def request_count(self):
        return self.requests


def _client(script, bootstrapped=True):
    client = BiliClient(http=ScriptHttp(script), offline=False)
    client._bootstrapped = bootstrapped
    return client


def test_html_page_retries_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(bili.time, "sleep", lambda seconds: sleeps.append(seconds))
    fake = ScriptHttp([b"<html><body>412 risk</body></html>", _json(_ok_payload())])
    client = BiliClient(http=fake, offline=False)
    client._bootstrapped = True

    items, cursor, ok = client.comment_page_ex(123, mode=3)
    assert ok is True and cursor == 0 and [item["rpid"] for item in items] == [1]
    assert client.comment_page_retry == 1 and client.comment_page_fail == 0
    assert sleeps == [0.5]
    assert len(fake.urls) == 2


@pytest.mark.parametrize(("body", "kind"), [
    (b"", "empty"),
    (b"<html><title>412</title></html>", "html"),
    (b"not-json-at-all", "non_json"),
])
def test_failure_kinds_are_distinguished_and_backoff_exponential(monkeypatch, body, kind):
    sleeps: list[float] = []
    monkeypatch.setattr(bili.time, "sleep", lambda seconds: sleeps.append(seconds))
    fake = ScriptHttp([body] * config.COMMENT_MAX_ATTEMPTS)
    client = BiliClient(http=fake, offline=False)
    client._bootstrapped = True

    items, cursor, ok = client.comment_page_ex(7, mode=2)
    assert (items, cursor, ok) == ([], 0, False)
    assert client.comment_page_fail == 1
    assert client.comment_page_retry == config.COMMENT_MAX_ATTEMPTS - 1 == 3
    assert dict(client.comment_page_fail_kinds) == {kind: 1}
    # 4 次尝试、3 次指数退避 0.5/1/2s；每次失败都发请求
    assert sleeps == [0.5, 1.0, 2.0]
    assert len(fake.urls) == 4
    assert client.errors and f"{kind} (attempts=4)" in client.errors[-1]["error"]


def test_412_backoff_rebootstraps_once_before_retrying(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(bili.time, "sleep", lambda seconds: sleeps.append(seconds))
    fake = ScriptHttp([HttpError("HTTP 412 risk", 412)] * config.COMMENT_MAX_ATTEMPTS)
    client = BiliClient(http=fake, offline=False)
    client._bootstrapped = True

    _, _, ok = client.comment_page_ex(99, mode=3)
    assert ok is False
    assert sleeps == [0.5, 1.0, 2.0]
    assert client.comment_page_retry == 3 and client.comment_page_fail == 1
    assert dict(client.comment_page_fail_kinds) == {"http_412": 1}
    # 每页最多强制重取一次 buvid（4 次尝试只 bootstrap 一次），避免放大请求。
    assert fake.bootstrap_calls == 1


def test_code_minus_352_rebootstraps_and_retries(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(bili.time, "sleep", lambda seconds: sleeps.append(seconds))
    fake = ScriptHttp([_json({"code": -352, "message": "risk"}),
                       _json(_ok_payload())])
    client = BiliClient(http=fake, offline=False)
    client._bootstrapped = True

    items, _, ok = client.comment_page_ex(5, mode=3)
    assert ok is True and [item["rpid"] for item in items] == [1]
    assert client.comment_page_retry == 1 and client.comment_page_fail == 0
    assert fake.bootstrap_calls == 1
    assert sleeps == [0.5]


def test_5xx_retries_but_business_errors_do_not(monkeypatch):
    monkeypatch.setattr(bili.time, "sleep", lambda seconds: None)
    fake = ScriptHttp([HttpError("HTTP 502", 502), _json(_ok_payload())])
    client = BiliClient(http=fake, offline=False)
    client._bootstrapped = True
    assert client.comment_page_ex(1, mode=3)[2] is True
    assert client.comment_page_retry == 1

    # -500：业务错误不重试，只发一次
    fake = ScriptHttp([_json({"code": -500, "message": "boom"})])
    client = BiliClient(http=fake, offline=False)
    client._bootstrapped = True
    assert client.comment_page_ex(1, mode=3) == ([], 0, False)
    assert client.comment_page_retry == 0 and len(fake.urls) == 1
    assert "code=-500" in client.errors[-1]["error"]

    # -404（无评论/不可见）：有效空页，不重试、不算失败页，可缓存
    fake = ScriptHttp([_json({"code": -404, "message": "啥都木有"})])
    client = BiliClient(http=fake, offline=False)
    client._bootstrapped = True
    assert client.comment_page_ex(1, mode=3) == ([], 0, True)
    assert client.comment_page_retry == 0 and client.comment_page_fail == 0
    assert client.errors == []


# ---------------- pipeline：失败统计 / 只缓存有效页 / 降级阈值 ----------------

class StubPages:
    """按顺序返回 ok/失败页；实现 comment_page_ex 供 collect_docs 使用。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.comment_page_retry = 0
        self.comment_page_fail_kinds: Counter = Counter()

    def comment_page_ex(self, aid, page=1, mode=3, cursor=None, ps=20):
        outcome = self.outcomes.pop(0)
        ok, next_cursor = outcome if isinstance(outcome, tuple) else (outcome, 0)
        if not ok:
            self.comment_page_retry += 1
            self.comment_page_fail_kinds["html"] += 1
            return [], 0, False
        return [{"text": f"评论{aid}-{mode}-{page}", "like": 1,
                 "ctime": int(NOW_TS - 86400), "rpid": hash((aid, mode, page)) & 0xFFFF}], next_cursor, True


def _videos(count=5):
    return [{"bvid": f"BV{index}", "aid": index + 1, "zone": "鬼畜"}
            for index in range(count)]


def _run_collect(monkeypatch, tmp_path, outcomes, videos=None):
    monkeypatch.setattr(pipeline, "_now", lambda: FROZEN)
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = store.connect(tmp_path / "collect.db")
    store.init_db(conn)
    targets = videos if videos is not None else _videos(len(outcomes) // 2)
    client = StubPages(outcomes)
    docs, stats = pipeline.collect_docs(client, conn, 1, [], targets, [])
    return conn, docs, stats


def test_only_valid_pages_cached_and_failure_stats(monkeypatch, tmp_path):
    # 2 视频 × (mode3 + mode2)：首页 mode3 失败、其余成功
    conn, docs, stats = _run_collect(
        monkeypatch, tmp_path, [False, True, True, True], videos=_videos(2),
    )
    assert stats["comment_requests"] == 4
    assert stats["comment_page_fail"] == 1
    assert stats["comment_page_retry"] == 1
    assert stats["comment_page_fail_kinds"] == {"html": 1}
    assert stats["comment_page_fail_rate"] == 0.25
    assert stats["comment_degraded"] is True
    cached = conn.execute(
        "SELECT aid, mode, page FROM comment_cache ORDER BY aid, mode"
    ).fetchall()
    # 失败页 (aid=1, mode=3, page=1) 不写缓存；其余 3 页命中
    assert [(row["aid"], row["mode"], row["page"]) for row in cached] == [
        (1, 2, 1), (2, 2, 1), (2, 3, 1),
    ]
    assert len([doc for doc in docs if doc.kind == "comment"]) == 3
    conn.close()


def test_failure_rate_threshold_is_strictly_above_20_percent(monkeypatch, tmp_path):
    # 5 视频 × 2 页 = 10 页；失败 2 页 = 20% → 不降级
    conn, _, stats = _run_collect(
        monkeypatch, tmp_path, [False, True, False, True, True, True, True, True, True, True],
        videos=_videos(5),
    )
    assert stats["comment_page_fail_rate"] == 0.2
    assert stats["comment_degraded"] is False
    conn.close()

    # 失败 3 页 = 30% → 降级
    conn, _, stats = _run_collect(
        monkeypatch, tmp_path / "second",
        [False, True, False, True, False, True, True, True, True, True],
        videos=_videos(5),
    )
    assert stats["comment_page_fail_rate"] == 0.3
    assert stats["comment_degraded"] is True
    conn.close()


def test_warm_round_skips_cached_pages_but_retries_failed_ones(monkeypatch, tmp_path):
    # 首次：1 视频，mode3 成功 + mode2 三页成功（cursor 20/40/0）
    monkeypatch.setattr(pipeline, "_now", lambda: FROZEN)
    conn = store.connect(tmp_path / "warm.db")
    store.init_db(conn)
    client = StubPages([(True, 0), (True, 20), (True, 40), (True, 0)])
    targets = _videos(1)
    _, first = pipeline.collect_docs(client, conn, 1, [], targets, [])
    assert first["comment_requests"] == 4 and first["comment_cached"] == 0
    assert conn.execute("SELECT COUNT(*) FROM comment_cache").fetchone()[0] == 4

    # warm：失败页才有请求。这里全部有效 → 0 请求、4 页缓存命中
    _, second = pipeline.collect_docs(client, conn, 2, [], targets, [])
    assert second["comment_requests"] == 0
    assert second["comment_cached"] == 4
    assert second["comments"] == first["comments"]
    conn.close()


# ---------------- 45 目标选择 ----------------

def test_select_45_comment_videos_and_10_danmaku():
    videos = []
    for zone in range(8):
        for index in range(10):
            videos.append({"bvid": f"BZ{zone}{index}", "aid": zone * 100 + index,
                           "zone": f"Z{zone}", "reply": 100 - index,
                           "danmaku": 1000 - index, "view": 1000, "source": "zone"})
    for index in range(20):
        videos.append({"bvid": f"BS{index}", "aid": 900 + index, "zone": "梗搜索",
                       "reply": 0, "danmaku": 0, "view": 5000, "source": "search"})

    comment_targets, danmaku_targets = pipeline.select_targets(videos)
    assert config.COMMENT_VIDEOS == 45 and config.DANMAKU_VIDEOS == 10
    assert len(comment_targets) == 45
    assert len(danmaku_targets) == 10
    assert len({video["bvid"] for video in comment_targets}) == 45
    assert len({video["bvid"] for video in danmaku_targets}) == 10


# ---------------- 端到端：单页失败不降级 / 失败率高仍不空榜 ----------------

class FlakyOfflineClient(BiliClient):
    """离线客户端：按需让评论页失败（模拟 412/非 JSON），用于 cycle 状态回归。"""

    def __init__(self, fail_all=False, fail_once=False):
        super().__init__(offline=True)
        self.fail_all = fail_all
        self.fail_once = fail_once
        self.failed = False

    def comment_page_ex(self, aid, page=1, mode=3, cursor=None, ps=20):
        should_fail = self.fail_all or (self.fail_once and not self.failed)
        if should_fail:
            self.failed = True
            self.comment_page_fail += 1
            self.comment_page_retry += 1
            self.comment_page_fail_kinds["http_412"] += 1
            self.errors.append({"source": f"reply:{aid}:{mode}",
                                "error": "http_412 (attempts=4)"})
            return [], 0, False
        return super().comment_page_ex(aid, page=page, mode=mode, cursor=cursor, ps=ps)


def test_single_comment_page_failure_does_not_degrade_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_now", lambda: FIXTURE_FROZEN)
    conn = store.connect(tmp_path / "single.db")
    store.init_db(conn)
    snapshot = pipeline.run_cycle(conn=conn, client=FlakyOfflineClient(fail_once=True))
    stats = snapshot["cycle"]["stats"]
    assert snapshot["cycle"]["status"] == "done"
    assert stats["comment_page_fail"] == 1
    assert stats["comment_page_fail_rate"] < config.COMMENT_FAIL_RATE_MAX
    assert stats["comment_page_retry"] == 1
    # 单页失败仍产出榜单与评论
    assert snapshot["top"]
    assert stats["comments"] > 0
    conn.close()


def test_comment_collection_total_failure_still_produces_board(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_now", lambda: FIXTURE_FROZEN)
    conn = store.connect(tmp_path / "allfail.db")
    store.init_db(conn)
    snapshot = pipeline.run_cycle(conn=conn, client=FlakyOfflineClient(fail_all=True))
    stats = snapshot["cycle"]["stats"]
    assert snapshot["cycle"]["status"] == "degraded"
    assert stats["comment_page_fail"] == stats["comment_requests"] > 0
    assert stats["comment_page_fail_rate"] == 1.0
    assert stats["comment_degraded"] is True
    assert stats["comments"] == 0 and stats["comment_evidence_ready"] is False
    # 评论全失败仍用标题/弹幕/种子产出，不空榜
    assert snapshot["top"]
    assert stats["families"] > 0
    conn.close()
