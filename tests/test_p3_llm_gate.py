"""P3 回归：LLM 审核接进采集流水线，list=new 发布门、审计与降级。"""
import importlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import config, pipeline, review, store
from app.bili import BiliClient
from app.extract import TextDoc


# ---------------------------------------------------------------------------
# 假审核器 / 合成结果
# ---------------------------------------------------------------------------

def _entry(candidate, verdict):
    return {
        "candidate_id": candidate["candidate_id"],
        "canonical": candidate["canonical"],
        "verdict": verdict,
        "meme_prob": 0.8 if verdict == "approve" else 0.1,
        "novelty": "recent" if verdict == "approve" else "unknown",
        "origin": "测试出处",
        "reason": f"fake {verdict}",
        "evidence_ids": ["ev:canonical"],
        "merge_into": "",
        "evidence_hash": "hash-" + candidate["canonical"],
        "model": "fake-gate",
        "prompt_version": "p2-review-v1",
    }


def make_result(candidates, decide):
    approved, rejected, merged, log = [], [], [], []
    for candidate in candidates:
        verdict = decide(candidate) if callable(decide) else decide
        entry = _entry(candidate, verdict)
        log.append(entry)
        if verdict == "approve":
            approved.append(entry)
        elif verdict == "merge":
            merged.append({**entry, "merge_into": "别的梗"})
        else:
            rejected.append(entry)
    return {
        "approved": approved,
        "rejected": rejected,
        "merged": merged,
        "rounds": 1,
        "calls": len(candidates),
        "reviewed": len(candidates),
        "log": log,
        "degraded_reason": "pool_exhausted",
        "fallback_used": True,
        "prompt_version": "p2-review-v1",
        "target": 10,
        "shortfall": max(0, 10 - len(approved)),
    }


class FakeGate:
    """可注入 run_cycle 的审核器：记录候选、按 decide 返回 P2 形状的结果。"""

    model = "fake-gate"

    def __init__(self, decide, error=None, delay=0.0):
        self.decide = decide
        self.error = error
        self.delay = delay
        self.calls = 0
        self.candidates = []

    def __call__(self, candidates, **kwargs):
        self.calls += 1
        self.candidates = [dict(item) for item in candidates]
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return make_result(candidates, self.decide)


class FakeLLM:
    """走真实 review_candidates 的最小 chat_json 客户端（不联网）。"""

    model = "fake-llm"

    def __init__(self, raw):
        self.raw = raw
        self.calls = 0
        self.messages = []

    def chat_json(self, messages, schema=None):
        self.calls += 1
        self.messages.append(messages)
        return dict(self.raw)


def _item(family_key, canonical, score, rank=0, list_type="new", status="observing"):
    return {
        "family_key": family_key,
        "canonical": canonical,
        "score": score,
        "rank": rank,
        "status": status,
        "list": list_type,
        "components": {
            "list": list_type,
            "comment": {
                "age_days": 2.0,
                "comment_7d_count": 3,
                "comment_df_video": 2,
                "comment_like_weighted": 4.2,
                "seed_origin_year": None,
            },
        },
        "raw_signals": {},
    }


def _run_offline(conn, reviewer=pipeline._DEFAULT_REVIEWER):
    return pipeline.run_cycle(
        conn=conn, client=BiliClient(offline=True), reviewer=reviewer
    )


def _llm_stats(snapshot):
    return {key: value for key, value in snapshot["cycle"]["stats"].items()
            if key.startswith("llm")}


# ---------------------------------------------------------------------------
# off / 默认 reviewer
# ---------------------------------------------------------------------------

def test_off_mode_makes_zero_calls_and_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "off")
    fake = FakeGate(lambda candidate: "reject")
    conn = store.connect(tmp_path / "off.db")
    store.init_db(conn)
    snapshot = _run_offline(conn, reviewer=fake)
    stats = snapshot["cycle"]["stats"]

    assert fake.calls == 0
    assert stats["llm_mode"] == "off"
    assert stats["llm_calls"] == 0
    assert stats["llm_fallback"] is False
    assert conn.execute("SELECT COUNT(*) FROM llm_review").fetchone()[0] == 0
    # 默认 all / new 仍是规则榜
    assert store.top_items(conn, snapshot["cycle"]["id"], 10)
    assert store.list_items(conn, snapshot["cycle"]["id"], "new", 10)
    conn.close()


def test_build_reviewer_returns_none_without_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "shadow")
    monkeypatch.delenv("HOTMEME_LLM_API_KEY", raising=False)
    assert pipeline.build_reviewer() is None


def test_shadow_mode_writes_audit_but_list_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "shadow")
    fake = FakeGate(lambda candidate: "reject")
    conn = store.connect(tmp_path / "shadow.db")
    store.init_db(conn)
    snapshot = _run_offline(conn, reviewer=fake)
    cycle_id = snapshot["cycle"]["id"]
    stats = snapshot["cycle"]["stats"]

    assert fake.calls == 1
    assert stats["llm_approved"] == 0 and stats["llm_rejected"] == 1
    assert stats["llm_gate_applied"] is True
    rows = conn.execute(
        "SELECT family_key, verdict, mode FROM llm_review WHERE cycle_id=?", (cycle_id,)
    ).fetchall()
    assert len(rows) == 1 and rows[0]["verdict"] == "reject"
    assert rows[0]["mode"] == "shadow"

    # shadow 不改榜：条目仍在 new/all，status/rank 不变
    new_items = store.list_items(conn, cycle_id, "new", 10)
    assert [item["canonical"] for item in new_items] == ["嘉豪"]
    assert new_items[0]["status"] == "observing"
    assert new_items[0]["llm_review"]["verdict"] == "reject"
    assert new_items[0]["llm_review"]["mode"] == "shadow"
    conn.close()


# ---------------------------------------------------------------------------
# demote / require_approve
# ---------------------------------------------------------------------------

def test_demote_drops_reject_from_new_but_all_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "demote")
    fake = FakeGate(lambda candidate: "reject")
    conn = store.connect(tmp_path / "demote.db")
    store.init_db(conn)
    snapshot = _run_offline(conn, reviewer=fake)
    cycle_id = snapshot["cycle"]["id"]

    assert store.list_items(conn, cycle_id, "new", 10) == []
    all_items = store.top_items(conn, cycle_id, 10)
    assert [item["canonical"] for item in all_items] == ["嘉豪"]
    assert all_items[0]["rank"] == 1
    assert all_items[0]["status"] == "observing"       # all/classic 规则状态不变
    assert all_items[0]["llm_review"]["verdict"] == "reject"
    conn.close()


def test_demote_rank_zero_item_status_becomes_rejected():
    items = [
        _item("甲", "甲", 90.0, rank=0),
        _item("乙", "乙", 80.0, rank=0),
    ]
    entries = {
        "甲": {"verdict": "reject", "reason": "r", "meme_prob": 0.1,
               "novelty": "unknown", "merge_into": ""},
        "乙": {"verdict": "approve", "reason": "r", "meme_prob": 0.9,
               "novelty": "recent", "merge_into": ""},
    }
    published, withheld = pipeline.apply_review_gate(items, entries, "demote")
    assert published == {"乙"} and withheld == {"甲"}
    assert items[0]["status"] == "rejected"            # rank=0 直接不落库
    assert items[1]["status"] == "observing"
    assert items[0]["new_list_status"] == "withheld"


def test_require_approve_keeps_only_approve_and_llm_review(tmp_path):
    conn = store.connect(tmp_path / "require.db")
    store.init_db(conn)
    cycle_id = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, cycle_id, "done", {"llm_mode": "require_approve"})
    mapping = store.upsert_memes(conn, [
        {"family_key": "甲", "canonical": "甲", "type": "词"},
        {"family_key": "乙", "canonical": "乙", "type": "词"},
    ])
    store.insert_signals(conn, cycle_id, [
        {"meme_id": mapping["甲"], "family_key": "甲", "score": 70.0, "rank": 1,
         "status": "observing", "components": {"list": "new"}, "sample": ""},
        {"meme_id": mapping["乙"], "family_key": "乙", "score": 60.0, "rank": 2,
         "status": "observing", "components": {"list": "new"}, "sample": ""},
    ])
    store.insert_llm_reviews(conn, cycle_id, [
        {"family_key": "甲", "verdict": "approve", "reason": "像梗",
         "mode": "require_approve", "model": "m"},
        {"family_key": "乙", "verdict": "reject", "reason": "新闻标题",
         "mode": "require_approve", "model": "m"},
    ])
    new_items = store.list_items(conn, cycle_id, "new", 10)
    assert [item["canonical"] for item in new_items] == ["甲"]
    assert new_items[0]["llm_review"]["verdict"] == "approve"
    assert new_items[0]["llm_review"]["reason"] == "像梗"
    # all 仍按 rank 返回两条，不被审核过滤
    assert [item["canonical"] for item in store.top_items(conn, cycle_id, 10)] == ["甲", "乙"]
    conn.close()


def test_require_approve_withholds_unreviewed_items():
    """超出 top50 未审的项在严格模式下不能冒充 approve。"""
    items = [_item("甲", "甲", 90.0, rank=0)]
    published, withheld = pipeline.apply_review_gate(items, {}, "require_approve")
    assert published == set() and withheld == {"甲"}
    assert items[0]["status"] == "rejected"


def test_require_approve_zero_approved_no_previous_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "require_approve")
    fake = FakeGate(lambda candidate: "reject")
    conn = store.connect(tmp_path / "zero.db")
    store.init_db(conn)
    snapshot = _run_offline(conn, reviewer=fake)
    cycle_id = snapshot["cycle"]["id"]
    stats = snapshot["cycle"]["stats"]

    assert snapshot["status"] == "degraded"
    assert stats["llm_approved"] == 0
    assert stats["llm_gate_applied"] is True
    assert stats["llm_fallback"] is True
    assert "no_approved" in stats["llm_fallback_reason"]
    assert stats["llm_new_list_cycle"] == 0
    assert store.list_items(conn, cycle_id, "new", 10) == []
    # 不退回未审项：all 仍存在，new 为空
    assert store.top_items(conn, cycle_id, 10)
    conn.close()


def test_require_approve_zero_approved_reuses_previous_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "require_approve")
    conn = store.connect(tmp_path / "carry.db")
    store.init_db(conn)

    first = _run_offline(conn, reviewer=FakeGate(lambda candidate: "approve"))
    assert first["cycle"]["stats"]["llm_approved"] == 1
    first_id = first["cycle"]["id"]

    second = _run_offline(conn, reviewer=FakeGate(lambda candidate: "reject"))
    second_id = second["cycle"]["id"]
    stats = second["cycle"]["stats"]

    assert stats["llm_approved"] == 0
    assert stats["llm_fallback_reason"] == "no_approved_previous_cycle"
    assert stats["llm_new_list_cycle"] == first_id
    carried = store.list_items(conn, second_id, "new", 10)
    assert [item["canonical"] for item in carried] == ["嘉豪"]
    assert carried[0]["llm_review"]["verdict"] == "approve"
    assert second["status"] == "done"
    conn.close()


def test_carry_from_previous_cycle_keeps_only_approved(tmp_path):
    """沿用上一轮时不限模式，但只带 approve；unclear 不能借沿用混入严格榜。"""
    conn = store.connect(tmp_path / "carry_filter.db")
    store.init_db(conn)
    prev = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, prev, "done", {"llm_mode": "demote", "llm_approved": 1})
    mapping = store.upsert_memes(conn, [
        {"family_key": "甲", "canonical": "甲", "type": "词"},
        {"family_key": "乙", "canonical": "乙", "type": "词"},
    ])
    store.insert_signals(conn, prev, [
        {"meme_id": mapping["甲"], "family_key": "甲", "score": 70.0, "rank": 1,
         "status": "observing", "components": {"list": "new"}, "sample": ""},
        {"meme_id": mapping["乙"], "family_key": "乙", "score": 60.0, "rank": 2,
         "status": "observing", "components": {"list": "new"}, "sample": ""},
    ])
    store.insert_llm_reviews(conn, prev, [
        {"family_key": "甲", "verdict": "approve", "reason": "像梗", "mode": "demote"},
        {"family_key": "乙", "verdict": "unclear", "reason": "证据不足", "mode": "demote"},
    ])
    current = store.start_cycle(conn, "2026-11-14T11:00:00+08:00")
    store.finish_cycle(conn, current, "done", {
        "llm_mode": "require_approve", "llm_approved": 0, "llm_new_list_cycle": prev,
    })
    carried = store.list_items(conn, current, "new", 10)
    assert [item["canonical"] for item in carried] == ["甲"]
    assert carried[0]["llm_review"]["verdict"] == "approve"
    conn.close()


# ---------------------------------------------------------------------------
# 降级路径
# ---------------------------------------------------------------------------

def test_review_exception_falls_back_to_rule_list(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "demote")
    fake = FakeGate(lambda candidate: "reject", error=RuntimeError("boom"))
    conn = store.connect(tmp_path / "exc.db")
    store.init_db(conn)
    snapshot = _run_offline(conn, reviewer=fake)
    cycle_id = snapshot["cycle"]["id"]
    stats = snapshot["cycle"]["stats"]

    assert stats["llm_fallback"] is True
    assert "RuntimeError" in stats["llm_fallback_reason"]
    assert stats["llm_calls"] == 0
    # 异常 → 规则榜（未审条目不撤回）
    assert [item["canonical"] for item in store.list_items(conn, cycle_id, "new", 10)] == ["嘉豪"]
    assert conn.execute("SELECT COUNT(*) FROM llm_review").fetchone()[0] == 0
    conn.close()


def test_review_budget_timeout_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "demote")
    monkeypatch.setattr(config, "LLM_BUDGET_SECONDS", 0.03)
    fake = FakeGate(lambda candidate: "reject", delay=0.2)
    conn = store.connect(tmp_path / "budget.db")
    store.init_db(conn)
    snapshot = _run_offline(conn, reviewer=fake)
    stats = snapshot["cycle"]["stats"]

    assert stats["llm_fallback"] is True
    assert stats["llm_fallback_reason"] == "timeout_budget"
    assert [item["canonical"] for item in store.list_items(
        conn, snapshot["cycle"]["id"], "new", 10)] == ["嘉豪"]
    conn.close()


def test_no_key_default_reviewer_falls_back_without_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "demote")
    monkeypatch.delenv("HOTMEME_LLM_API_KEY", raising=False)
    conn = store.connect(tmp_path / "nokey.db")
    store.init_db(conn)
    snapshot = _run_offline(conn)          # 默认 reviewer：无 key → None
    stats = snapshot["cycle"]["stats"]
    assert stats["llm_calls"] == 0
    assert stats["llm_fallback"] is True
    assert stats["llm_fallback_reason"] == "not_configured"
    assert store.list_items(conn, snapshot["cycle"]["id"], "new", 10)
    conn.close()


def test_require_approve_no_key_falls_back_to_rule_list(tmp_path, monkeypatch):
    """降级必须回规则榜：严格模式不能被「只留 approve」误伤成空榜。"""
    monkeypatch.setattr(config, "LLM_MODE", "require_approve")
    monkeypatch.delenv("HOTMEME_LLM_API_KEY", raising=False)
    conn = store.connect(tmp_path / "nokey_require.db")
    store.init_db(conn)
    snapshot = _run_offline(conn)
    cycle_id = snapshot["cycle"]["id"]
    stats = snapshot["cycle"]["stats"]
    assert stats["llm_fallback_reason"] == "not_configured"
    assert stats["llm_gate_applied"] is False
    assert [item["canonical"] for item in store.list_items(conn, cycle_id, "new", 10)] == ["嘉豪"]
    conn.close()


# ---------------------------------------------------------------------------
# 与 P2 review_candidates 的接线
# ---------------------------------------------------------------------------

def test_shadow_uses_review_candidates_with_fake_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "shadow")
    review.clear_cache()
    fake = FakeLLM({
        "verdict": "reject", "meme_prob": 0.1, "novelty": "unknown",
        "origin": "", "reason": "假客户端判定", "evidence_ids": [],
        "merge_into": "", "rubric": {"meme_likeness": 0.1},
    })
    conn = store.connect(tmp_path / "fake_llm.db")
    store.init_db(conn)
    snapshot = _run_offline(conn, reviewer=fake)
    cycle_id = snapshot["cycle"]["id"]
    stats = snapshot["cycle"]["stats"]

    assert fake.calls == 1
    assert stats["llm_calls"] == 1 and stats["llm_rounds"] == 1
    assert stats["llm_reviewed"] == 1
    row = conn.execute(
        "SELECT verdict, reason, model, prompt_version FROM llm_review WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    assert row["verdict"] == "reject"
    assert row["reason"] == "假客户端判定"
    assert row["model"] == "fake-llm"
    assert row["prompt_version"] == review.PROMPT_VERSION
    # 传给 LLM 的候选带 P1 证据字段
    assert "comment_7d_count" in fake.messages[0][1]["content"]
    conn.close()


def test_review_candidate_maps_p1_evidence_and_tolerates_missing():
    item = _item("嘉豪", "嘉豪", 45.6, rank=1)
    item["components"]["comment"].update({"seed_origin_year": 2025, "age_days": 16.0})
    family = {"members": ["嘉豪", "嘉豪哥"], "type": "人物"}
    docs = [
        TextDoc(text="嘉豪这梗笑死", bvid="BV1", kind="comment", likes=5),
        TextDoc(text="嘉豪 名场面", bvid="BV1", kind="danmaku"),
        TextDoc(text="无关内容", bvid="BV1", kind="comment"),
    ]
    docs[0].ctime = 1792053945
    usage = {"嘉豪": {"videos": {"BV1"}}}
    video_meta = {"BV1": {"title": "嘉豪合集", "zone": "生活"}}

    candidate = pipeline.review_candidate(item, family, usage, video_meta, docs)
    packet = review.EvidencePacket.from_candidate(candidate)
    assert candidate["candidate_id"] == "嘉豪"
    assert candidate["comment_7d_count"] == 3
    assert candidate["comment_df_video"] == 2
    assert candidate["age_days"] == 16.0
    assert candidate["seed_origin_year"] == 2025
    assert packet.comment_7d_count == 3
    assert len(packet.recent_comments) == 1
    assert packet.recent_comments[0]["text"] == "嘉豪这梗笑死"
    assert packet.danmaku_samples == ["嘉豪 名场面"]
    assert packet.videos == [{"title": "嘉豪合集", "zone": "生活"}]

    # 旧数据缺证据字段也不能崩，EvidencePacket 走 unclear 兜底
    bare = pipeline.review_candidate(
        {"family_key": "x", "canonical": "x", "components": {}}, {}, {}, {}, []
    )
    bare_packet = review.EvidencePacket.from_candidate(bare)
    assert bare_packet.comment_7d_count == 0
    assert bare_packet.missing_fields()


# ---------------------------------------------------------------------------
# 审计表迁移 / API
# ---------------------------------------------------------------------------

def test_llm_review_table_migration_and_idempotent_write(tmp_path):
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE llm_review(
               cycle_id INTEGER NOT NULL,
               family_key TEXT NOT NULL,
               verdict TEXT NOT NULL,
               meme_prob REAL NOT NULL DEFAULT 0,
               novelty TEXT NOT NULL DEFAULT '',
               origin TEXT NOT NULL DEFAULT '',
               reason TEXT NOT NULL DEFAULT '',
               evidence_hash TEXT NOT NULL DEFAULT '',
               mode TEXT NOT NULL DEFAULT '',
               model TEXT NOT NULL DEFAULT '',
               prompt_version TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               PRIMARY KEY(cycle_id, family_key));"""
    )
    old.commit()
    old.close()

    conn = store.connect(path)
    store.init_db(conn)        # 幂等迁移：补 merge_into
    store.init_db(conn)        # 重复执行不报错
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_review)")}
    assert {"cycle_id", "family_key", "verdict", "meme_prob", "novelty", "origin",
            "reason", "evidence_hash", "mode", "model", "prompt_version",
            "created_at", "merge_into"} <= columns

    cycle_id = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.insert_llm_reviews(conn, cycle_id, [
        {"family_key": "甲", "verdict": "reject", "reason": "旧"},
    ])
    store.insert_llm_reviews(conn, cycle_id, [
        {"family_key": "甲", "verdict": "approve", "reason": "新"},
    ])
    rows = conn.execute(
        "SELECT verdict, reason FROM llm_review WHERE cycle_id=?", (cycle_id,)
    ).fetchall()
    assert len(rows) == 1 and rows[0]["verdict"] == "approve" and rows[0]["reason"] == "新"
    conn.close()


def test_api_list_new_returns_reviewed_items(tmp_path, monkeypatch):
    path = tmp_path / "api_p3.db"
    conn = store.connect(path)
    store.init_db(conn)
    cycle_id = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    store.finish_cycle(conn, cycle_id, "done", {
        "llm_mode": "demote", "llm_calls": 2, "llm_approved": 1,
        "llm_rejected": 1, "llm_fallback": False, "llm_fallback_reason": "",
    })
    mapping = store.upsert_memes(conn, [
        {"family_key": "甲", "canonical": "甲", "type": "词"},
        {"family_key": "乙", "canonical": "乙", "type": "词"},
        {"family_key": "丙", "canonical": "丙", "type": "词"},
    ])
    store.insert_signals(conn, cycle_id, [
        {"meme_id": mapping["甲"], "family_key": "甲", "score": 70.0, "rank": 1,
         "status": "observing", "components": {"list": "new"}, "sample": ""},
        {"meme_id": mapping["乙"], "family_key": "乙", "score": 60.0, "rank": 2,
         "status": "observing", "components": {"list": "new"}, "sample": ""},
        {"meme_id": mapping["丙"], "family_key": "丙", "score": 50.0, "rank": 3,
         "status": "observing", "components": {"list": "classic"}, "sample": ""},
    ])
    store.insert_llm_reviews(conn, cycle_id, [
        {"family_key": "甲", "verdict": "approve", "reason": "像梗", "mode": "demote"},
        {"family_key": "乙", "verdict": "reject", "reason": "新闻", "mode": "demote"},
    ])
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    from app import api as api_module
    api_module = importlib.reload(api_module)
    with TestClient(api_module.app) as client:
        new_body = client.get("/api/top?list=new").json()
        assert [item["canonical"] for item in new_body["top"]] == ["甲"]
        assert new_body["top"][0]["llm_review"]["verdict"] == "approve"
        assert new_body["llm"]["mode"] == "demote"
        assert new_body["llm"]["approved"] == 1

        all_body = client.get("/api/top").json()
        assert [item["canonical"] for item in all_body["top"]] == ["甲", "乙", "丙"]

        classic_body = client.get("/api/top?list=classic").json()
        assert [item["canonical"] for item in classic_body["top"]] == ["丙"]
