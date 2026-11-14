"""P6 回归：审核池优先级（抽取候选 > 评论证据 > 规则分）、池上限与 shadow 收尾。"""
import importlib.util
import json
from pathlib import Path

from app import config, pipeline, store


def _item(canonical, score, *, kind=None, status="observing", list_type="new",
          known=False, rank=0):
    """构造 list=new 的 pipeline item；kind=extract/comment 走 P5/P1 标记。"""
    components = {"list": list_type}
    raw = {}
    if kind == "extract":
        meta = {"source": "llm_extract", "known": known, "confidence": 0.7}
        components["llm_extract"] = meta
        components["llm_known"] = known
        raw["llm_extract"] = meta
    elif kind == "comment":
        components["comment_backed"] = True
    return {
        "family_key": canonical,
        "canonical": canonical,
        "score": score,
        "rank": rank,
        "status": status,
        "list": list_type,
        "components": components,
        "raw_signals": raw,
        "comment_backed": kind == "comment",
    }


def _entry(candidate, verdict):
    return {
        "candidate_id": candidate["candidate_id"],
        "canonical": candidate["canonical"],
        "verdict": verdict,
        "meme_prob": 0.9 if verdict == "approve" else 0.1,
        "novelty": "recent" if verdict == "approve" else "unknown",
        "origin": "测试",
        "reason": f"fake {verdict}",
        "evidence_ids": [],
        "merge_into": "",
        "evidence_hash": "hash",
        "model": "fake-p6",
        "prompt_version": "p2-review-v1",
    }


class FakeGate:
    """P3 同款 callable 审核器：按 canonical 决定 verdict，记录收到的候选顺序。"""

    model = "fake-p6"

    def __init__(self, decide):
        self.decide = decide
        self.calls = 0
        self.candidates = []

    def __call__(self, candidates, **kwargs):
        self.calls += 1
        self.candidates = [dict(item) for item in candidates]
        approved, rejected, log = [], [], []
        for candidate in candidates:
            verdict = self.decide(candidate)
            entry = _entry(candidate, verdict)
            log.append(entry)
            (approved if verdict == "approve" else rejected).append(entry)
        return {
            "approved": approved,
            "rejected": rejected,
            "merged": [],
            "rounds": 1,
            "calls": len(candidates),
            "log": log,
            "degraded_reason": "",
            "prompt_version": "p2-review-v1",
        }


# ---------------------------------------------------------------------------
# 1. review_pool 优先级与上限
# ---------------------------------------------------------------------------

def test_review_pool_extract_first_then_comment_then_score(monkeypatch):
    monkeypatch.setattr(config, "LLM_REVIEW_LIMIT", 3)
    items = [
        _item("垃圾99", 99.0),
        _item("抽取高分", 20.0, kind="extract"),
        _item("评论证据", 50.0, kind="comment"),
        _item("抽取低分", 10.0, kind="extract"),
    ]
    pool = pipeline.review_pool(items)
    assert [item["canonical"] for item in pool] == ["抽取高分", "抽取低分", "评论证据"]
    composition = pipeline.review_pool_stats(pool)
    assert composition["llm_pool_extract"] == 2
    assert composition["llm_pool_comment"] == 1
    assert composition["llm_pool_score"] == 0
    assert [detail["pool_kind"] for detail in composition["llm_pool_details"]] == [
        "extract", "extract", "comment"]


def test_review_pool_low_score_extract_not_crowded_out_by_garbage(monkeypatch):
    monkeypatch.setattr(config, "LLM_REVIEW_LIMIT", 24)
    items = [_item(f"垃圾{i}", 90.0 + i) for i in range(30)]
    items += [_item(f"抽取{i}", 1.0, kind="extract") for i in range(20)]
    pool = pipeline.review_pool(items)
    assert len(pool) == 24
    # 20 条抽取候选全部进池；剩余 4 个名额才轮到规则分（垃圾不会挤掉抽取）
    assert sum(1 for item in pool if item["canonical"].startswith("抽取")) == 20
    assert [item["canonical"] for item in pool[20:]] == ["垃圾29", "垃圾28", "垃圾27", "垃圾26"]


def test_review_pool_capped_at_llm_review_limit_and_skips_rejected(monkeypatch):
    monkeypatch.setattr(config, "LLM_REVIEW_LIMIT", 24)
    items = [_item(f"抽取{i}", 1.0, kind="extract") for i in range(30)]
    items.append(_item("已拒", 99.0, kind="extract", status="rejected"))
    pool = pipeline.review_pool(items)
    assert len(pool) == 24
    assert all(item["status"] == "observing" for item in pool)
    assert "已拒" not in {item["canonical"] for item in pool}


def test_review_pool_recognizes_raw_signals_source_shape(monkeypatch):
    monkeypatch.setattr(config, "LLM_REVIEW_LIMIT", 2)
    raw_item = {
        "family_key": "raw", "canonical": "raw", "score": 1.0, "rank": 0,
        "status": "observing", "list": "new",
        "components": {}, "raw_signals": {"source": "n-gram+llm_extract"},
    }
    meta_item = {
        "family_key": "meta", "canonical": "meta", "score": 1.0, "rank": 0,
        "status": "observing", "list": "new",
        "components": {"llm_extract": {"source": "llm_extract"}},
        "raw_signals": {},
    }
    pool = pipeline.review_pool([_item("垃圾", 99.0), raw_item, meta_item])
    assert {item["canonical"] for item in pool} == {"raw", "meta"}


def test_review_pool_limit_and_call_budget_defaults_aligned():
    """P6：池上限 24 = 单轮调用预算 24，保证池内每条都能审到。"""
    assert config.LLM_REVIEW_LIMIT == 24
    assert config.LLM_MAX_CALLS == 24


# ---------------------------------------------------------------------------
# 2. gate stats 构成 + known 不放水
# ---------------------------------------------------------------------------

def test_gate_stats_composition_and_known_not_auto_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "shadow")
    monkeypatch.setattr(config, "LLM_REVIEW_LIMIT", 24)
    items = [
        _item("抽取普通", 10.0, kind="extract"),
        _item("抽取已知", 20.0, kind="extract", known=True),
        _item("评论证据", 50.0, kind="comment"),
        _item("抽取被拒", 5.0, kind="extract", status="rejected"),
    ] + [_item(f"垃圾{i}", 90.0 + i) for i in range(21)]
    fake = FakeGate(
        lambda candidate: "approve" if candidate["canonical"] == "抽取已知" else "reject"
    )
    conn = store.connect(tmp_path / "p6_gate.db")
    store.init_db(conn)
    cycle_id = store.start_cycle(conn, "2026-11-14T10:00:00+08:00")
    stats = pipeline.llm_stats_defaults()
    stats_out, status = pipeline._run_llm_gate(
        conn, cycle_id, items, {}, [], {}, {}, fake, stats,
    )
    assert status == ""
    assert fake.calls == 1
    assert stats_out["llm_pool"] == 24
    assert stats_out["llm_pool_extract"] == 2
    assert stats_out["llm_pool_comment"] == 1
    assert stats_out["llm_pool_score"] == 21
    assert stats_out["llm_extract_new_total"] == 3
    assert stats_out["llm_extract_admitted_total"] == 2
    # 抽取候选排在最前；known 候选照常送审，只有明确 approve 才计通过
    assert fake.candidates[0]["canonical"] == "抽取已知"
    assert stats_out["llm_approved"] == 1
    assert stats_out["llm_reviewed"] == 24
    rows = conn.execute(
        "SELECT family_key, verdict FROM llm_review WHERE cycle_id=?", (cycle_id,)
    ).fetchall()
    verdicts = {row["family_key"]: row["verdict"] for row in rows}
    assert verdicts["抽取已知"] == "approve"
    assert verdicts["抽取普通"] == "reject"
    conn.close()


# ---------------------------------------------------------------------------
# 3. shadow 脚本：3 轮正常收尾写 JSON、exit 0、无 `.closed` 崩溃
# ---------------------------------------------------------------------------

def _load_shadow_module():
    path = Path(__file__).resolve().parent.parent / "tools" / "shadow_run.py"
    spec = importlib.util.spec_from_file_location("p6_shadow_run", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_snapshot(round_index, stats):
    return {
        "status": "done",
        "cycle": {"id": round_index, "status": "done", "stats": stats},
        "top": [],
        "health": {},
    }


def test_shadow_run_three_rounds_writes_json_and_exits_0(tmp_path, monkeypatch):
    from app import llm

    shadow = _load_shadow_module()
    monkeypatch.setenv("HOTMEME_LLM_API_KEY", "fake-p6-key-not-printed")
    monkeypatch.setattr(llm, "ChatClient", _FakeShadowClient)
    monkeypatch.setattr("app.bili.BiliClient", lambda: object())

    runs = {"count": 0}

    def fake_run_cycle(conn=None, client=None, reviewer=None, extractor=None):
        runs["count"] += 1
        reviewer.stats["calls"] += 2
        reviewer.stats["prompt_tokens"] += 100
        stats = {
            "llm_mode": "shadow",
            "llm_calls": 2,
            "llm_rounds": 2,
            "llm_approved": 0,
            "llm_rejected": 2,
            "llm_reviewed": 2,
            "llm_pool": 2,
            "llm_pool_extract": 1,
            "llm_pool_comment": 1,
            "llm_pool_score": 0,
            "llm_extract_new_total": 3,
            "llm_extract_admitted_total": 1,
            "llm_extract_candidates": 2,
            "llm_extract_calls": 1,
            "llm_extract_details": [{"surface": "给我整不会了", "known": False}],
            "llm_pool_details": [
                {"family_key": "本鱼", "canonical": "本鱼", "score": 72.0,
                 "pool_kind": "extract", "known": False},
            ],
        }
        return _fake_snapshot(runs["count"], stats)

    monkeypatch.setattr(pipeline, "run_cycle", fake_run_cycle)
    db = tmp_path / "shadow_p7.db"
    out = tmp_path / "shadow_p7.json"
    code = shadow.main([
        "--rounds", "3", "--db", str(db), "--json", str(out),
    ])
    assert code == 0
    assert runs["count"] == 3
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert len(payload["rounds"]) == 3
    assert payload["rounds"][0]["review"]["pool_extract"] == 1
    assert payload["rounds"][0]["extract"]["candidates_detail"][0]["surface"] == "给我整不会了"
    assert payload["totals"]["review_calls"] == 6
    # 每轮独立 ChatClient：避免 llm.py 的进程内累计 max_calls 误伤第 3 轮
    assert len(_FakeShadowClient.instances) == 3
    text = out.read_text(encoding="utf-8")
    assert "fake-p6-key-not-printed" not in text
    # 成功路径也会走 finally 关连接；旧代码的 conn.closed 会在这里 AttributeError


class _FakeShadowClient:
    instances = []

    def __init__(self, config=None):
        _FakeShadowClient.instances.append(self)
        self.stats = {
            "calls": 0, "retries": 0, "errors": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }

    def stats_dict(self):
        return dict(self.stats)
