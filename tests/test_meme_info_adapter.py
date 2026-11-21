"""README shadow adapter 测试：读端状态机 + `/api/meme-info` 只读契约（不联网）。

覆盖：ok/missing/invalid/stale、limit、only=publishable、evidence 截断、
publishable 标记、mtime 缓存、`/api/top` 行为不变、无 DB 写入、跨模块边界。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, meme_info_adapter
from app import api as api_module


# ---------------------------------------------------------------- fixtures
def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat()


def make_meme(rank: int = 1, *, canonical: str | None = None, status: str = "hot",
              rate_2x2: bool = True, with_verified: bool = True) -> dict:
    canonical = canonical or f"梗{rank}"
    meme = {
        "family_key": f"family_{rank}",
        "canonical": canonical,
        "norm": f"norm_{rank}",
        "type": "句式",
        "meaning": "测" * 400,
        "score": 70.123456,
        "status": status,
        "members": ["a", "b"],
        "signals": {
            "evidence_rate_2x2": rate_2x2,
            "meta_videos": 2,
            "usage_docs": 5,
            "usage_videos": 4,
            "is_current": True,
        },
        "verified": (
            {
                "is_meme": True,
                "is_current": True,
                "heat": 78.0,
                "confidence": 0.82,
                "reason": "理" * 500,
                "evidence_ids": ["x", "y"],
                "model": "deepseek-chat",
                "error": "",
            }
            if with_verified
            else None
        ),
        # 大字段：原始报告里的全量数组，绝不能透传给前端
        "meta": [{"bvid": f"BV{rank}{i}", "title": "t" * 100} for i in range(3)],
        "usage": [{"doc_id": f"d{i}", "text": "u" * 100} for i in range(4)],
        "candidates": [{"surface": "s" * 50} for _ in range(5)],
        "evidence": [
            {
                "comment_id": f"c{i}",
                "quote": "引" * 300,
                "ctime": 1790000000 + i,
                "like": 7 + i,
                "bvid": f"BV1rank{i}",
                "kind": "comment",
                "video_title": "标题",
            }
            for i in range(4)
        ],
    }
    return meme


def make_report(*, generated_at: str | None = None, memes: list[dict] | None = None,
                schema_version: str = "meme-info/v1") -> dict:
    return {
        "schema_version": schema_version,
        "prompt_version": "meme-info/verify-v4",
        "generated_at": _iso() if generated_at is None else generated_at,
        "as_of": _iso(),
        "window_days": 30,
        "memes": memes if memes is not None else [
            make_meme(1, canonical="吃了不烧心", status="hot", rate_2x2=True),
            make_meme(2, canonical="扎啤", status="rising", rate_2x2=False),
            make_meme(3, canonical="观察中", status="observing", rate_2x2=True),
        ],
        "observing": [],
        "candidates": [{"surface": "s" * 50} for _ in range(200)],
        "stats": {
            "videos": 45,
            "comments": 900,
            "danmaku": 2000,
            "docs": 3000,
            "families": 30,
            "current_families": 3,
            "current_2x2": 2,
            "observing_families": 1,
            "discovery_candidates": 120,
            "requests_total": 300,
            "collection_requests": 300,
            "elapsed_total": 180.0,
            "budget_exhausted": False,
            "verification": {
                "attempted": 2,
                "ok": 2,
                "rejected": 0,
                "errors": 0,
                "llm": {
                    "calls": 2,
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "total_tokens": 1200,
                    "errors": [],
                },
                "llm_config": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "configured": True,
                },
            },
            "discovery_audit": {"big": ["x" * 100]},
            "endpoint_counts": {"big": 1},
        },
        "config": {"huge": "x" * 1000},
        "notes": ["n"],
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    meme_info_adapter.clear_cache()
    monkeypatch.setattr(config, "MEME_INFO_REPORT_PATH", tmp_path / "report.json")
    monkeypatch.setattr(config, "MEME_INFO_MAX_AGE_SECONDS", 3600)
    yield
    meme_info_adapter.clear_cache()


@pytest.fixture
def report_file(tmp_path) -> Path:
    return tmp_path / "report.json"


def _write(path: Path, payload) -> None:
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    # 强制 mtime 变化，避免同 ns 内写两次导致缓存命中
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))


# ---------------------------------------------------------------- 读端状态机
def test_ok_trimmed_contract(report_file):
    _write(report_file, make_report())
    data = meme_info_adapter.load_report()
    assert data["status"] == "ok"
    assert data["count"] == 3
    assert data["total_memes"] == 3
    assert data["age_seconds"] >= 0
    first = data["memes"][0]
    assert first["rank"] == 1 and first["family_key"] == "family_1"
    assert first["canonical"] == "吃了不烧心"
    assert first["score"] == 70.12                 # 四舍五入两位
    assert first["publishable"] is True
    assert first["evidence_rate_2x2"] is True
    assert first["meta_count"] == 3 and first["usage_count"] == 4
    # verified 精简 + reason 截断
    assert first["verified"]["is_meme"] is True
    assert "model" not in first["verified"]  # 不透传 model/evidence_ids
    assert "evidence_ids" not in first["verified"]
    assert len(first["verified"]["reason"]) <= 200
    # evidence 只保留前 2 条且截断
    assert len(first["evidence"]) == 2
    assert set(first["evidence"][0]) == {"quote", "ctime", "like", "bvid"}
    assert len(first["evidence"][0]["quote"]) <= 120
    # 大字段不透传
    for forbidden in ("candidates", "meta", "usage", "components", "reasons"):
        assert forbidden not in first
    assert "candidates" not in data
    assert "discovery_audit" not in data["stats"]
    assert data["stats"]["videos"] == 45
    assert data["verification"]["llm"]["calls"] == 2
    assert data["verification"]["configured"] is True
    assert len(json.dumps(data, ensure_ascii=False)) < 20_000  # 远小于原始报告


def test_publishable_marking(report_file):
    _write(report_file, make_report())
    data = meme_info_adapter.load_report()
    flags = {m["canonical"]: m["publishable"] for m in data["memes"]}
    # 2026-11-15：2×2 只是徽标；LLM is_meme/is_current=true 的 rising 也 publishable
    assert flags == {"吃了不烧心": True, "扎啤": True, "观察中": False}
    # 只做标记，不硬过滤：all 仍返回 3 条
    assert data["count"] == 3
    assert data["publishable_count"] == 2
    all_items = meme_info_adapter.load_report(only="all")
    assert all_items["count"] == 3
    publishable = meme_info_adapter.load_report(only="publishable")
    assert publishable["count"] == 2
    assert [m["canonical"] for m in publishable["memes"]] == ["吃了不烧心", "扎啤"]
    assert publishable["total_memes"] == 3
    with pytest.raises(ValueError):
        meme_info_adapter.load_report(only="bogus")


def test_limit_and_rank(report_file):
    memes = [make_meme(i + 1, canonical=f"梗{i + 1}") for i in range(5)]
    _write(report_file, make_report(memes=memes))
    data = meme_info_adapter.load_report(limit=2)
    assert [m["rank"] for m in data["memes"]] == [1, 2]
    assert data["total_memes"] == 5
    assert meme_info_adapter.load_report(limit=0)["count"] == 1     # 下界 1
    assert meme_info_adapter.load_report(limit=999)["count"] == 5   # 上界 50（数据仅 5）
    assert meme_info_adapter.load_report(limit="bad")["count"] == 5  # 非整数回默认 10


def test_missing(report_file):
    data = meme_info_adapter.load_report()
    assert data["status"] == "missing"
    assert data["memes"] == [] and data["count"] == 0
    assert data["error"]


def test_invalid_json(report_file):
    _write(report_file, "{not-json")
    data = meme_info_adapter.load_report()
    assert data["status"] == "invalid"
    assert "JSONDecodeError" in data["error"] or data["error"]


@pytest.mark.parametrize(
    "payload,needle",
    [
        (make_report(schema_version="other/v1"), "schema_version"),
        (make_report(schema_version=None), "schema_version"),
        ({**make_report(), "memes": "nope"}, "memes"),
        ({**make_report(), "memes": [{"canonical": "x", "score": 1, "status": "hot"}]}, "family_key"),
        ({**make_report(), "memes": [{"family_key": "k", "score": 1, "status": "hot"}]}, "canonical"),
        ({**make_report(), "memes": [{"family_key": "k", "canonical": "x", "status": "hot"}]}, "score"),
        ({**make_report(), "memes": [{"family_key": "k", "canonical": "x", "score": 1}]}, "status"),
        ([], "JSON 对象"),
    ],
)
def test_invalid_contract(report_file, payload, needle):
    _write(report_file, payload)
    data = meme_info_adapter.load_report()
    assert data["status"] == "invalid"
    assert needle in data["error"]
    assert data["memes"] == []


def test_stale_keeps_data(report_file):
    old = time.time() - 2 * 3600
    _write(report_file, make_report(generated_at=_iso(old)))
    data = meme_info_adapter.load_report()
    assert data["status"] == "stale"
    assert data["age_seconds"] >= 3600
    assert data["count"] == 3 and data["memes"]


def test_fresh_boundary_is_ok(report_file):
    _write(report_file, make_report(generated_at=_iso(time.time() - 60)))
    assert meme_info_adapter.load_report()["status"] == "ok"


def test_cache_reuses_file_until_changed(report_file, monkeypatch):
    _write(report_file, make_report())
    calls: list[Path] = []
    original = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    first = meme_info_adapter.load_report()
    second = meme_info_adapter.load_report()
    assert first["generated_at"] == second["generated_at"]
    assert len(calls) == 1
    _write(report_file, make_report(memes=[make_meme(1, canonical="新梗")]))
    third = meme_info_adapter.load_report()
    assert third["memes"][0]["canonical"] == "新梗"
    assert len(calls) == 2


def test_bad_generated_at_falls_back_to_mtime(report_file):
    _write(report_file, make_report(generated_at="not-a-date"))
    data = meme_info_adapter.load_report()
    assert data["status"] == "ok"
    assert data["generated_at"] is None
    assert data["age_seconds"] >= 0


# ---------------------------------------------------------------- API 契约
@pytest.fixture
def client(monkeypatch, tmp_path):
    importlib.reload(api_module)
    with TestClient(api_module.app) as test_client:
        yield test_client


def test_api_meme_info_ok(client, report_file):
    _write(report_file, make_report())
    response = client.get("/api/meme-info")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["count"] == 3
    assert body["memes"][0]["canonical"] == "吃了不烧心"
    assert body["generated_at"]
    assert "verification" in body and "stats" in body
    assert "candidates" not in body


def test_api_meme_info_limit_and_only(client, report_file):
    _write(report_file, make_report())
    limited = client.get("/api/meme-info", params={"limit": 2}).json()
    assert limited["count"] == 2
    assert [m["rank"] for m in limited["memes"]] == [1, 2]
    clamped = client.get("/api/meme-info", params={"limit": 0}).json()
    assert clamped["count"] == 1
    only = client.get("/api/meme-info", params={"only": "publishable"}).json()
    assert only["count"] == 2
    assert all(m["publishable"] is True for m in only["memes"])


def test_api_meme_info_status_codes(client, report_file):
    # missing
    assert client.get("/api/meme-info").json()["status"] == "missing"
    # invalid：200 而不是 500
    _write(report_file, "broken")
    invalid = client.get("/api/meme-info")
    assert invalid.status_code == 200
    assert invalid.json()["status"] == "invalid"
    # stale
    _write(report_file, make_report(generated_at=_iso(time.time() - 7200)))
    stale = client.get("/api/meme-info")
    assert stale.status_code == 200
    assert stale.json()["status"] == "stale"
    assert stale.json()["count"] == 3
    # bad only → 400
    bad = client.get("/api/meme-info", params={"only": "bogus"})
    assert bad.status_code == 400


def test_legacy_top_and_history_removed(client, report_file):
    _write(report_file, make_report())
    assert client.get("/api/top").status_code == 404
    assert client.get("/api/meme/test/history").status_code == 404
    assert client.get("/api/meme-info").status_code == 200


# ---------------------------------------------------------------- 边界
def test_adapter_does_not_import_meme_info():
    code = (
        "import sys; import app.meme_info_adapter; "
        "bad = [m for m in sys.modules if m == 'meme_info' or m.startswith('meme_info.')]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parent.parent)


def test_shadow_tool_does_not_import_app_and_refuses_db(tmp_path):
    from tools import meme_info_shadow

    code = (
        "import sys; import tools.meme_info_shadow; "
        "bad = [m for m in sys.modules if m == 'app' or m.startswith('app.')]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parent.parent)
    with pytest.raises(SystemExit):
        meme_info_shadow._assert_safe_out(tmp_path / "bad.db")
    with pytest.raises(SystemExit):
        meme_info_shadow._assert_safe_out(Path("data/hotmeme.db"))


def test_shadow_tool_atomic_write(tmp_path):
    from tools import meme_info_shadow

    target = tmp_path / "sub" / "latest.json"
    size = meme_info_shadow._write_json_atomic(target, {"schema_version": "meme-info/v1"})
    assert target.is_file() and size > 0
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == "meme-info/v1"
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# ---------------------------------------------------------------- v2 契约（Phase E）
def make_entry_v2(rank: int = 1, *, key: str | None = None, canonical: str | None = None,
                  pool: str = "p2", publishable: bool = True, status: str = "normal",
                  score: float = 70.0, heat: float = 50.0, badge_2x2: bool = True,
                  cred: float | None = 66.5) -> dict:
    canonical = canonical or f"梗{rank}"
    pool_rank = rank if pool == "p2" else None
    return {
        "family_key": key or f"family_{rank}",
        "canonical": canonical,
        "aliases": [canonical, f"{canonical}别名"],
        "status": status,
        "pool": pool,
        "publishable": publishable,
        "score": score,
        "cred_score": cred,
        "heat_score": heat if pool == "p2" else None,
        "rank": pool_rank,
        "heat_components": {"comment": 10.0, "danmaku": 20.0, "video": 5.0,
                            "score": 15.0} if pool == "p2" else None,
        "count_total": 40,
        "comment_count": 10,
        "danmaku_count": 20,
        "video_count": 3,
        "usage_count": 30,
        "meta_count": 2,
        "explainer_only": False,
        "meta_only": False,
        "audit_state": "none",
        "audit_adjustment": 0.0,
        "badge_2x2": badge_2x2,
        "evidence_video_count": 3,
        # 报告里前 5 条；adapter 只透传前 2 条且截断
        "evidence": [
            {"quote": "引" * 300, "ctime": 1790000000 + i, "like": i, "bvid": f"BV{i}",
             "kind": "comment", "video_title": "标题"}
            for i in range(5)
        ],
        "first_seen_round": 1,
        "last_seen_round": 2,
        "reason": "p2:in" if pool == "p2" else "",
    }


def make_report_v2(*, generated_at: str | None = None,
                   report_status: str = "ok",
                   pool2: list[dict] | None = None,
                   pool1_only: list[dict] | None = None) -> dict:
    pool2_entries = pool2 if pool2 is not None else [
        make_entry_v2(1, canonical="吃了不烧心", heat=66.25, score=78.5),
        make_entry_v2(2, canonical="扎啤", heat=40.0, score=70.0, badge_2x2=False),
    ]
    p1_entries = pool1_only if pool1_only is not None else [
        make_entry_v2(9, key="family_p1", canonical="观察中", pool="p1",
                      publishable=False, status="provisional", score=35.0,
                      heat=None, cred=30.0),
    ]
    shadow = pool2_entries + p1_entries
    return {
        "schema_version": "meme-info/v2",
        "generated_at": _iso() if generated_at is None else generated_at,
        "status": report_status,
        "round": {"id": 29, "window_start": _iso(time.time() - 900),
                  "window_end": _iso()},
        "window": {"minutes": 15},
        "counts": {
            "pool1": len(shadow),
            "pool2": len(pool2_entries),
            "pool1_only": len(p1_entries),
            "pool1_out": 1,
            "observations": 4,
        },
        "stats": {
            "comment_count": 900,
            "danmaku_count": 500,
            "families": 20,
            "degraded": report_status == "degraded",
            "heat_model": "phase-e/window-heat-v1",
            "window_minutes": 15,
            "pool1": len(shadow),
            "pool2": len(pool2_entries),
            "round": 29,
        },
        "verification": {
            "configured": False,
            "calls": 0,
            "attempted": 0,
            "ok": 0,
            "rejected": 0,
            "errors": 0,
            "llm": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0},
            "model": "",
        },
        "memes": list(pool2_entries),
        "pool1": shadow,
        "pool2": pool2_entries,
    }


def test_v2_ok_contract(report_file):
    _write(report_file, make_report_v2())
    data = meme_info_adapter.load_report()
    assert data["status"] == "ok"                  # 新鲜度状态机不变
    assert data["schema_version"] == "meme-info/v2"
    assert data["report_status"] == "ok"
    assert data["degraded"] is False
    assert data["round"] == {
        "id": 29,
        "window_start": data["round"]["window_start"],
        "window_end": data["round"]["window_end"],
    }
    assert data["round"]["window_start"] and data["round"]["window_end"]
    assert data["window"] == {"minutes": 15}
    assert data["counts"]["pool1"] == 3 and data["counts"]["pool2"] == 2
    # only=all 默认返回 pool1 全量（shadow：含 pool2 成员）
    assert data["total_memes"] == 3
    assert data["publishable_count"] == 2
    assert data["count"] == 3
    assert data["age_seconds"] >= 0
    first = data["memes"][0]
    assert first["rank"] == 1 and first["heat_rank"] == 1
    assert first["family_key"] == "family_1"
    assert first["canonical"] == "吃了不烧心"
    assert first["pool"] == "p2" and first["publishable"] is True
    assert first["score"] == 78.5 and first["cred_score"] == 66.5
    assert first["heat_score"] == 66.25
    assert first["status"] == "normal"
    assert first["evidence_rate_2x2"] is True      # 2×2 只做徽标
    assert first["badge_2x2"] is True
    assert first["meta_count"] == 2 and first["usage_count"] == 30
    assert first["count_total"] == 40
    assert first["comment_count"] == 10 and first["danmaku_count"] == 20
    assert first["video_count"] == 3
    assert first["explainer_only"] is False and first["meta_only"] is False
    assert first["audit_state"] == "none" and first["audit_adjustment"] == 0.0
    assert first["evidence_video_count"] == 3
    assert first["first_seen_round"] == 1 and first["last_seen_round"] == 2
    assert first["verified"] is None               # 不伪造 LLM 结论
    assert first["type"] == "" and first["meaning"] == ""
    # evidence 只透传前 2 条且截断
    assert len(first["evidence"]) == 2
    assert set(first["evidence"][0]) == {"quote", "ctime", "like", "bvid"}
    assert len(first["evidence"][0]["quote"]) <= 120
    # stats / verification 精简
    assert data["stats"]["families"] == 20
    assert data["stats"]["comment_count"] == 900
    assert data["stats"]["heat_model"] == "phase-e/window-heat-v1"
    assert data["verification"]["configured"] is False
    assert "candidates" not in data
    assert len(json.dumps(data, ensure_ascii=False)) < 20_000


def test_v2_only_filters(report_file):
    _write(report_file, make_report_v2())
    publishable = meme_info_adapter.load_report(only="publishable")
    assert publishable["count"] == 2
    assert [m["family_key"] for m in publishable["memes"]] == ["family_1", "family_2"]
    assert all(m["publishable"] is True and m["pool"] == "p2" for m in publishable["memes"])
    # 全量口径不受 only 影响：总数看 pool1，可发布数看 pool2
    assert publishable["total_memes"] == 3
    assert publishable["publishable_count"] == 2

    shadow = meme_info_adapter.load_report(only="all")
    assert shadow["count"] == 3
    assert [m["pool"] for m in shadow["memes"]] == ["p2", "p2", "p1"]
    p1_entry = shadow["memes"][-1]
    assert p1_entry["family_key"] == "family_p1"
    assert p1_entry["publishable"] is False
    assert p1_entry["status"] == "provisional"
    assert p1_entry["heat_score"] is None and p1_entry["heat_rank"] is None
    # 池1候选的 rank 退回列表序号（前端编号连续），heat_rank 明确为空
    assert p1_entry["rank"] == 3


def test_v2_limit_clamp_and_rank(report_file):
    _write(report_file, make_report_v2())
    one = meme_info_adapter.load_report(limit=1)
    assert one["count"] == 1 and one["total_memes"] == 3
    assert meme_info_adapter.load_report(limit=0)["count"] == 1
    assert meme_info_adapter.load_report(limit=999)["count"] == 3
    assert meme_info_adapter.load_report(limit="bad")["count"] == 3
    with pytest.raises(ValueError):
        meme_info_adapter.load_report(only="bogus")


def test_v2_degraded_flag(report_file):
    _write(report_file, make_report_v2(report_status="degraded"))
    data = meme_info_adapter.load_report()
    assert data["status"] == "ok"                  # 新鲜度状态机保持
    assert data["report_status"] == "degraded"
    assert data["degraded"] is True
    # 保留 pool2、不改发布结论：adapter 只标记，不删条目
    assert data["count"] == 3
    assert data["publishable_count"] == 2
    assert data["stats"]["degraded"] is True


def test_v2_stale_and_degraded_together(report_file):
    old = _iso(time.time() - 2 * 3600)
    _write(report_file, make_report_v2(generated_at=old, report_status="degraded"))
    data = meme_info_adapter.load_report()
    assert data["status"] == "stale"
    assert data["degraded"] is True and data["report_status"] == "degraded"


def test_v2_missing_blank_contract(report_file):
    data = meme_info_adapter.load_report()
    assert data["status"] == "missing"
    assert data["schema_version"] == ""
    assert data["report_status"] is None
    assert data["degraded"] is False
    assert data["round"] == {"id": None, "window_start": None, "window_end": None}
    assert data["counts"] == {} and data["window"] == {}
    assert data["count"] == 0 and data["memes"] == []


@pytest.mark.parametrize(
    "payload,needle",
    [
        ({**make_report_v2(), "pool1": "nope"}, "pool1"),
        ({**make_report_v2(), "pool2": {}}, "pool2"),
        ({**make_report_v2(),
          "pool2": [{"canonical": "x", "score": 1, "status": "normal"}]}, "family_key"),
        ({**make_report_v2(),
          "pool1": [{"family_key": "k", "canonical": "x", "score": "bad",
                     "status": "normal"}]}, "score"),
        ({**make_report_v2(), "memes": []}, ""),          # 空 memes 合法（空榜）
    ],
)
def test_v2_validation(report_file, payload, needle):
    _write(report_file, payload)
    data = meme_info_adapter.load_report()
    if not needle:
        assert data["status"] == "ok"
        assert data["count"] == 3                      # only=all 走 pool1
        return
    assert data["status"] == "invalid"
    assert needle in data["error"]
    assert data["memes"] == []


def test_v2_verification_top_level(report_file):
    payload = make_report_v2()
    payload["verification"] = {
        "configured": True,
        "calls": 3,
        "attempted": 5,
        "ok": 4,
        "rejected": 1,
        "errors": 0,
        "audited_families": 2,
        "llm": {"calls": 3, "prompt_tokens": 100, "completion_tokens": 20,
                "total_tokens": 120},
        "model": "deepseek-chat",
    }
    _write(report_file, payload)
    verification = meme_info_adapter.load_report()["verification"]
    assert verification["configured"] is True
    assert verification["calls"] == 3
    assert verification["attempted"] == 5
    assert verification["ok"] == 4
    assert verification["rejected"] == 1
    assert verification["audited_families"] == 2
    assert verification["model"] == "deepseek-chat"
    assert verification["llm"]["total_tokens"] == 120


def test_api_meme_info_v2_pool_views(client, report_file):
    _write(report_file, make_report_v2())
    all_body = client.get("/api/meme-info", params={"only": "all", "limit": 10})
    assert all_body.status_code == 200
    assert all_body.json()["count"] == 3
    assert all_body.json()["total_memes"] == 3

    pub_body = client.get("/api/meme-info", params={"only": "publishable", "limit": 8})
    assert pub_body.status_code == 200
    body = pub_body.json()
    assert body["count"] == 2
    assert body["publishable_count"] == 2
    assert all(m["publishable"] is True for m in body["memes"])

    bad = client.get("/api/meme-info", params={"only": "bogus"})
    assert bad.status_code == 400


def test_v2_cache_uses_mtime(report_file):
    _write(report_file, make_report_v2())
    calls = []

    def counting_read_text(self, *args, **kwargs):
        calls.append(self)
        return Path.read_text(self, *args, **kwargs)

    original = Path.read_text

    def wrapped(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(Path, "read_text", wrapped):
        first = meme_info_adapter.load_report()
        second = meme_info_adapter.load_report()
    assert len(calls) == 1
    assert first["generated_at"] == second["generated_at"]
