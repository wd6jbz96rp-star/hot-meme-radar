"""Phase E 测试：报告 v2 生成 / heat 公式 / 排序 / 降级 / 原子写 / CLI / Postgres。

无 ``TEST_DATABASE_URL`` 时只跑纯单元测试；有则额外跑 Postgres 集成测试
（事务内执行并回滚，不污染生产数据）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meme_info.models import (
    FamilyObservation,
    FamilyRoundStat,
    Pool1Row,
    Pool2Row,
    PoolRoundResult,
    WindowComment,
    WindowDanmaku,
    WindowSample,
    WindowTarget,
)
from meme_info.pools import run_pool_round
from meme_info.report import (
    HEAT_ANCHORS,
    HEAT_WEIGHTS,
    SCHEMA_VERSION,
    build_report,
    compute_heat_score,
    heat_components,
    report_from_db,
    report_from_mapping,
    report_from_result,
    write_report,
)

DB_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not DB_URL, reason="需要 TEST_DATABASE_URL 才能跑 Postgres 集成测试"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000
WINDOW = 15 * 60


# ================================================================ 测试工具
def _p1(
    key: str,
    *,
    status: str = "normal",
    score: float = 70.0,
    count_now: int = 0,
    canonical: str | None = None,
    aliases: list[str] | None = None,
    meta_only: bool = False,
    explainer_only: bool = False,
    audit_state: str = "none",
    audit_adjustment: float = 0.0,
    evidence_json: dict | list | None = None,
    first_seen: int | None = 1,
    last_seen: int | None = 2,
) -> Pool1Row:
    return Pool1Row(
        family_key=key,
        canonical=canonical or key,
        aliases=aliases if aliases is not None else [key],
        status=status,
        score=score,
        count_prev=0,
        count_now=count_now,
        zero_streak=0,
        audit_state=audit_state,
        audit_adjustment=audit_adjustment,
        meta_only=meta_only,
        explainer_only=explainer_only,
        first_seen_round=first_seen,
        last_seen_round=last_seen,
        evidence_json=evidence_json if evidence_json is not None else {},
    )


def _p2(key: str, *, reason: str = "p2:in", explainer_only: bool = False) -> Pool2Row:
    return Pool2Row(
        family_key=key,
        round_id=2,
        heat_score=None,
        rank=None,
        reason=reason,
        explainer_only=explainer_only,
    )


def _stat(
    key: str,
    *,
    comment: int = 0,
    danmaku: int = 0,
    video: int = 0,
    meta: int = 0,
) -> FamilyRoundStat:
    return FamilyRoundStat(
        round_id=2,
        family_key=key,
        comment_count=comment,
        danmaku_count=danmaku,
        video_count=video,
        meta_count=meta,
        count_total=comment + danmaku + video,
        score_after=70.0,
        reason="new:ok",
    )


def _obs(
    key: str,
    *,
    comment: int = 0,
    danmaku: int = 0,
    video: int = 0,
    meta: int = 0,
    cred: float = 0.0,
) -> FamilyObservation:
    return FamilyObservation(
        family_key=key,
        canonical=key,
        norm=key,
        aliases=[key],
        present=True,
        discovered=True,
        comment_count=comment,
        danmaku_count=danmaku,
        video_count=video,
        meta_count=meta,
        count_total=comment + danmaku + video,
        usage_count=comment + danmaku,
        cred_base=cred,
    )


def _result(
    *,
    pool1: list[Pool1Row] | None = None,
    pool2: list[Pool2Row] | None = None,
    stats_rows: list[FamilyRoundStat] | None = None,
    observations: list[FamilyObservation] | None = None,
    status: str = "ok",
    degraded: bool = False,
    stats: dict | None = None,
    round_id: int = 2,
) -> PoolRoundResult:
    return PoolRoundResult(
        generated_at="",
        round_id=round_id,
        window_start=NOW - WINDOW,
        window_end=NOW,
        status=status,
        degraded=degraded,
        observations=observations or [],
        pool1=pool1 or [],
        pool2=pool2 or [],
        stats_rows=stats_rows or [],
        transitions=[],
        aliases=[],
        stats=stats or {},
    )


def _basic_result() -> PoolRoundResult:
    return _result(
        pool1=[
            _p1("爆梗A", score=82.0, count_now=40),
            _p1("潜力B", score=55.0, count_now=10),
            _p1("撤出C", status="out", score=20.0),
        ],
        pool2=[_p2("爆梗A")],
        stats_rows=[_stat("爆梗A", comment=18, danmaku=60, video=4, meta=1), _stat("潜力B", comment=2)],
        observations=[
            _obs("爆梗A", comment=18, danmaku=60, video=4, meta=1, cred=77.5),
            _obs("潜力B", comment=2, cred=48.25),
        ],
    )


# ================================================================ 1. 顶层契约
def test_v2_top_level_contract() -> None:
    report = report_from_result(_basic_result(), generated_at="2026-11-22T00:00:00+08:00")
    assert report["schema_version"] == SCHEMA_VERSION == "meme-info/v2"
    assert report["generated_at"] == "2026-11-22T00:00:00+08:00"
    assert report["status"] == "ok"
    assert report["round"]["id"] == 2
    assert report["round"]["window_start"] and report["round"]["window_end"]
    assert report["window"] == {"minutes": 15}
    assert report["counts"] == {
        "pool1": 2,
        "pool2": 1,
        "pool1_only": 1,
        "pool1_out": 1,
        "observations": 2,
    }
    stats = report["stats"]
    assert stats["degraded"] is False
    assert stats["families"] == 2
    assert stats["comment_count"] == 20           # 18 + 2（窗口去重评论汇总）
    assert stats["danmaku_count"] == 60
    assert stats["heat_model"] == "phase-e/window-heat-v1"
    assert stats["heat_weights"] == HEAT_WEIGHTS
    assert stats["heat_anchors"] == HEAT_ANCHORS
    assert abs(sum(HEAT_WEIGHTS.values()) - 1.0) < 1e-9
    verification = report["verification"]
    assert verification["calls"] == 0 and verification["attempted"] == 0
    assert verification["configured"] is False
    assert isinstance(verification["llm"], dict)

    # 单条字段齐全
    expected = {
        "family_key", "canonical", "aliases", "status", "pool", "publishable",
        "score", "cred_score", "heat_score", "rank", "heat_components",
        "count_total", "comment_count", "danmaku_count", "video_count",
        "usage_count", "meta_count", "explainer_only", "meta_only",
        "audit_state", "audit_adjustment", "badge_2x2", "evidence_video_count",
        "evidence", "first_seen_round", "last_seen_round", "reason",
    }
    assert expected <= set(report["memes"][0])

    # memes 与 pool2 是同一批（JSON 等价）
    assert report["memes"] == report["pool2"]
    assert [entry["canonical"] for entry in report["memes"]] == ["爆梗A"]


# ================================================================ 2. 排序 / 发布门
def test_pool2_only_sorted_by_heat_and_rank() -> None:
    pool1 = [
        _p1("A", score=80.0, count_now=500),
        _p1("B", score=90.0, count_now=100),
        _p1("C", score=60.0, count_now=5),
        _p1("D", score=75.0, count_now=0),
    ]
    stats_rows = [
        _stat("A", comment=1, danmaku=1, video=1),
        _stat("B", comment=20, danmaku=40, video=2),
        _stat("C", comment=0, danmaku=0, video=0),
    ]
    report = build_report(
        pool1=pool1,
        pool2=[_p2("A"), _p2("B"), _p2("C")],
        counts_rows=stats_rows,
        now=NOW,
    )
    memes = report["memes"]
    assert [entry["family_key"] for entry in memes] == ["B", "A", "C"]
    assert [entry["rank"] for entry in memes] == [1, 2, 3]
    assert all(entry["publishable"] for entry in memes)
    assert all(entry["pool"] == "p2" for entry in memes)
    assert [e["heat_score"] for e in memes] == sorted(
        (e["heat_score"] for e in memes), reverse=True
    )
    # 池1 非 pool2 条目：不进新梗榜，只进 shadow
    assert "D" not in {entry["family_key"] for entry in memes}
    shadow = report["pool1"]
    assert [entry["family_key"] for entry in shadow] == ["B", "A", "C", "D"]
    assert [entry["pool"] for entry in shadow] == ["p2", "p2", "p2", "p1"]
    assert [entry["rank"] for entry in shadow] == [1, 2, 3, None]
    assert [entry["heat_score"] for entry in shadow] == [
        report["memes"][0]["heat_score"],
        report["memes"][1]["heat_score"],
        report["memes"][2]["heat_score"],
        None,
    ]
    d = shadow[-1]
    assert d["publishable"] is False and d["score"] == 75.0
    # 2×2 不再是发布门：C 没徽标也可发布（属于 pool2）
    assert report["memes"][2]["badge_2x2"] is False


def test_heat_tiebreak_by_score() -> None:
    # A: comment 1 → 2.0 分贡献；score 70 → 10.5，合计 12.5
    # B: comment 0；score 83.333 → 12.49995 → 四舍五入 12.5
    report = build_report(
        pool1=[_p1("A", score=70.0), _p1("B", score=83.33)],
        pool2=[_p2("A"), _p2("B")],
        counts_rows=[_stat("A", comment=1), _stat("B")],
        now=NOW,
    )
    assert report["memes"][0]["heat_score"] == report["memes"][1]["heat_score"] == 12.5
    assert [entry["family_key"] for entry in report["memes"]] == ["B", "A"]
    assert [entry["rank"] for entry in report["memes"]] == [1, 2]


def test_out_rows_not_in_pool1_list() -> None:
    report = build_report(
        pool1=[
            _p1("正常", score=60.0),
            _p1("观察", status="provisional", score=32.0),
            _p1("撤出", status="out", score=10.0),
        ],
        now=NOW,
    )
    assert report["counts"]["pool1"] == 2
    assert report["counts"]["pool1_out"] == 1
    assert {entry["status"] for entry in report["pool1"]} == {"normal", "provisional"}
    assert all(entry["publishable"] is False for entry in report["pool1"])


# ================================================================ 3. heat 公式
def test_heat_formula_exact() -> None:
    # comment 10/20 → 0.5；danmaku 50/100 → 0.5；video 2/5 → 0.4；score 80/100
    components = heat_components(
        comment_count=10, danmaku_count=50, video_count=2, score=80.0
    )
    assert components == {
        "comment": 20.0,   # 100*0.40*0.5
        "danmaku": 15.0,   # 100*0.30*0.5
        "video": 6.0,      # 100*0.15*0.4
        "score": 12.0,     # 100*0.15*0.8
    }
    assert compute_heat_score(
        comment_count=10, danmaku_count=50, video_count=2, score=80.0
    ) == 53.0


def test_heat_saturates_and_clamps() -> None:
    assert compute_heat_score(
        comment_count=10_000, danmaku_count=10_000, video_count=10_000, score=100.0
    ) == 100.0
    assert compute_heat_score(
        comment_count=0, danmaku_count=0, video_count=0, score=0.0
    ) == 0.0
    assert compute_heat_score(
        comment_count=-5, danmaku_count=-5, video_count=-5, score=-30.0
    ) == 0.0
    assert compute_heat_score(
        comment_count=10_000, danmaku_count=0, video_count=0, score=0.0
    ) == 40.0  # 单评论饱和 = 权重 0.40


# ================================================================ 4. 降级 / explainer / 证据
def test_degraded_keeps_pool2_without_promotion() -> None:
    result = _basic_result()
    result.status = "degraded"
    result.degraded = True
    report = report_from_result(result)
    assert report["status"] == "degraded"
    assert report["stats"]["degraded"] is True
    # pool2 保留（不新增 promote）：潜力B 仍在池1，不进 memes
    assert [entry["family_key"] for entry in report["memes"]] == ["爆梗A"]
    assert report["counts"]["pool2"] == 1
    assert all(entry["publishable"] is True for entry in report["memes"])
    b = [entry for entry in report["pool1"] if entry["family_key"] == "潜力B"][0]
    assert b["publishable"] is False


def test_degraded_round_status_in_stats() -> None:
    report = build_report(
        pool1=[_p1("A")],
        pool2=[_p2("A")],
        round_status="degraded",
        now=NOW,
    )
    assert report["status"] == "degraded"
    assert report["stats"]["degraded"] is True


def test_explainer_only_and_evidence_badges() -> None:
    items = [
        {"doc_id": f"d{i}", "quote": f"引用{i}", "ctime": NOW - i, "like": i, "bvid": f"BV{i}"}
        for i in range(8)
    ]
    result = _result(
        pool1=[
            _p1(
                "梗百科",
                score=66.0,
                meta_only=True,
                explainer_only=True,
                audit_state="human_pass",
                audit_adjustment=15.0,
                evidence_json={
                    "items": items,
                    "badge_2x2": True,
                    "badge_docs": 3,
                    "badge_videos": 2,
                    "evidence_video_count": 6,
                    "updated_round": 2,
                },
            )
        ],
        pool2=[_p2("梗百科", reason="p2:in_explainer", explainer_only=True)],
        observations=[_obs("梗百科", meta=3, cred=66.6)],
    )
    entry = report_from_result(result)["memes"][0]
    assert entry["explainer_only"] is True
    assert entry["meta_only"] is True
    assert entry["audit_state"] == "human_pass"
    assert entry["audit_adjustment"] == 15.0
    assert entry["badge_2x2"] is True
    assert entry["evidence_video_count"] == 6
    assert len(entry["evidence"]) == 5          # 只留前 5 条
    assert entry["evidence"][0]["quote"] == "引用0"
    assert entry["cred_score"] == 66.6
    assert entry["reason"] == "p2:in_explainer"


def test_evidence_legacy_list_form() -> None:
    legacy = [{"quote": "旧格式", "ctime": NOW, "like": 3, "bvid": "BV1"}]
    report = build_report(
        pool1=[_p1("旧", evidence_json=legacy)],
        pool2=[_p2("旧")],
        now=NOW,
    )
    entry = report["memes"][0]
    assert entry["evidence"] == legacy
    assert entry["badge_2x2"] is False
    assert entry["evidence_video_count"] == 0
    assert entry["cred_score"] is None  # 无 observations：不伪造 cred


def test_cred_score_from_evidence_json_fallback() -> None:
    """DB 重建路径：cred_score 从 Phase D 落库的 evidence_json.cred_base 读取。"""
    legacy_evidence = {"items": [], "cred_base": 77.25}
    report = build_report(
        pool1=[_p1("DB梗", evidence_json=legacy_evidence)],
        pool2=[_p2("DB梗")],
        now=NOW,
    )
    assert report["memes"][0]["cred_score"] == 77.25
    # 内存 observations 的精确 cred_base 优先于 evidence_json
    report2 = build_report(
        pool1=[_p1("DB梗", evidence_json=legacy_evidence)],
        pool2=[_p2("DB梗")],
        observations=[_obs("DB梗", comment=3, cred=66.14)],
        now=NOW,
    )
    assert report2["memes"][0]["cred_score"] == 66.14
    # 旧数据没有 cred_base → null（不伪造）
    report3 = build_report(
        pool1=[_p1("旧梗", evidence_json={"items": []})],
        pool2=[_p2("旧梗")],
        now=NOW,
    )
    assert report3["memes"][0]["cred_score"] is None


def _phase_d_sample() -> WindowSample:
    """真实 Phase D 输入：40 评论 / 200 弹幕 / 3 bvid + 元话语目标（应生成 pool2）。"""
    bvids = ["BV1E", "BV2E", "BV3E"]
    targets = [
        WindowTarget(bvid=bvid, title="尊嘟假嘟是什么梗", zone="鬼畜")
        for bvid in bvids
    ]
    return WindowSample(
        window_start=NOW - WINDOW,
        window_end=NOW,
        status="ok",
        comments=[
            WindowComment(id=f"c{i}", bvid=bvids[i % 3], text="尊嘟假嘟太好笑了",
                          ctime=NOW - 60 - i, like=i, zone="鬼畜",
                          video_title="热门视频")
            for i in range(40)
        ],
        danmaku=[
            WindowDanmaku(id=f"d{i}", dmid=f"dm{i}", bvid=bvids[i % 3],
                          text="尊嘟假嘟", ctime=NOW - 30 - i, zone="鬼畜",
                          video_title="热门视频")
            for i in range(200)
        ],
        evidence_comments=[
            WindowComment(id=f"e{i}", bvid=bvids[i % 3], text="尊嘟假嘟",
                          ctime=NOW - 300 - i, like=5, zone="鬼畜",
                          video_title="热门视频")
            for i in range(10)
        ],
        comment_targets=targets,
        danmaku_targets=targets,
    )


def test_verification_does_not_fake_calls() -> None:
    report = report_from_result(_basic_result())
    assert report["verification"]["calls"] == 0
    assert report["verification"]["attempted"] == 0
    assert report["verification"]["configured"] is False


def test_verification_configured_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("MEME_INFO_VERIFY", raising=False)
    monkeypatch.delenv("MEME_INFO_LLM_API_KEY", raising=False)
    monkeypatch.delenv("HOTMEME_LLM_API_KEY", raising=False)
    monkeypatch.delenv("MEME_INFO_LLM_API_KEY_FILE", raising=False)
    monkeypatch.delenv("HOTMEME_LLM_API_KEY_FILE", raising=False)
    assert report_from_result(_basic_result())["verification"]["configured"] is False
    monkeypatch.setenv("MEME_INFO_VERIFY", "1")
    monkeypatch.setenv("MEME_INFO_LLM_API_KEY", "test-key")
    report = report_from_result(_basic_result())
    assert report["verification"]["configured"] is True
    assert report["verification"]["calls"] == 0  # 只是「可用」，不伪造成已调用


def test_explicit_verification_passthrough() -> None:
    report = build_report(
        pool1=[_p1("A")],
        pool2=[_p2("A")],
        verification={
            "configured": True,
            "calls": 3,
            "attempted": 5,
            "ok": 4,
            "rejected": 1,
            "errors": 0,
            "model": "deepseek-chat",
            "llm": {"calls": 3, "prompt_tokens": 10, "completion_tokens": 5,
                    "total_tokens": 15},
        },
        now=NOW,
    )
    verification = report["verification"]
    assert verification["calls"] == 3
    assert verification["attempted"] == 5
    assert verification["model"] == "deepseek-chat"
    assert verification["llm"]["total_tokens"] == 15


# ================================================================ 5. 空池 / replay / 原子写
def test_empty_pool_report() -> None:
    report = build_report(now=NOW)
    assert report["status"] == "ok"
    assert report["memes"] == [] and report["pool1"] == [] and report["pool2"] == []
    assert report["counts"]["pool1"] == 0 and report["counts"]["pool2"] == 0
    assert report["stats"]["comment_count"] == 0
    assert report["stats"]["families"] == 0


def test_report_from_mapping_matches_result() -> None:
    result = _basic_result()
    direct = report_from_result(result, generated_at="2026-11-22T00:00:00+08:00")
    replay = report_from_mapping(
        result.to_dict(), generated_at="2026-11-22T00:00:00+08:00"
    )
    assert replay["schema_version"] == direct["schema_version"]
    assert replay["status"] == direct["status"]
    assert replay["counts"] == direct["counts"]
    assert replay["stats"] == direct["stats"]
    assert replay["memes"] == direct["memes"]  # cred_score 也来自 observations
    assert replay["pool1"] == direct["pool1"]
    assert replay["round"] == direct["round"]


def test_report_from_mapping_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        report_from_mapping([1, 2, 3])


def test_atomic_write_success(tmp_path) -> None:
    target = tmp_path / "sub" / "report_v2.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"old": true}', encoding="utf-8")
    report = report_from_result(_basic_result())
    size = write_report(report, target)
    assert size > 0 and target.is_file()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == "meme-info/v2"
    assert loaded["memes"][0]["family_key"] == "爆梗A"
    assert [p.name for p in target.parent.iterdir() if ".tmp-" in p.name] == []


def test_atomic_write_failure_keeps_old_report(tmp_path) -> None:
    target = tmp_path / "report_v2.json"
    target.write_text('{"old": true}', encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(TypeError):
        write_report({"schema_version": "meme-info/v2", "bad": {1, 2, 3}}, target)
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name] == []


def test_write_report_creates_parent_dir(tmp_path) -> None:
    target = tmp_path / "a" / "b" / "c.json"
    write_report({"schema_version": "meme-info/v2"}, target)
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == "meme-info/v2"


# ================================================================ 6. CLI
def _run_cli(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tools.build_report", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "HOTMEME_OFFLINE": "1"},
        **kwargs,
    )


def test_cli_no_db_roundtrip(tmp_path) -> None:
    pool_json = tmp_path / "pool_round.json"
    pool_json.write_text(
        json.dumps(_basic_result().to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    out = tmp_path / "report_v2.json"
    proc = _run_cli(
        [
            "--no-db",
            "--pool-json", str(pool_json),
            "--out", str(out),
            "--json",
            "--now", str(NOW),
            "--generated-at", "2026-11-22T00:00:00+08:00",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    printed = json.loads(proc.stdout)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert printed == written
    assert written["schema_version"] == "meme-info/v2"
    assert written["generated_at"] == "2026-11-22T00:00:00+08:00"
    assert written["counts"]["pool2"] == 1
    assert [entry["rank"] for entry in written["memes"]] == [1]


def test_cli_no_db_requires_pool_json() -> None:
    proc = _run_cli(["--no-db"])
    assert proc.returncode == 2
    assert "--pool-json" in proc.stderr


def test_cli_rejects_pool_json_without_no_db(tmp_path) -> None:
    path = tmp_path / "x.json"
    path.write_text("{}", encoding="utf-8")
    proc = _run_cli(["--pool-json", str(path)])
    assert proc.returncode == 2
    assert "--no-db" in proc.stderr


def test_cli_quiet_summary(tmp_path) -> None:
    pool_json = tmp_path / "pool.json"
    pool_json.write_text(json.dumps(_basic_result().to_dict()), encoding="utf-8")
    proc = _run_cli(["--no-db", "--pool-json", str(pool_json), "--quiet", "--now", str(NOW)])
    assert proc.returncode == 0, proc.stderr
    assert "pool2=1" in proc.stdout and "memes=1" in proc.stdout


def test_report_module_does_not_import_app() -> None:
    code = (
        "import sys; import meme_info.report; "
        "bad = [m for m in sys.modules if m == 'app' or m.startswith('app.')]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=REPO_ROOT)


# ================================================================ 7. Postgres 集成
class _Rollback(Exception):
    """用异常让 state.transaction 回滚，保证测试不污染生产数据。"""


def _db_fixture_round(conn, *, status: str = "ok") -> int:
    return int(
        conn.execute(
            "INSERT INTO round (started_at, window_start, window_end, status, "
            "comments_sampled, danmaku_sampled, stats) "
            "VALUES (to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s::jsonb) "
            "RETURNING id",
            (NOW, NOW - WINDOW, NOW, status, 900, 500, json.dumps({"phase_c": {"x": 1}})),
        ).fetchone()[0]
    )


def _db_insert_pool1(conn, row: Pool1Row, *, round_id: int) -> None:
    conn.execute(
        "INSERT INTO pool1 (family_key, canonical, aliases_json, status, score, "
        "count_prev, count_now, zero_streak, audit_state, audit_hash, audit_adjustment, "
        "meta_only, explainer_only, first_seen_round, last_seen_round, evidence_json) "
        "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            row.family_key,
            row.canonical,
            json.dumps(row.aliases, ensure_ascii=False),
            row.status,
            float(row.score),
            int(row.count_prev),
            int(row.count_now),
            int(row.zero_streak),
            row.audit_state,
            row.audit_hash or None,
            float(row.audit_adjustment or 0.0),
            bool(row.meta_only),
            bool(row.explainer_only),
            row.first_seen_round,
            round_id,
            json.dumps(row.evidence_json, ensure_ascii=False),
        ),
    )


def _db_insert_pool2(conn, row: Pool2Row, *, round_id: int) -> None:
    conn.execute(
        "INSERT INTO pool2 (family_key, round_id, heat_score, rank, reason, explainer_only) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            row.family_key,
            round_id,
            row.heat_score,
            row.rank,
            row.reason,
            bool(row.explainer_only),
        ),
    )


def _db_insert_stat(conn, stat: FamilyRoundStat, *, round_id: int) -> None:
    conn.execute(
        "INSERT INTO family_round_stat (round_id, family_key, comment_count, "
        "danmaku_count, video_count, meta_count, count_total, score_after, reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            round_id,
            stat.family_key,
            stat.comment_count,
            stat.danmaku_count,
            stat.video_count,
            stat.meta_count,
            stat.count_total,
            stat.score_after,
            stat.reason,
        ),
    )


def _db_tables() -> tuple[str, ...]:
    return (
        "round",
        "pool1",
        "pool2",
        "family_round_stat",
        "family_alias",
        "pool_transition",
        "audit_event",
    )


def _db_counts(conn) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in _db_tables()
    }


@requires_db
def test_report_from_db_postgres_rollback() -> None:
    """事务内插入 round/pool1/pool2/stat/audit_event → 生成报告 → 回滚 0 残留。"""
    from meme_info import state

    evidence = {
        "items": [
            {"quote": "梗A好活", "ctime": NOW - 10, "like": 5, "bvid": "BV1"},
            {"quote": "梗A笑死", "ctime": NOW - 20, "like": 2, "bvid": "BV2"},
        ],
        "badge_2x2": True,
        "evidence_video_count": 4,
        "cred_base": 83.6,
        "updated_round": None,
    }
    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            before = _db_counts(conn)
            round_id = _db_fixture_round(conn)
            evidence["updated_round"] = round_id
            pool1_rows = [
                _p1("梗A", score=78.5, count_now=80, evidence_json=evidence,
                    aliases=["梗A", "梗A别名"]),
                _p1("梗B", score=52.0, count_now=6),
                _p1("撤出C", status="out", score=12.0),
            ]
            for row in pool1_rows:
                _db_insert_pool1(conn, row, round_id=round_id)
            _db_insert_pool2(conn, _p2("梗A"), round_id=round_id)
            for stat in (
                _stat("梗A", comment=18, danmaku=60, video=4, meta=1),
                _stat("梗B", comment=2, danmaku=1, video=1),
            ):
                _db_insert_stat(conn, stat, round_id=round_id)
            conn.execute(
                "INSERT INTO audit_event (family_key, round_id, kind, verdict, delta, "
                "confidence, reason, model, prompt_version, evidence_hash) "
                "VALUES (%s, %s, 'llm', 'pass', 8, 0.9, 'ok', 'deepseek-chat', 'v4', 'h1')",
                ("梗A", round_id),
            )

            report = report_from_db(conn, round_id=round_id, now=NOW)
            assert report["schema_version"] == "meme-info/v2"
            assert report["status"] == "ok"
            assert report["round"]["id"] == round_id
            assert report["round"]["window_start"] and report["round"]["window_end"]
            # 计数：round.comments_sampled / danmaku_sampled + stat 行数
            assert report["stats"]["comment_count"] == 900
            assert report["stats"]["danmaku_count"] == 500
            assert report["stats"]["families"] == 2
            # 池成员：pool1 全量（不含 out），pool2 只有梗A
            assert report["counts"] == {
                "pool1": 2,
                "pool2": 1,
                "pool1_only": 1,
                "pool1_out": 1,
                "observations": 0,
            }
            meme = report["memes"][0]
            assert meme["family_key"] == "梗A"
            assert meme["canonical"] == "梗A"
            assert meme["aliases"] == ["梗A", "梗A别名"]
            assert meme["rank"] == 1 and meme["publishable"] is True
            assert meme["pool"] == "p2"
            assert meme["count_total"] == 82  # 18 + 60 + 4
            assert meme["comment_count"] == 18 and meme["danmaku_count"] == 60
            assert meme["video_count"] == 4 and meme["meta_count"] == 1
            assert meme["heat_score"] == compute_heat_score(
                comment_count=18, danmaku_count=60, video_count=4, score=78.5
            )
            # Phase E 落库：DB 重建从 evidence_json.cred_base 读取，不再为 null
            assert meme["cred_score"] == 83.6
            assert meme["badge_2x2"] is True
            assert meme["evidence_video_count"] == 4
            assert len(meme["evidence"]) == 2
            assert meme["first_seen_round"] == 1
            shadow = {entry["family_key"]: entry for entry in report["pool1"]}
            assert set(shadow) == {"梗A", "梗B"}
            assert shadow["梗A"]["pool"] == "p2" and shadow["梗A"]["rank"] == 1
            assert shadow["梗B"]["pool"] == "p1" and shadow["梗B"]["rank"] is None
            assert shadow["梗B"]["publishable"] is False
            # 旧数据无 cred_base 时回退 null（兼容，不伪造）
            assert shadow["梗B"]["cred_score"] is None
            assert shadow["梗A"]["cred_score"] == 83.6
            # verification 从 audit_event 聚合，不伪造
            verification = report["verification"]
            assert verification["calls"] == 1
            assert verification["attempted"] == 1
            assert verification["ok"] == 1
            assert verification["rejected"] == 0
            assert verification["model"] == "deepseek-chat"
            # 只读：不写 audit_event、不改 round
            assert conn.execute(
                "SELECT count(*) FROM audit_event WHERE family_key = '梗A'"
            ).fetchone()[0] == 1
            raise _Rollback

    with state.connection(DB_URL) as conn:
        assert _db_counts(conn) == before, "测试行必须全部回滚"


@requires_db
def test_report_from_db_cred_score_after_phase_d_round() -> None:
    """Phase D 真实写库一轮 → 报告 v2 的 pool1/pool2 条目 cred_score 非空且等于 observation。"""
    from meme_info import state

    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            before = _db_counts(conn)
            round_id = _db_fixture_round(conn)
            result = run_pool_round(
                _phase_d_sample(),
                use_db=True,
                db_conn=conn,
                round_id=round_id,
                now=NOW,
            )
            assert result.pool2, "强样本应至少 1 条进 pool2"
            report = report_from_db(conn, round_id=round_id, now=NOW)
            observations = {item.family_key: item for item in result.observations}
            assert report["memes"], "新梗榜必须非空"
            checked = report["memes"] + report["pool1"]
            assert checked
            for entry in checked:
                assert entry["cred_score"] is not None, entry["family_key"]
                expected = observations[entry["family_key"]].cred_base
                assert entry["cred_score"] == pytest.approx(expected, abs=0.01)
            # DB 里 Phase D 新写的 evidence_json 必须带 cred_base
            rows = conn.execute(
                "SELECT family_key, evidence_json FROM pool1"
            ).fetchall()
            assert rows
            for key, evidence in rows:
                assert isinstance(evidence, dict), key
                assert isinstance(evidence.get("cred_base"), (int, float)), key
            raise _Rollback

    with state.connection(DB_URL) as conn:
        assert _db_counts(conn) == before


@requires_db
def test_report_from_db_degraded_round_keeps_pool2() -> None:
    from meme_info import state

    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            before = _db_counts(conn)
            round_id = _db_fixture_round(conn, status="degraded")
            _db_insert_pool1(conn, _p1("梗A", score=78.0), round_id=round_id)
            _db_insert_pool1(conn, _p1("梗B", score=55.0), round_id=round_id)
            _db_insert_pool2(conn, _p2("梗A"), round_id=round_id)
            report = report_from_db(conn, round_id=round_id, now=NOW)
            assert report["status"] == "degraded"
            assert report["stats"]["degraded"] is True
            # 保留 pool2、不新增 promote
            assert [entry["family_key"] for entry in report["memes"]] == ["梗A"]
            assert report["counts"]["pool2"] == 1
            assert all(entry["publishable"] is True for entry in report["memes"])
            # 无 stat 行时 families 退回活跃池1行数（不伪造 0）
            assert report["stats"]["families"] == 2
            raise _Rollback

    with state.connection(DB_URL) as conn:
        assert _db_counts(conn) == before


@requires_db
def test_report_from_db_latest_and_missing_round() -> None:
    from meme_info import state

    with pytest.raises(_Rollback):
        with state.transaction(DB_URL) as conn:
            before = _db_counts(conn)
            round_id = _db_fixture_round(conn)
            conn.execute(
                "INSERT INTO pool1 (family_key, canonical, status, score) "
                "VALUES ('只读梗', '只读梗', 'normal', 66)"
            )
            conn.execute(
                "INSERT INTO pool2 (family_key, round_id, reason) "
                "VALUES ('只读梗', %s, 'p2:in')",
                (round_id,),
            )
            latest = report_from_db(conn, now=NOW)  # latest
            assert latest["round"]["id"] == round_id
            assert [entry["family_key"] for entry in latest["memes"]] == ["只读梗"]
            with pytest.raises(ValueError):
                report_from_db(conn, round_id=999_999_999)
            raise _Rollback

    with state.connection(DB_URL) as conn:
        assert _db_counts(conn) == before
