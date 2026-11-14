"""P5：LLM 候选抽取接进流水线（key 文件 / 配置 / 合并去重 / known / 降级 / 同轮审核）。"""
import importlib
import json
import os
import time

from app import config, pipeline, store
from app.bili import BiliClient
from app.extract import Candidate, TextDoc
from app.llm import LlmConfig

BV1 = "BV1nwZEYqE2B"
BV2 = "BV1ArZnYUE9a"


# ---------------------------------------------------------------------------
# 假抽取器 / 文档
# ---------------------------------------------------------------------------

class FakeExtractor:
    """返回 list 或 report dict；记录被喂入的评论。"""

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else []
        self.error = error
        self.calls = 0
        self.comments = []

    def __call__(self, comments):
        self.calls += 1
        self.comments = [dict(item) for item in comments]
        if self.error is not None:
            raise self.error
        return self.result(comments) if callable(self.result) else self.result


def _doc(text, bvid=BV1, zone="鬼畜调教", like=10, rpid=1, age_days=2):
    doc = TextDoc(text=text, bvid=bvid, zone=zone, kind="comment", likes=like)
    doc.ctime = int(time.time() - age_days * 86400)
    doc.rpid = rpid
    return doc


def _candidate(surface, count=3, df_video=2):
    return Candidate(surface=surface, norm=surface, count=count,
                     df_video=df_video, df_zone=1)


def _llm_item(surface, evidence_ids, *, canonical=None, known=False,
              meaning="测试释义", confidence=0.8, type_="词"):
    return {
        "surface": surface,
        "canonical": canonical or surface,
        "type": type_,
        "meaning": meaning,
        "origin_quote": f"原文里出现了{surface}",
        "evidence_ids": evidence_ids,
        "confidence": confidence,
        "known": known,
    }


def _run_cycle(monkeypatch, tmp_path, docs, extractor, *, discover_candidates=None,
               reviewer=None, db_name="p5.db"):
    monkeypatch.setattr(
        pipeline, "collect_docs",
        lambda *args, **kwargs: (
            list(docs),
            {"comments": len(docs), "comments_recent": len(docs),
             "comments_7d": len(docs)},
        ),
    )
    if discover_candidates is not None:
        def fake_discover(docs, data_dir=None):
            from app.extract import prepare

            prepare(docs)   # 真实 discover 会先 prepare；patched 版本也要补 segs
            return list(discover_candidates)

        monkeypatch.setattr(pipeline, "discover", fake_discover)
    conn = store.connect(tmp_path / db_name)
    store.init_db(conn)
    snapshot = pipeline.run_cycle(
        conn=conn, client=BiliClient(offline=True), reviewer=reviewer,
        extractor=extractor,
    )
    return conn, snapshot


# ---------------------------------------------------------------------------
# 1. key 文件
# ---------------------------------------------------------------------------

def test_key_file_is_read_and_stripped(tmp_path):
    key_file = tmp_path / "hotmeme_key"
    key_file.write_text("  sk-file-secret-123\n", encoding="utf-8")
    key_file.chmod(0o600)

    cfg = LlmConfig.from_env({"HOTMEME_LLM_API_KEY_FILE": str(key_file)})
    assert cfg.configured is True
    assert cfg.api_key == "sk-file-secret-123"
    assert "sk-file-secret-123" not in json.dumps(cfg.redacted())
    assert "sk-file-secret-123" not in repr(cfg)
    assert cfg.redacted()["api_key"] == "***"


def test_env_key_takes_precedence_over_key_file(tmp_path):
    key_file = tmp_path / "hotmeme_key"
    key_file.write_text("file-secret", encoding="utf-8")
    cfg = LlmConfig.from_env({
        "HOTMEME_LLM_API_KEY": " env-secret ",
        "HOTMEME_LLM_API_KEY_FILE": str(key_file),
    })
    assert cfg.api_key == "env-secret"
    assert "env-secret" not in json.dumps(cfg.redacted())
    assert "env-secret" not in repr(cfg)


def test_missing_or_blank_key_file_is_not_configured(tmp_path):
    blank = tmp_path / "blank"
    blank.write_text("   \n\t", encoding="utf-8")
    assert LlmConfig.from_env({"HOTMEME_LLM_API_KEY_FILE": str(blank)}).configured is False
    assert LlmConfig.from_env({
        "HOTMEME_LLM_API_KEY_FILE": str(tmp_path / "missing")
    }).configured is False
    assert LlmConfig.from_env({}).configured is False


# ---------------------------------------------------------------------------
# 2. 配置：默认 0，非法值回默认
# ---------------------------------------------------------------------------

def test_extract_config_defaults_and_invalid_values(monkeypatch):
    import app.config as config_module

    names = ("HOTMEME_LLM_EXTRACT", "HOTMEME_LLM_EXTRACT_MAX_CALLS",
             "HOTMEME_LLM_EXTRACT_MAX_COMMENTS")
    original = {name: os.environ.get(name) for name in names}
    monkeypatch.setenv("HOTMEME_LLM_EXTRACT", "maybe")
    monkeypatch.setenv("HOTMEME_LLM_EXTRACT_MAX_CALLS", "99")
    monkeypatch.setenv("HOTMEME_LLM_EXTRACT_MAX_COMMENTS", "abc")
    reloaded = importlib.reload(config_module)
    try:
        assert reloaded.LLM_EXTRACT_ENABLED is False
        assert reloaded.LLM_EXTRACT_MAX_CALLS == 3
        assert reloaded.LLM_EXTRACT_MAX_COMMENTS == 450

        monkeypatch.setenv("HOTMEME_LLM_EXTRACT", "1")
        monkeypatch.setenv("HOTMEME_LLM_EXTRACT_MAX_CALLS", "2")
        monkeypatch.setenv("HOTMEME_LLM_EXTRACT_MAX_COMMENTS", "600")
        reloaded = importlib.reload(config_module)
        assert reloaded.LLM_EXTRACT_ENABLED is True
        assert reloaded.LLM_EXTRACT_MAX_CALLS == 2
        assert reloaded.LLM_EXTRACT_MAX_COMMENTS == 600
    finally:
        for name, value in original.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        importlib.reload(config_module)


# ---------------------------------------------------------------------------
# 3. 关闭 / 空输入 / 无 key / 失败降级
# ---------------------------------------------------------------------------

def test_disabled_does_not_call_extractor_and_still_has_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACT_ENABLED", False)
    fake = FakeExtractor([_llm_item("典中典", ["1"])])
    conn, snapshot = _run_cycle(
        monkeypatch, tmp_path, [_doc("典中典")], fake,
        discover_candidates=[_candidate("旧梗")],
    )
    stats = snapshot["cycle"]["stats"]
    assert fake.calls == 0
    assert stats["llm_extract_reason"] == "disabled"
    assert stats["llm_extract_calls"] == 0
    assert stats["llm_extract_details"] == []
    # gate stats 的 update 不应清掉抽取字段
    assert stats["llm_approved"] == 0 and stats["llm_mode"] == "off"
    conn.close()


def test_zero_comments_reports_empty_input(monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACT_ENABLED", True)
    fake = FakeExtractor([_llm_item("典中典", ["1"])])
    candidates, index, stats = pipeline.run_llm_extraction(
        [TextDoc(text="标题", bvid=BV1, kind="title")], {}, extractor=fake
    )
    assert fake.calls == 0
    assert candidates == [] and index == {}
    assert stats["llm_extract_reason"] == "empty_input"
    assert stats["llm_extract_calls"] == 0


def test_no_key_reports_not_configured_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACT_ENABLED", True)
    monkeypatch.delenv("HOTMEME_LLM_API_KEY", raising=False)
    monkeypatch.delenv("HOTMEME_LLM_API_KEY_FILE", raising=False)
    docs = [_doc("典中典", rpid=11)]
    candidates, _, stats = pipeline.run_llm_extraction(
        docs, {}, extractor=pipeline.DEFAULT_EXTRACTOR
    )
    assert candidates == []
    assert stats["llm_extract_reason"] == "not_configured"
    assert stats["llm_extract_calls"] == 0

    # 整轮也要记账，且规则榜照旧产出
    conn, snapshot = _run_cycle(
        monkeypatch, tmp_path, docs, pipeline.DEFAULT_EXTRACTOR,
        discover_candidates=[_candidate("旧梗")],
    )
    cycle_stats = snapshot["cycle"]["stats"]
    assert cycle_stats["llm_extract_reason"] == "not_configured"
    assert cycle_stats["llm_extract_calls"] == 0
    assert cycle_stats["candidates"] >= 1
    conn.close()


def test_extractor_failure_degrades_to_rule_board(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACT_ENABLED", True)
    fake = FakeExtractor(error=RuntimeError("boom"))
    conn, snapshot = _run_cycle(
        monkeypatch, tmp_path, [_doc("典中典")], fake,
        discover_candidates=[_candidate("旧梗")],
    )
    stats = snapshot["cycle"]["stats"]
    assert fake.calls == 1
    assert stats["llm_extract_reason"] == "error"
    assert "RuntimeError" in stats["llm_extract_error"]
    assert stats["llm_extract_candidates"] == 0
    assert stats["llm_extract_added"] == 0
    assert snapshot["cycle"]["status"] in ("done", "degraded")
    conn.close()


# ---------------------------------------------------------------------------
# 4. 合并 / 去重 / metadata
# ---------------------------------------------------------------------------

def test_existing_candidate_gets_metadata_without_recount():
    existing = Candidate(surface="典中典", norm="典中典", count=7,
                         df_video=3, df_zone=2)
    comment_index = {
        "r1": {"bvid": BV1, "zone": "鬼畜调教"},
        "r2": {"bvid": BV2, "zone": "鬼畜剧场"},
    }
    merged, stats = pipeline.merge_llm_candidates(
        [existing],
        [_llm_item("典中典", ["r1", "r2"], known=True, meaning="经典重现")],
        comment_index,
    )
    assert merged[0] is existing
    assert existing.count == 7 and existing.df_video == 3 and existing.df_zone == 2
    assert existing.metadata["source"] == "n-gram+llm_extract"
    assert existing.metadata["meaning"] == "经典重现"
    assert existing.metadata["known"] is True
    assert stats == {"merged": 1, "added": 0, "known": 1, "skipped": 0}


def test_new_candidate_counts_from_evidence_and_dedupes():
    comment_index = {
        "r1": {"bvid": BV1, "zone": "鬼畜调教"},
        "r2": {"bvid": BV2, "zone": "鬼畜剧场"},
        "r3": {"bvid": BV2, "zone": "鬼畜剧场"},
    }
    items = [
        _llm_item("新梗复活", ["r1", "r2", "unknown-evidence"]),
        _llm_item("新梗复活", ["r3"]),   # 同 canonical：跳过，不重复计数
        "not-a-dict",
    ]
    candidates, stats = pipeline.merge_llm_candidates([], items, comment_index)
    assert stats["added"] == 1
    assert stats["skipped"] == 2
    candidate = candidates[0]
    assert candidate.surface == "新梗复活"
    assert candidate.count == 2
    assert candidate.df_video == 2
    assert candidate.df_zone == 2
    assert candidate.kinds == {"llm_extract"}
    assert candidate.metadata["source"] == "llm_extract"


def test_metadata_defaults_and_family_attach():
    meta = pipeline._llm_metadata({"surface": "只有表面"})
    assert meta["type"] == "词"
    assert meta["meaning"] == "" and meta["origin_quote"] == ""
    assert meta["confidence"] == 0.0
    assert meta["known"] is False and meta["evidence_ids"] == []

    candidate = _candidate("典中典")
    candidate.metadata = {**meta, "meaning": "经典重现", "known": True,
                          "confidence": 0.9}
    families = pipeline.merge_families([candidate], {}, {})
    attached = pipeline.attach_llm_metadata(families, [candidate])
    assert attached == {"attached": 1, "known": 1}
    assert families[0]["llm_known"] is True
    assert families[0]["llm_extract"]["meaning"] == "经典重现"


# ---------------------------------------------------------------------------
# 5. known：不走评论证据旁路
# ---------------------------------------------------------------------------

def _known_cycle(monkeypatch, tmp_path, known, db_name):
    docs = [
        _doc("老梗复活第一弹", bvid=BV1, rpid=101),
        _doc("老梗复活第二弹", bvid=BV2, rpid=102),
        _doc("老梗复活第三弹", bvid=BV1, rpid=103),
    ]
    fake = FakeExtractor([_llm_item("老梗复活", ["101", "102", "103"],
                                    known=known)])
    monkeypatch.setattr(config, "LLM_EXTRACT_ENABLED", True)
    monkeypatch.setattr(config, "LLM_MODE", "off")
    monkeypatch.setattr(pipeline, "passes_meme_gate",
                        lambda score, seed_hit=False: False)
    conn, snapshot = _run_cycle(
        monkeypatch, tmp_path, docs, fake, discover_candidates=[],
        db_name=db_name,
    )
    return conn, snapshot["cycle"]["stats"]


def test_known_does_not_use_comment_backed_bypass(tmp_path, monkeypatch):
    conn, stats = _known_cycle(monkeypatch, tmp_path, True, "known.db")
    assert stats["llm_extract_known"] == 1
    assert stats["llm_extract_known_families"] == 1
    assert stats["llm_extract_known_blocked"] == 1
    assert stats["comment_backed_admitted"] == 0
    conn.close()

    # 同样语料但不标 known：应走评论证据旁路进 observing（对照）
    conn, normal = _known_cycle(monkeypatch, tmp_path, False, "normal.db")
    assert normal["llm_extract_known"] == 0
    assert normal["llm_extract_known_blocked"] == 0
    assert normal["comment_backed_admitted"] == 1
    conn.close()


# ---------------------------------------------------------------------------
# 6. 抽取与审核同轮；metadata 落库
# ---------------------------------------------------------------------------

class FakeReviewer:
    model = "fake-reviewer"

    def __init__(self):
        self.calls = 0
        self.candidates = []

    def __call__(self, candidates, **kwargs):
        self.calls += 1
        self.candidates = [dict(item) for item in candidates]
        entries = [{
            "candidate_id": item["candidate_id"],
            "canonical": item["canonical"],
            "verdict": "approve",
            "meme_prob": 0.9,
            "novelty": "recent",
            "origin": "",
            "reason": "fake approve",
            "evidence_hash": "hash-" + item["canonical"],
            "model": "fake-reviewer",
            "prompt_version": "p2-review-v1",
        } for item in candidates]
        return {
            "approved": entries,
            "rejected": [],
            "merged": [],
            "log": entries,
            "rounds": 1,
            "calls": len(entries),
            "reviewed": len(entries),
            "prompt_version": "p2-review-v1",
        }


def test_extraction_and_review_run_in_same_cycle(tmp_path, monkeypatch):
    docs = [
        _doc("新梗复活第一弹", bvid=BV1, rpid=201),
        _doc("新梗复活第二弹", bvid=BV2, rpid=202),
        _doc("新梗复活第三弹", bvid=BV1, rpid=203),
    ]
    fake = FakeExtractor([_llm_item("新梗复活", ["201", "202", "203"],
                                    meaning="经典重现")])
    reviewer = FakeReviewer()
    monkeypatch.setattr(config, "LLM_EXTRACT_ENABLED", True)
    monkeypatch.setattr(config, "LLM_MODE", "shadow")
    monkeypatch.setattr(pipeline, "passes_meme_gate",
                        lambda score, seed_hit=False: True)
    conn, snapshot = _run_cycle(
        monkeypatch, tmp_path, docs, fake, discover_candidates=[],
        reviewer=reviewer, db_name="same_cycle.db",
    )
    stats = snapshot["cycle"]["stats"]
    assert fake.calls == 1 and reviewer.calls == 1
    assert stats["llm_extract_candidates"] == 1
    assert stats["llm_extract_added"] == 1
    assert stats["llm_extract_reason"] == "ok"
    assert stats["llm_approved"] >= 1
    # 同轮抽取与审核字段共存（gate_stats update 不覆盖抽取 stats）
    assert stats["llm_extract_details"][0]["surface"] == "新梗复活"

    row = conn.execute(
        "SELECT components_json FROM meme_signal WHERE cycle_id=("
        "SELECT MAX(id) FROM cycle)"
    ).fetchone()
    assert row is not None
    components = json.loads(row[0])
    assert components["llm_extract"]["meaning"] == "经典重现"
    assert components["llm_known"] is False
    conn.close()


# ---------------------------------------------------------------------------
# 7. shadow 脚本：无 key 明确退出 2；拒绝生产库
# ---------------------------------------------------------------------------

def _run_shadow(args, env):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, "tools/shadow_run.py", *args],
        cwd=root, env=env, capture_output=True, text=True, timeout=60,
    )


def test_shadow_run_without_key_exits_2_and_does_not_create_db(tmp_path):
    env = os.environ.copy()
    env.pop("HOTMEME_LLM_API_KEY", None)
    env.pop("HOTMEME_LLM_API_KEY_FILE", None)
    db = tmp_path / "shadow.db"
    result = _run_shadow(["--db", str(db), "--json", str(tmp_path / "out.json")], env)
    assert result.returncode == 2
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["reason"] == "not_configured"
    assert not db.exists()
    assert not (tmp_path / "out.json").exists()


def test_shadow_run_refuses_production_db():
    env = os.environ.copy()
    result = _run_shadow(["--db", "data/hotmeme.db"], env)
    assert result.returncode == 2
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["reason"] == "bad_db"
