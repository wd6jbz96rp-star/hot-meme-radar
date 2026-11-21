"""Phase F 审核测试：加减分替换 / evidence_hash / 队列 / 池同步 / 人工覆盖 LLM。

无 ``TEST_DATABASE_URL`` 时只跑纯单元测试；有则额外跑 Postgres 集成测试
（全部在事务内执行并回滚，不污染生产数据）：

- ``TEST_DATABASE_URL``：admin/worker 运行时连接串。
- 云上验证示例（服务器上，密码不进命令行）::

      sudo bash -c 'set -a; . /etc/hotmeme/env; set +a;
        TEST_DATABASE_URL=$ADMIN_DATABASE_URL pytest -q tests/test_audit.py'
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from meme_info import audit
from meme_info.models import Pool1Row
from meme_info.verify import Verification

DB_URL = os.environ.get("TEST_DATABASE_URL")
MIGRATE_URL = os.environ.get("TEST_MIGRATE_DATABASE_URL") or ""
requires_db = pytest.mark.skipif(
    not DB_URL, reason="需要 TEST_DATABASE_URL 才能跑 Postgres 集成测试"
)
REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = int(time.time())


# ================================================================ 测试工具
def _evidence(items: int = 1, *, quote_prefix: str = "用起来很顺") -> dict:
    rows = [
        {
            "doc_id": f"doc-{index}",
            "bvid": f"BV{index:03d}",
            "kind": "comment",
            "quote": f"{quote_prefix}{index}",
            "ctime": NOW - 60,
            "like": 3,
            "zone": "鬼畜",
            "video_title": f"标题{index}",
        }
        for index in range(items)
    ]
    badge_2x2 = items >= 2
    return {
        "items": rows,
        "badge_2x2": badge_2x2,
        "badge_docs": items,
        "badge_videos": items,
        "evidence_video_count": items,
        "cred_base": 62.0,
        "updated_round": 1,
    }


def _row(
    key: str = "梗A",
    *,
    score: float = 50.0,
    status: str = "normal",
    count_now: int = 3,
    audit_state: str = "none",
    audit_hash: str = "",
    audit_adjustment: float = 0.0,
    explainer_only: bool = False,
    meta_only: bool = False,
    evidence: dict | None = None,
    canonical: str | None = None,
    aliases: list[str] | None = None,
) -> Pool1Row:
    return Pool1Row(
        family_key=key,
        canonical=canonical or key,
        aliases=list(aliases or []),
        status=status,
        score=score,
        count_now=count_now,
        audit_state=audit_state,
        audit_hash=audit_hash,
        audit_adjustment=audit_adjustment,
        explainer_only=explainer_only,
        meta_only=meta_only,
        evidence_json=_evidence() if evidence is None else evidence,
    )


def _apply_update(row: Pool1Row, kind: str, verdict: str, *, in_pool2: bool = False):
    update = audit.compute_audit_update(row, kind=kind, verdict=verdict, in_pool2=in_pool2)
    row.score = update.score_after
    row.audit_adjustment = update.new_adjustment
    row.audit_state = audit.audit_state_of(kind, verdict)
    row.status = update.status_after
    row.explainer_only = update.explainer_only
    return update


# ================================================================ 1. 规则常量
def test_delta_tables_match_spec() -> None:
    assert audit.LLM_DELTAS == {"pass": 8.0, "fail": -15.0, "unclear": 0.0}
    assert audit.HUMAN_DELTAS == {
        "pass": 15.0,
        "fail": -25.0,
        "unclear": 0.0,
        "merge": 0.0,
    }
    assert audit.verdict_adjustment("llm", "pass") == 8.0
    assert audit.verdict_adjustment("llm", "fail") == -15.0
    assert audit.verdict_adjustment("human", "pass") == 15.0
    assert audit.verdict_adjustment("human", "fail") == -25.0
    # merge 的事件 delta 记 0，实际重算由 record_audit(merge) 处理
    assert audit.verdict_adjustment("human", "merge") == 0.0
    with pytest.raises(audit.AuditValidationError):
        audit.verdict_adjustment("robot", "pass")
    with pytest.raises(audit.AuditValidationError):
        audit.verdict_adjustment("human", "passs")


def test_action_map_and_audit_state() -> None:
    assert audit.ACTION_MAP["approve"] == ("human", "pass", "human_pass")
    assert audit.ACTION_MAP["reject"] == ("human", "fail", "human_fail")
    assert audit.ACTION_MAP["merge"] == ("human", "merge", "human_merge")
    assert audit.ACTION_MAP["need_more"] == ("human", "unclear", "human_need_more")
    assert audit.audit_state_of("llm", "unclear") == "llm_unclear"
    assert audit.audit_state_of("human", "merge") == "human_merge"
    with pytest.raises(audit.AuditValidationError):
        audit.audit_state_of("human", "passs")


# ================================================================ 2. evidence_hash
def test_evidence_hash_stable_and_sensitive() -> None:
    row = _row("梗A")
    first = audit.family_evidence_hash(row)
    assert first == audit.family_evidence_hash(_row("梗A"))
    # 证据变化 → 新 hash（允许重新审核）
    changed = _row("梗A")
    changed.evidence_json["items"][0]["quote"] = "完全不同的证据"
    assert audit.family_evidence_hash(changed) != first
    # family 身份变化 → 新 hash
    assert audit.family_evidence_hash(_row("梗A", canonical="梗B")) != first
    # 计数/分数变化不改变 hash（不因每轮计数抖动触发 LLM 重审）
    scored = _row("梗A", score=93.0, count_now=999)
    assert audit.family_evidence_hash(scored) == first
    assert len(first) == 32


def test_evidence_is_strong() -> None:
    assert audit.evidence_is_strong(_row("甲")) is False  # 单条证据
    strong = _row("乙", evidence=_evidence(2))
    assert audit.evidence_is_strong(strong) is True
    assert audit.evidence_is_strong(_row("丙", meta_only=True)) is True
    assert audit.evidence_is_strong(_row("丁", explainer_only=True)) is True


# ================================================================ 3. 分数替换（最新不累加）
def test_score_replacement_llm_pass_fail_human_pass() -> None:
    """+8 → -25 → +15：最新替换，最终只保留 +15（不是 +8-25+15）。"""
    row = _row("梗A", score=50.0)
    _apply_update(row, "llm", "pass")
    assert row.score == pytest.approx(58.0)
    assert row.audit_adjustment == 8.0
    assert row.audit_state == "llm_pass"

    _apply_update(row, "human", "fail")
    assert row.score == pytest.approx(25.0)  # 50 - 25
    assert row.audit_adjustment == -25.0
    assert row.audit_state == "human_fail"

    _apply_update(row, "human", "pass")
    assert row.score == pytest.approx(65.0)  # 25 - (-25) + 15 = 50 + 15
    assert row.audit_adjustment == 15.0


def test_human_overrides_llm_same_evidence() -> None:
    row = _row("梗A", score=50.0)
    _apply_update(row, "llm", "fail")
    assert row.score == pytest.approx(35.0)
    assert row.audit_adjustment == -15.0
    _apply_update(row, "human", "pass")
    assert row.score == pytest.approx(65.0)  # 35 + 15 + 15
    assert row.audit_adjustment == 15.0
    assert row.audit_state == "human_pass"


def test_unclear_and_need_more_are_zero_delta() -> None:
    row = _row("梗A", score=50.0, audit_adjustment=0.0)
    update = _apply_update(row, "llm", "unclear")
    assert update.delta == 0.0 and row.score == pytest.approx(50.0)
    update = _apply_update(row, "human", "unclear")
    assert update.delta == 0.0 and row.score == pytest.approx(50.0)
    assert row.audit_state == "human_need_more"


def test_score_is_clamped() -> None:
    row = _row("梗A", score=5.0)
    update = _apply_update(row, "human", "fail")
    assert update.score_after == 0.0
    high = _row("梗B", score=98.0)
    update = _apply_update(high, "human", "pass")
    assert update.score_after == 100.0


# ================================================================ 4. 池同步（纯计算）
def test_pool2_insert_on_audit_pass() -> None:
    row = _row("梗A", score=60.0, count_now=4)
    update = audit.compute_audit_update(
        row, kind="human", verdict="pass", in_pool2=False
    )
    assert update.score_after == pytest.approx(75.0)
    assert update.pool2_action == "insert"


def test_pool2_removed_when_score_below_55() -> None:
    row = _row("梗A", score=60.0)
    update = audit.compute_audit_update(
        row, kind="llm", verdict="fail", in_pool2=True
    )
    assert update.score_after == pytest.approx(45.0)
    assert update.pool2_action == "remove"


def test_audit_fail_removes_pool2_even_if_score_high() -> None:
    # 模式 B：fail 是否决，即使分数仍 >=55 也撤出池2
    row = _row("梗A", score=90.0)
    update = audit.compute_audit_update(
        row, kind="human", verdict="fail", in_pool2=True
    )
    assert update.score_after == pytest.approx(65.0)
    assert update.pool2_action == "remove"
    assert "p2:out_audit_fail" in update.reasons


def test_llm_pass_can_insert_pool2_only_with_usage() -> None:
    strong = _row("梗A", score=60.0, count_now=5)
    update = audit.compute_audit_update(strong, kind="llm", verdict="pass")
    assert update.score_after == pytest.approx(68.0)
    assert update.pool2_action == "insert"
    no_usage = _row("梗B", score=70.0, count_now=0)
    update = audit.compute_audit_update(no_usage, kind="llm", verdict="pass")
    assert update.pool2_action == "keep"  # 无使用不进池2


def test_p1_out_and_reentry() -> None:
    row = _row("梗A", score=40.0)
    update = audit.compute_audit_update(row, kind="human", verdict="fail")
    assert update.score_after == pytest.approx(15.0)
    assert update.status_after == "out"
    assert any(item["reason"] == "p1:audit_out" for item in update.transitions)
    out = _row("梗B", score=40.0, status="out")
    update = audit.compute_audit_update(out, kind="human", verdict="pass")
    assert update.score_after == pytest.approx(55.0)
    assert update.status_after == "normal"
    assert any(item["reason"] == "p1:audit_reentry" for item in update.transitions)


def test_provisional_becomes_normal_on_human_pass() -> None:
    row = _row("梗A", score=30.0, status="provisional", meta_only=True)
    update = audit.compute_audit_update(row, kind="human", verdict="pass")
    assert update.score_after == pytest.approx(45.0)
    assert update.status_after == "normal"


def test_explainer_only_rules_follow_phase_d() -> None:
    # 已在池2的 explainer-only：低分不立即淘汰
    row = _row("梗A", score=40.0, explainer_only=True, meta_only=True)
    update = audit.compute_audit_update(
        row, kind="human", verdict="unclear", in_pool2=True
    )
    assert update.pool2_action == "keep"
    # explainer-only 无使用不进池2
    not_in = _row("梗B", score=60.0, explainer_only=True)
    update = audit.compute_audit_update(
        not_in, kind="human", verdict="pass", in_pool2=False
    )
    assert update.pool2_action == "keep"
    # fail 清空 explainer 标记并撤出
    fail = _row("梗C", score=50.0, explainer_only=True)
    update = audit.compute_audit_update(fail, kind="human", verdict="fail", in_pool2=True)
    assert update.explainer_only is False
    assert update.pool2_action == "remove"


# ================================================================ 5. 队列优先级
def test_rank_queue_priority_order() -> None:
    rows = [
        _row("接近池2", score=62.0),  # P1
        _row("LLM不确定", score=20.0, audit_state="llm_unclear", evidence=_evidence(2)),  # P2（强证据）
        _row("explainer", score=20.0, explainer_only=True),  # P3
    ]
    pool2 = {"长期池2"}
    rows.append(_row("长期池2", score=80.0))
    old_event = {"created_at": time.strftime(
        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 30 * 86400)
    )}
    queue = audit.rank_audit_queue(
        rows,
        pool2_keys=pool2,
        latest_events={"长期池2": old_event},
        now=NOW,
    )
    keys = [item.family_key for item in queue]
    assert keys[0] == "接近池2"
    assert queue[0].priority == audit.PRIORITY_NEAR_P2
    assert "LLM不确定" in keys and queue[keys.index("LLM不确定")].priority == audit.PRIORITY_LLM_STRONG
    assert "explainer" in keys and queue[keys.index("explainer")].priority == audit.PRIORITY_EXPLAINER
    assert "长期池2" in keys and keys.index("长期池2") > keys.index("explainer")


def test_rank_queue_skips_already_reviewed_and_empty_evidence() -> None:
    reviewed = _row("已人工通过", audit_state="human_pass")
    reviewed.audit_hash = audit.family_evidence_hash(reviewed)
    empty = _row("无证据", evidence={"items": [], "badge_2x2": False})
    llm_passed = _row("LLM已过", audit_state="llm_pass")
    llm_passed.audit_hash = audit.family_evidence_hash(llm_passed)
    queue = audit.rank_audit_queue([reviewed, empty, llm_passed], now=NOW)
    assert queue == []


def test_rank_queue_reenters_after_evidence_changes() -> None:
    row = _row("梗A", audit_state="human_fail")
    row.audit_hash = "stale-hash"
    queue = audit.rank_audit_queue([row], now=NOW)
    assert [item.family_key for item in queue] == ["梗A"]


def test_rank_queue_limit() -> None:
    rows = [_row(f"梗{index}", score=60.0) for index in range(10)]
    assert len(audit.rank_audit_queue(rows, now=NOW, limit=3)) == 3
    assert audit.rank_audit_queue(rows, now=NOW, limit=0) == []
    assert audit.rank_audit_queue(rows, now=NOW, limit=-5) == []


# ================================================================ 6. LLM 包装
def test_verifier_verdict_mapping() -> None:
    assert audit._verifier_verdict(
        Verification(is_meme=True, is_current=True, heat=50, confidence=0.8, reason="")
    )[0] == "pass"
    assert audit._verifier_verdict(
        Verification(is_meme=False, is_current=True, heat=0, confidence=0.9, reason="")
    )[0] == "fail"
    assert audit._verifier_verdict(
        Verification(is_meme=True, is_current=False, heat=0, confidence=0.9, reason="")
    )[0] == "fail"
    assert audit._verifier_verdict(
        Verification(is_meme=True, is_current=True, heat=50, confidence=0.2, reason="")
    )[0] == "unclear"
    assert audit._verifier_verdict(
        Verification(
            is_meme=False, is_current=False, heat=0, confidence=0.0, reason="timeout",
            error="timeout",
        )
    )[0] == "error"
    # 兼容返回 dict 的 fake verifier（不要求 dataclass）
    assert audit._verifier_verdict(
        {"is_meme": True, "is_current": True, "confidence": 0.7}
    )[0] == "pass"
    assert audit._verifier_verdict(
        {"is_meme": False, "is_current": True, "confidence": 0.9}
    )[0] == "fail"
    assert audit._verifier_verdict({"error": "boom"})[0] == "error"


def test_llm_enabled_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEME_INFO_VERIFY", raising=False)
    assert audit.llm_enabled() is False
    monkeypatch.setenv("MEME_INFO_VERIFY", "0")
    assert audit.llm_enabled() is False
    monkeypatch.setenv("MEME_INFO_VERIFY", "1")
    # 无 key → 仍关闭（不因开关误调用）
    monkeypatch.delenv("MEME_INFO_LLM_API_KEY", raising=False)
    monkeypatch.delenv("HOTMEME_LLM_API_KEY", raising=False)
    monkeypatch.setenv("MEME_INFO_LLM_API_KEY_FILE", "/nonexistent/key")
    monkeypatch.setenv("HOME", "/nonexistent-home")
    assert audit.llm_enabled() is False


def test_run_audit_round_disabled_makes_no_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认关闭：只读队列，不写 audit_event/pool1，也不调用 verifier。"""
    import contextlib

    executed: list[str] = []

    class _Cursor:
        def fetchall(self):  # noqa: ANN201
            return []

    class _FakeConn:
        def execute(self, sql, params=None):  # noqa: ANN001, ANN201
            executed.append(str(sql))
            return _Cursor()

    conn = _FakeConn()

    class _FakeState:
        @staticmethod
        def transaction(_target=None):  # noqa: ANN001, ANN201
            return contextlib.nullcontext(conn)

    monkeypatch.delenv("MEME_INFO_VERIFY", raising=False)
    original = audit._state_module
    original_url = audit.default_audit_url
    audit._state_module = lambda: _FakeState  # type: ignore[assignment]
    audit.default_audit_url = lambda _explicit=None: "fake-dsn"  # type: ignore[assignment]
    try:
        result = audit.run_audit_round(limit=3)
    finally:
        audit._state_module = original  # type: ignore[assignment]
        audit.default_audit_url = original_url  # type: ignore[assignment]
    assert result["enabled"] is False
    assert result["reason"] == "llm_disabled"
    assert result["audited"] == 0
    assert executed, "应只读取队列"
    for statement in executed:
        head = statement.strip().split(None, 1)[0].lower()
        assert head in ("select", "show"), f"LLM 关闭时不允许任何写 SQL：{statement[:60]}"


def test_cli_run_disabled_without_db() -> None:
    env = {key: value for key, value in os.environ.items() if key != "MEME_INFO_VERIFY"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    completed = subprocess.run(
        [sys.executable, "-m", "tools.audit_round", "run", "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert '"llm_disabled"' in completed.stdout
    assert '"enabled": false' in completed.stdout


def test_cli_parser_commands() -> None:
    from tools.audit_round import build_parser

    parser = build_parser()
    assert parser.parse_args(["list"]).command == "list"
    assert parser.parse_args(["run", "--dry-run"]).dry_run is True
    assert parser.parse_args(["show", "梗A"]).family_key == "梗A"


# ================================================================ 7. Postgres 集成
class _Rollback(Exception):
    """用回滚结束测试事务（0 残留）。"""


@pytest.fixture()
def db():  # noqa: ANN001 - 简单 fixture
    if not DB_URL:
        pytest.skip("需要 TEST_DATABASE_URL")
    from meme_info import state

    if MIGRATE_URL:  # 迁移需要 migrator 角色；无 migrator DSN 时假设 schema 已就绪
        state.migrate(MIGRATE_URL)
    else:
        assert state.check(DB_URL)["ok"] is True
    return DB_URL


def _table_counts(conn) -> dict[str, int]:  # noqa: ANN001
    tables = (
        "pool1",
        "pool2",
        "audit_event",
        "pool_transition",
        "family_alias",
        "family_round_stat",
        "round",
    )
    return {
        table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def _seed_round(conn) -> int:  # noqa: ANN001
    row = conn.execute(
        "INSERT INTO round (started_at, window_start, window_end, status) "
        "VALUES (now(), now() - interval '15 minutes', now(), 'ok') RETURNING id"
    ).fetchone()
    return int(row[0])


def _insert_pool1(  # noqa: PLR0913
    conn,  # noqa: ANN001
    key: str,
    *,
    score: float = 50.0,
    status: str = "normal",
    count_now: int = 3,
    round_id: int | None = None,
    audit_state: str = "none",
    audit_hash: str = "",
    audit_adjustment: float = 0.0,
    meta_only: bool = False,
    explainer_only: bool = False,
    evidence: dict | None = None,
    canonical: str | None = None,
    aliases: list[str] | None = None,
) -> None:
    import json

    conn.execute(
        "INSERT INTO pool1 (family_key, canonical, aliases_json, status, score, "
        "count_prev, count_now, zero_streak, audit_state, audit_hash, "
        "audit_adjustment, meta_only, explainer_only, first_seen_round, "
        "last_seen_round, evidence_json) "
        "VALUES (%s, %s, %s::jsonb, %s, %s, 0, %s, 0, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            key,
            canonical or key,
            json.dumps(aliases or [], ensure_ascii=False),
            status,
            float(score),
            int(count_now),
            audit_state,
            audit_hash,
            float(audit_adjustment),
            bool(meta_only),
            bool(explainer_only),
            round_id,
            round_id,
            json.dumps(_evidence() if evidence is None else evidence, ensure_ascii=False),
        ),
    )


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@requires_db
def test_db_record_audit_writes_event_and_score(db: str) -> None:
    from meme_info import state

    key = _unique("集成梗")
    counts_before: dict[str, int] = {}
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            counts_before = _table_counts(conn)
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=50.0, round_id=round_id)
            result = audit.record_audit(
                conn, family_key=key, kind="llm", verdict="pass",
                model="fake-model", confidence=0.9, reason="测试通过",
            )
            assert result["skipped"] is False
            assert result["score_before"] == 50.0
            assert result["score_after"] == 58.0
            assert result["audit_state"] == "llm_pass"
            row = conn.execute(
                "SELECT score, audit_state, audit_hash, audit_adjustment "
                "FROM pool1 WHERE family_key = %s",
                (key,),
            ).fetchone()
            assert float(row[0]) == 58.0
            assert row[1] == "llm_pass"
            assert row[2] == result["evidence_hash"]
            assert float(row[3]) == 8.0
            event = conn.execute(
                "SELECT kind, verdict, delta, model, evidence_hash FROM audit_event "
                "WHERE family_key = %s",
                (key,),
            ).fetchall()
            assert len(event) == 1
            assert event[0][0] == "llm" and event[0][1] == "pass"
            assert float(event[0][2]) == 8.0
            assert event[0][3] == "fake-model"
            raise _Rollback

    with state.connection(db) as conn:  # 事务回滚后 0 残留
        after = _table_counts(conn)
        assert after == counts_before, "测试写入必须随事务回滚"
        assert (
            conn.execute(
                "SELECT count(*) FROM pool1 WHERE family_key = %s", (key,)
            ).fetchone()[0]
            == 0
        )


@requires_db
def test_db_llm_duplicate_evidence_skipped(db: str) -> None:
    from meme_info import state

    key = _unique("重复梗")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=50.0, round_id=round_id)
            first = audit.record_audit(
                conn, family_key=key, kind="llm", verdict="pass"
            )
            second = audit.record_audit(
                conn, family_key=key, kind="llm", verdict="fail"
            )
            assert first["skipped"] is False
            assert second["skipped"] is True
            assert second["skip_reason"] == "duplicate_evidence"
            row = conn.execute(
                "SELECT score, audit_adjustment FROM pool1 WHERE family_key = %s",
                (key,),
            ).fetchone()
            assert float(row[0]) == 58.0  # 没有重复减分
            assert float(row[1]) == 8.0
            assert (
                conn.execute(
                    "SELECT count(*) FROM audit_event WHERE family_key = %s",
                    (key,),
                ).fetchone()[0]
                == 1
            )
            raise _Rollback


@requires_db
def test_db_human_overrides_llm_same_evidence(db: str) -> None:
    from meme_info import state

    key = _unique("覆盖梗")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=50.0, round_id=round_id)
            evidence_hash = audit.family_evidence_hash(audit.load_pool1_row(conn, key))
            llm = audit.record_audit(
                conn, family_key=key, kind="llm", verdict="fail",
                evidence_hash=evidence_hash,
            )
            assert llm["score_after"] == 35.0
            human = audit.record_audit(
                conn, family_key=key, kind="human", verdict="pass",
                operator="tester", reason="人工确认", evidence_hash=evidence_hash,
            )
            assert human["score_before"] == 35.0
            assert human["score_after"] == 65.0
            assert human["audit_state"] == "human_pass"
            assert human["old_adjustment"] == -15.0
            assert human["delta"] == 15.0
            events = conn.execute(
                "SELECT kind, verdict FROM audit_event WHERE family_key = %s "
                "ORDER BY id",
                (key,),
            ).fetchall()
            assert [tuple(row) for row in events] == [
                ("llm", "fail"),
                ("human", "pass"),
            ]
            # 人工结论之后，LLM 对同一证据必须跳过
            again = audit.record_audit(
                conn, family_key=key, kind="llm", verdict="pass",
                evidence_hash=evidence_hash,
            )
            assert again["skipped"] is True
            assert again["skip_reason"] == "human_override"
            raise _Rollback


@requires_db
def test_db_latest_replacement_three_steps(db: str) -> None:
    from meme_info import state

    key = _unique("替换梗")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=50.0, round_id=round_id)
            # 每次换证据 hash，模拟证据变化后的重新审核
            audit.record_audit(
                conn, family_key=key, kind="llm", verdict="pass",
                evidence_hash=audit.family_evidence_hash(
                    audit.load_pool1_row(conn, key)
                ),
            )
            row = audit.load_pool1_row(conn, key)
            row.evidence_json = _evidence(2)
            conn.execute(
                "UPDATE pool1 SET evidence_json = %s::jsonb WHERE family_key = %s",
                (json.dumps(row.evidence_json, ensure_ascii=False), key),
            )
            audit.record_audit(
                conn, family_key=key, kind="human", verdict="fail",
                operator="tester", reason="人工否决",
                evidence_hash=audit.family_evidence_hash(
                    audit.load_pool1_row(conn, key)
                ),
            )
            row = audit.load_pool1_row(conn, key)
            row.evidence_json = _evidence(3)
            conn.execute(
                "UPDATE pool1 SET evidence_json = %s::jsonb WHERE family_key = %s",
                (json.dumps(row.evidence_json, ensure_ascii=False), key),
            )
            result = audit.record_audit(
                conn, family_key=key, kind="human", verdict="pass",
                operator="tester", reason="人工通过",
                evidence_hash=audit.family_evidence_hash(
                    audit.load_pool1_row(conn, key)
                ),
            )
            assert result["score_after"] == 65.0  # 50 + 15，不是 50-2
            assert result["old_adjustment"] == -25.0
            final = audit.load_pool1_row(conn, key)
            assert final.audit_adjustment == 15.0
            assert final.score == 65.0
            raise _Rollback


@requires_db
def test_db_stale_evidence_rejected(db: str) -> None:
    from meme_info import state

    key = _unique("过期梗")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=60.0, round_id=round_id)
            with pytest.raises(audit.AuditStaleEvidenceError):
                audit.record_audit(
                    conn, family_key=key, kind="human", verdict="pass",
                    operator="tester", evidence_hash="deadbeef" * 4,
                )
            assert (
                conn.execute(
                    "SELECT count(*) FROM audit_event WHERE family_key = %s",
                    (key,),
                ).fetchone()[0]
                == 0
            )
            assert audit.load_pool1_row(conn, key).score == 60.0
            raise _Rollback


@requires_db
def test_db_pool2_sync_insert_and_fail_removal(db: str) -> None:
    from meme_info import state

    key = _unique("池2梗")
    fail_key = _unique("否决梗")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=60.0, count_now=4, round_id=round_id)
            result = audit.record_audit(
                conn, family_key=key, kind="human", verdict="pass",
                operator="tester",
            )
            assert result["score_after"] == 75.0
            assert result["pool2_after"] is True
            assert (
                conn.execute(
                    "SELECT count(*) FROM pool2 WHERE family_key = %s", (key,)
                ).fetchone()[0]
                == 1
            )
            # 高分 + fail：即使 65 仍 >=55 也撤出池2（模式 B 否决）
            _insert_pool1(
                conn, fail_key, score=90.0, count_now=9, round_id=round_id
            )
            conn.execute(
                "INSERT INTO pool2 (family_key, round_id, reason) VALUES (%s, %s, 'test')",
                (fail_key, round_id),
            )
            failed = audit.record_audit(
                conn, family_key=fail_key, kind="human", verdict="fail",
                operator="tester", reason="人工否决",
            )
            assert failed["score_after"] == 65.0
            assert failed["pool2_before"] is True
            assert failed["pool2_after"] is False
            assert (
                conn.execute(
                    "SELECT count(*) FROM pool2 WHERE family_key = %s", (fail_key,)
                ).fetchone()[0]
                == 0
            )
            transitions = conn.execute(
                "SELECT reason FROM pool_transition WHERE family_key = %s",
                (fail_key,),
            ).fetchall()
            assert any("p2:out_audit_fail" in row[0] for row in transitions)
            raise _Rollback


@requires_db
def test_db_explainer_only_kept_in_pool2(db: str) -> None:
    from meme_info import state

    key = _unique("种子梗")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(
                conn,
                key,
                score=40.0,
                count_now=0,
                round_id=round_id,
                meta_only=True,
                explainer_only=True,
            )
            conn.execute(
                "INSERT INTO pool2 (family_key, round_id, reason, explainer_only) "
                "VALUES (%s, %s, 'test', true)",
                (key, round_id),
            )
            result = audit.record_audit(
                conn, family_key=key, kind="human", verdict="unclear",
                operator="tester", reason="需要更多证据",
            )
            assert result["score_after"] == 40.0
            assert result["pool2_after"] is True
            assert (
                conn.execute(
                    "SELECT count(*) FROM pool2 WHERE family_key = %s", (key,)
                ).fetchone()[0]
                == 1
            )
            raise _Rollback


@requires_db
def test_db_merge_moves_aliases_and_source_out(db: str) -> None:
    import json

    from meme_info import state

    source = _unique("源梗")
    target = _unique("目标梗")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(
                conn, source, canonical=source, score=55.0, count_now=2,
                round_id=round_id, aliases=["源别名"],
            )
            _insert_pool1(
                conn, target, canonical=target, score=60.0, count_now=4,
                round_id=round_id, aliases=["目标别名"],
            )
            result = audit.record_audit(
                conn, family_key=source, kind="human", verdict="merge",
                operator="tester", reason="同一梗", merge_into=target,
            )
            assert result["ok"] is True
            assert result["merged_into"] == target
            src_row = conn.execute(
                "SELECT status, audit_state, evidence_json FROM pool1 "
                "WHERE family_key = %s",
                (source,),
            ).fetchone()
            assert src_row[0] == "out"
            assert src_row[1] == "human_merge"
            evidence_value = src_row[2]
            if isinstance(evidence_value, (str, bytes)):
                evidence_value = json.loads(evidence_value)
            assert evidence_value["merged_into"] == target
            target_row = conn.execute(
                "SELECT aliases_json FROM pool1 WHERE family_key = %s", (target,)
            ).fetchone()
            aliases = target_row[0]
            if isinstance(aliases, (str, bytes)):
                aliases = json.loads(aliases)
            assert "源别名" in aliases and source in aliases
            event = conn.execute(
                "SELECT verdict, delta FROM audit_event WHERE family_key = %s",
                (source,),
            ).fetchone()
            assert event[0] == "merge" and float(event[1]) == 0.0
            raise _Rollback


@requires_db
def test_db_queue_and_human_review_exclusion(db: str) -> None:
    from meme_info import state

    key = _unique("队列梗")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=62.0, count_now=2, round_id=round_id)
            queue = audit.build_audit_queue(conn, limit=50)
            match = [item for item in queue if item.family_key == key]
            assert match and match[0].priority == audit.PRIORITY_NEAR_P2
            record = audit.record_audit(
                conn, family_key=key, kind="human", verdict="pass",
                operator="tester",
                evidence_hash=match[0].evidence_hash,
            )
            assert record["pool2_after"] is True
            queue_after = audit.build_audit_queue(conn, limit=50)
            assert all(item.family_key != key for item in queue_after)
            raise _Rollback


@requires_db
def test_run_audit_round_with_fake_verifier(db: str) -> None:
    from meme_info import state

    key = _unique("假审核梗")

    class FakeVerifier:
        def __init__(self) -> None:
            self.calls = 0

        def verify_meme(self, meme, *, now):  # noqa: ANN001, ANN201
            self.calls += 1
            assert meme.family_key == key
            return Verification(
                is_meme=True,
                is_current=True,
                heat=60.0,
                confidence=0.9,
                reason="fake 通过",
                model="fake-model",
                prompt_version="fake/1",
            )

    fake = FakeVerifier()
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=62.0, count_now=2, round_id=round_id)
            # 队列里可能有其他真实数据：limit 取大，再断言本 family 被审核
            result = audit.run_audit_round(conn, limit=100, verifier=fake, now=NOW)
            assert result["enabled"] is True
            assert result["audited"] >= 1
            assert fake.calls >= 1
            row = audit.load_pool1_row(conn, key)
            assert row.audit_state == "llm_pass"
            assert row.audit_adjustment == 8.0
            raise _Rollback


@requires_db
def test_run_audit_round_disabled_makes_no_writes_db(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meme_info import state

    monkeypatch.delenv("MEME_INFO_VERIFY", raising=False)

    counts_before: dict[str, int] = {}
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            counts_before = _table_counts(conn)
            result = audit.run_audit_round(conn, limit=5)
            assert result["enabled"] is False
            assert result["reason"] == "llm_disabled"
            assert result["audited"] == 0
            assert _table_counts(conn) == counts_before
            raise _Rollback
