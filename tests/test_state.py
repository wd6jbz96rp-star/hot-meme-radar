"""Postgres 状态层测试（Phase A）。

无 ``TEST_DATABASE_URL`` 时只跑纯单元测试；有则额外跑集成测试：

- ``TEST_DATABASE_URL``：运行时角色（worker/admin）连接串，用来验证 DML 权限；
- ``TEST_MIGRATE_DATABASE_URL``：迁移角色（migrator）连接串，缺省回退
  ``TEST_DATABASE_URL``。云上验证示例（在服务器上、密码不进命令行）::

      sudo bash -c 'set -a; . /etc/hotmeme/env; set +a;
        TEST_DATABASE_URL=$DATABASE_URL \
        TEST_MIGRATE_DATABASE_URL=$MIGRATE_DATABASE_URL \
        pytest -q tests/test_state.py'
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from meme_info import state

DB_URL = os.environ.get("TEST_DATABASE_URL")
MIGRATE_URL = os.environ.get("TEST_MIGRATE_DATABASE_URL") or DB_URL

requires_db = pytest.mark.skipif(not DB_URL, reason="需要 TEST_DATABASE_URL 才能跑 Postgres 集成测试")

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_SQL = REPO_ROOT / "migrations" / "0001_state.sql"
MIGRATION_0002_SQL = REPO_ROOT / "migrations" / "0002_pool_flags.sql"


# ================================================================ 纯单元测试
def test_expected_tables_are_eight_plus_version() -> None:
    assert state.BUSINESS_TABLES == (
        "round",
        "video_metric",
        "pool1",
        "pool2",
        "family_round_stat",
        "audit_event",
        "family_alias",
        "pool_transition",
    )
    assert len(state.EXPECTED_TABLES) == 9
    assert state.SCHEMA_VERSION_TABLE in state.EXPECTED_TABLES


def test_retention_plan_matches_spec() -> None:
    plan = state.retention_plan()
    assert plan["video_metric"] == 7
    assert plan["family_round_stat"] == 30
    assert plan["round"] == 90
    assert plan["audit_event"] == 90
    assert plan["pool_transition"] == 90


def test_retention_purge_is_placeholder_only() -> None:
    statements = state.retention_purge_sql()
    assert [label for label, _ in statements] == [
        "video_metric",
        "family_round_stat",
        "audit_event",
        "pool_transition",
        "round",
    ]
    sql = "\n".join(text for _, text in statements)
    assert "interval '7 days'" in sql
    assert "interval '90 days'" in sql
    # 占位 helper 只是返回 SQL，不做任何调度/执行
    assert "cron" not in sql.lower()


def test_split_sql_basic() -> None:
    statements = state.split_sql_statements(
        "CREATE TABLE a (id int);\nINSERT INTO a VALUES (1);\n"
    )
    assert statements == ["CREATE TABLE a (id int)", "INSERT INTO a VALUES (1)"]


def test_split_sql_ignores_semicolons_in_strings_and_quotes() -> None:
    sql = """
    INSERT INTO t (a) VALUES ('x;y');
    INSERT INTO t (a) VALUES ('it''s;ok');
    SELECT "weird;column" FROM t;
    """
    statements = state.split_sql_statements(sql)
    assert len(statements) == 3
    assert "'x;y'" in statements[0]
    assert "'it''s;ok'" in statements[1]


def test_split_sql_handles_comments() -> None:
    sql = """
    -- 行注释; 有分号
    /* 块注释; /* 嵌套; */ 仍在注释里; */
    SELECT 1;
    """
    statements = state.split_sql_statements(sql)
    assert statements == ["SELECT 1"]


def test_split_sql_handles_dollar_quoting() -> None:
    sql = """
    DO $grants$
    BEGIN
      EXECUTE 'GRANT SELECT ON t TO r;';
      -- 注释里的 ;
    END
    $grants$;
    SELECT 2;
    """
    statements = state.split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("DO $grants$")
    assert statements[0].endswith("$grants$")
    assert statements[1] == "SELECT 2"


def test_split_sql_rejects_unclosed_dollar_quote() -> None:
    with pytest.raises(state.MigrationError):
        state.split_sql_statements("DO $$ BEGIN SELECT 1; END")


def test_discover_migrations_reads_repo() -> None:
    migrations = state.discover_migrations()
    assert [item.version for item in migrations] == [1, 2]
    assert [item.name for item in migrations] == ["state", "pool_flags"]
    assert migrations[0].label == "0001_state"
    assert migrations[1].label == "0002_pool_flags"
    assert state.latest_migration_version() == 2


def test_discover_migrations_rejects_bad_names(tmp_path: Path) -> None:
    (tmp_path / "0001_ok.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "not_a_migration.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(state.MigrationError):
        state.discover_migrations(tmp_path)


def test_migration_sql_has_required_schema() -> None:
    sql = MIGRATION_SQL.read_text(encoding="utf-8")
    for table in state.BUSINESS_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    # 主键 / 外键 / 索引 / 类型
    assert "PRIMARY KEY (round_id, bvid)" in sql
    assert "REFERENCES pool1(family_key) ON DELETE CASCADE" in sql
    assert "idx_video_metric_bvid_round" in sql
    assert "ON video_metric (bvid, round_id DESC)" in sql
    assert "idx_video_metric_round_hot" in sql
    assert "ON video_metric (round_id, hot_score DESC)" in sql
    assert "idx_pool1_status_score" in sql
    assert "idx_pool1_last_seen_round" in sql
    assert sql.count("TIMESTAMPTZ") >= 10
    assert "JSONB" in sql
    # 7 天保留只写注释/占位，本迁移不创建定时任务/扩展
    assert "7 days" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE" in sql
    assert "CREATE EXTENSION" not in sql
    assert "SCHEDULE" not in sql.upper()


def test_0002_migration_sql_contract() -> None:
    sql = MIGRATION_0002_SQL.read_text(encoding="utf-8")
    # pool1 新列（ADD COLUMN IF NOT EXISTS 保证幂等）
    for column in ("meta_only", "explainer_only", "audit_adjustment"):
        assert f"ALTER TABLE pool1 ADD COLUMN IF NOT EXISTS {column}" in sql
    assert "meta_only BOOLEAN NOT NULL DEFAULT false" in sql
    assert "explainer_only BOOLEAN NOT NULL DEFAULT false" in sql
    assert "audit_adjustment REAL NOT NULL DEFAULT 0" in sql
    # status 语义与默认值
    assert "ALTER TABLE pool1 ALTER COLUMN status SET DEFAULT 'normal'" in sql
    assert "UPDATE pool1 SET status = 'normal' WHERE status = 'active'" in sql
    assert "normal / provisional / out" in sql
    assert "COMMENT ON COLUMN pool1.status" in sql
    # pool2 展示列
    assert "ALTER TABLE pool2 ADD COLUMN IF NOT EXISTS explainer_only" in sql
    assert "COMMENT ON COLUMN pool2.explainer_only" in sql
    # 0001 保持不变（新字段只在 0002）
    assert "meta_only" not in MIGRATION_SQL.read_text(encoding="utf-8")


def test_state_module_has_no_app_or_fastapi_dependency() -> None:
    import ast

    tree = ast.parse((REPO_ROOT / "meme_info" / "state.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert "fastapi" not in imported
    assert "app" not in imported
    assert "psycopg" in imported
    # 只允许标准库 + psycopg（第三方模块清单在此锁定）
    stdlib_ok = {
        "__future__", "os", "re", "contextlib", "dataclasses", "pathlib", "typing",
    }
    assert imported <= stdlib_ok | {"psycopg"}


def test_config_errors_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (state.DATABASE_URL_ENV, state.ADMIN_DATABASE_URL_ENV, state.MIGRATE_DATABASE_URL_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(state.StateConfigError):
        state.database_url()
    with pytest.raises(state.StateConfigError):
        state.migrate_database_url()
    # 显式参数优先
    assert state.database_url("postgresql://x/y") == "postgresql://x/y"


def test_migrate_database_url_falls_back_to_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(state.MIGRATE_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(state.DATABASE_URL_ENV, "postgresql://runtime/db")
    monkeypatch.setenv(state.ADMIN_DATABASE_URL_ENV, "postgresql://admin/db")
    assert state.migrate_database_url() == "postgresql://runtime/db"
    assert state.database_url() == "postgresql://runtime/db"
    assert state.admin_database_url() == "postgresql://admin/db"


def test_cli_help_exits_zero() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "tools.migrate_state", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "migrate" in completed.stdout
    assert "check" in completed.stdout
    assert "version" in completed.stdout


def test_cli_check_without_url_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (state.DATABASE_URL_ENV, state.MIGRATE_DATABASE_URL_ENV)
    }
    completed = subprocess.run(
        [sys.executable, "-m", "tools.migrate_state", "check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert completed.returncode == 1
    assert "缺少数据库连接串" in completed.stderr


# ================================================================ 集成测试（需要 Postgres）
@pytest.fixture(scope="session")
def migrated() -> state.MigrationResult:
    if not DB_URL:
        pytest.skip("需要 TEST_DATABASE_URL")
    first = state.migrate(MIGRATE_URL)
    second = state.migrate(MIGRATE_URL)
    assert second.already_current, "重复迁移必须幂等"
    return first


def _new_round(conn, *, status: str = "ok") -> int:
    row = conn.execute(
        """
        INSERT INTO round (started_at, window_start, window_end, status, stats)
        VALUES (now(), now() - interval '15 minutes', now(), %s, %s)
        RETURNING id
        """,
        (status, json.dumps({"test_marker": str(uuid.uuid4())})),
    ).fetchone()
    return int(row[0])


def _delete_round(conn, round_id: int) -> None:
    conn.execute("DELETE FROM family_round_stat WHERE round_id = %s", (round_id,))
    conn.execute("DELETE FROM audit_event WHERE round_id = %s", (round_id,))
    conn.execute("DELETE FROM pool_transition WHERE round_id = %s", (round_id,))
    conn.execute("UPDATE pool2 SET round_id = NULL WHERE round_id = %s", (round_id,))
    conn.execute("DELETE FROM video_metric WHERE round_id = %s", (round_id,))
    conn.execute("DELETE FROM round WHERE id = %s", (round_id,))


@requires_db
def test_migrate_is_idempotent(migrated: state.MigrationResult) -> None:
    assert migrated.version == 2
    # 首次调用可能是空库（applied=[1,2]）、version=1 的库（applied=[2]）
    # 或已是最新（applied=[]）；但重复调用必须始终 already_current 且不再应用迁移。
    applied_versions = [item.version for item in migrated.applied]
    if migrated.already_current:
        assert applied_versions == []
    else:
        assert applied_versions in ([1, 2], [2])
        assert applied_versions[-1] == 2
    third = state.migrate(MIGRATE_URL)
    assert third.already_current and third.applied == ()
    assert third.version == 2


@requires_db
def test_check_reports_healthy(migrated: state.MigrationResult) -> None:
    info = state.check(DB_URL)
    assert info["ok"] is True
    assert info["schema_version"] == 2
    assert info["required_version"] == 2
    assert info["server_version"]
    assert "PostgreSQL" in info["version_string"]
    assert len(info["tables"]) == 9
    assert all(info["tables"].values())
    assert info["missing_tables"] == []
    assert info["current_user"] == info["session_user"]
    assert info["database"]
    assert info["server_encoding"] == "UTF8"
    assert state.health_check(DB_URL)["ok"] is True
    assert state.healthy(DB_URL) is True


@requires_db
def test_check_with_migration_role(migrated: state.MigrationResult) -> None:
    info = state.check(MIGRATE_URL)
    assert info["ok"] is True
    assert info["current_user"] == info["session_user"]


@requires_db
def test_version_info(migrated: state.MigrationResult) -> None:
    info = state.version_info(DB_URL)
    assert info["schema_version"] == 2
    assert info["latest_available"] == 2
    assert info["pending"] == []
    assert info["up_to_date"] is True


@requires_db
def test_new_pool_columns_exist_with_not_null_defaults(migrated: state.MigrationResult) -> None:
    expected = {
        ("pool1", "meta_only"): "boolean",
        ("pool1", "explainer_only"): "boolean",
        ("pool1", "audit_adjustment"): "real",
        ("pool2", "explainer_only"): "boolean",
    }
    with state.connection(DB_URL) as conn:
        rows = conn.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND (
              (table_name = 'pool1' AND column_name IN ('meta_only', 'explainer_only', 'audit_adjustment'))
              OR (table_name = 'pool2' AND column_name = 'explainer_only')
            )
            """
        ).fetchall()
    found = {(table, column): (data_type, is_nullable, default) for table, column, data_type, is_nullable, default in rows}
    for key, data_type in expected.items():
        assert key in found, f"缺列 {key}"
        actual_type, is_nullable, default = found[key]
        assert actual_type == data_type
        assert is_nullable == "NO", f"{key} 必须 NOT NULL"
        assert default is not None, f"{key} 必须有默认值"


@requires_db
def test_pool1_new_column_defaults_and_worker_write(migrated: state.MigrationResult) -> None:
    family_key = f"test-family-{uuid.uuid4()}"
    try:
        with state.transaction(DB_URL) as conn:
            conn.execute(
                "INSERT INTO pool1 (family_key, canonical) VALUES (%s, %s)",
                (family_key, "新列默认值"),
            )
        with state.connection(DB_URL) as conn:
            row = conn.execute(
                "SELECT status, meta_only, explainer_only, audit_adjustment "
                "FROM pool1 WHERE family_key = %s",
                (family_key,),
            ).fetchone()
        assert row[0] == "normal"
        assert row[1] is False
        assert row[2] is False
        assert float(row[3]) == 0.0
        # worker 对新列有 UPDATE 权限
        with state.transaction(DB_URL) as conn:
            conn.execute(
                "UPDATE pool1 SET meta_only = true, explainer_only = true, audit_adjustment = 15 "
                "WHERE family_key = %s",
                (family_key,),
            )
        with state.connection(DB_URL) as conn:
            row = conn.execute(
                "SELECT meta_only, explainer_only, audit_adjustment "
                "FROM pool1 WHERE family_key = %s",
                (family_key,),
            ).fetchone()
        assert row[0] is True
        assert row[1] is True
        assert float(row[2]) == 15.0
    finally:
        with state.transaction(DB_URL) as conn:
            conn.execute("DELETE FROM pool1 WHERE family_key = %s", (family_key,))


@requires_db
def test_pool2_explainer_only_default_and_sync(migrated: state.MigrationResult) -> None:
    family_key = f"test-family-{uuid.uuid4()}"
    try:
        with state.transaction(DB_URL) as conn:
            conn.execute(
                "INSERT INTO pool1 (family_key, canonical, explainer_only) VALUES (%s, %s, true)",
                (family_key, "explainer-only 测试"),
            )
            conn.execute(
                "INSERT INTO pool2 (family_key, explainer_only) VALUES (%s, true)", (family_key,)
            )
        with state.connection(DB_URL) as conn:
            row = conn.execute(
                "SELECT p1.explainer_only, p2.explainer_only FROM pool1 p1 "
                "JOIN pool2 p2 USING (family_key) WHERE p1.family_key = %s",
                (family_key,),
            ).fetchone()
        assert row == (True, True)
        # 默认值 false
        other_key = f"test-family-{uuid.uuid4()}"
        try:
            with state.transaction(DB_URL) as conn:
                conn.execute(
                    "INSERT INTO pool1 (family_key, canonical) VALUES (%s, %s)",
                    (other_key, "默认 false"),
                )
                conn.execute("INSERT INTO pool2 (family_key) VALUES (%s)", (other_key,))
            with state.connection(DB_URL) as conn:
                default_row = conn.execute(
                    "SELECT explainer_only FROM pool2 WHERE family_key = %s", (other_key,)
                ).fetchone()
            assert default_row[0] is False
        finally:
            with state.transaction(DB_URL) as conn:
                conn.execute("DELETE FROM pool2 WHERE family_key = %s", (other_key,))
                conn.execute("DELETE FROM pool1 WHERE family_key = %s", (other_key,))
    finally:
        with state.transaction(DB_URL) as conn:
            conn.execute("DELETE FROM pool2 WHERE family_key = %s", (family_key,))
            conn.execute("DELETE FROM pool1 WHERE family_key = %s", (family_key,))


@requires_db
def test_0002_statements_are_rerunnable(migrated: state.MigrationResult) -> None:
    """0002 的 SQL 语句本身幂等：每条连跑两次不报错（不依赖版本门禁）。"""
    statements = state.split_sql_statements(MIGRATION_0002_SQL.read_text(encoding="utf-8"))
    assert len(statements) >= 7
    with state.transaction(MIGRATE_URL) as conn:
        for statement in statements:
            conn.execute(statement)
            conn.execute(statement)


@requires_db
def test_round_insert_select(migrated: state.MigrationResult) -> None:
    with state.transaction(DB_URL) as conn:
        round_id = _new_round(conn)
    try:
        assert round_id > 0
        with state.connection(DB_URL) as conn:
            row = conn.execute(
                "SELECT status, stats->>'test_marker' FROM round WHERE id = %s", (round_id,)
            ).fetchone()
        assert row[0] == "ok"
        assert row[1]
    finally:
        with state.transaction(DB_URL) as conn:
            _delete_round(conn, round_id)


@requires_db
def test_transaction_rollback(migrated: state.MigrationResult) -> None:
    marker = str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        with state.transaction(DB_URL) as conn:
            conn.execute(
                """
                INSERT INTO round (started_at, window_start, window_end, status, stats)
                VALUES (now(), now(), now(), 'running', %s)
                """,
                (json.dumps({"test_marker": marker}),),
            )
            raise RuntimeError("boom")
    with state.connection(DB_URL) as conn:
        count = conn.execute(
            "SELECT count(*) FROM round WHERE stats->>'test_marker' = %s", (marker,)
        ).fetchone()[0]
    assert count == 0


@requires_db
def test_transaction_reuses_existing_connection(migrated: state.MigrationResult) -> None:
    with state.connection(DB_URL) as conn:
        with state.transaction(conn) as tx:
            assert tx is conn
            row = tx.execute("SELECT 1").fetchone()
        assert row[0] == 1


@requires_db
def test_video_metric_primary_key_and_indexes(migrated: state.MigrationResult) -> None:
    with state.transaction(DB_URL) as conn:
        round_id = _new_round(conn)
    try:
        with state.transaction(DB_URL) as conn:
            conn.execute(
                "INSERT INTO video_metric (round_id, bvid, title, hot_score) VALUES (%s, %s, %s, %s)",
                (round_id, "BV1test", "t", 1.5),
            )
            # 主键冲突在 SAVEPOINT 里验证，避免整条测试事务进入 aborted 状态
            with pytest.raises(Exception):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO video_metric (round_id, bvid) VALUES (%s, %s)",
                        (round_id, "BV1test"),
                    )
        with state.connection(DB_URL) as conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'video_metric'"
                ).fetchall()
            }
        assert {"idx_video_metric_bvid_round", "idx_video_metric_round_hot"} <= indexes
    finally:
        with state.transaction(DB_URL) as conn:
            _delete_round(conn, round_id)


@requires_db
def test_pool2_fk_and_cascade(migrated: state.MigrationResult) -> None:
    family_key = f"test-family-{uuid.uuid4()}"
    with state.transaction(DB_URL) as conn:
        round_id = _new_round(conn)
    try:
        with state.transaction(DB_URL) as conn:
            conn.execute(
                "INSERT INTO pool1 (family_key, canonical) VALUES (%s, %s)",
                (family_key, "测试族"),
            )
            conn.execute(
                "INSERT INTO pool2 (family_key, round_id, heat_score, rank) VALUES (%s, %s, 1.0, 1)",
                (family_key, round_id),
            )
        with pytest.raises(Exception):
            with state.transaction(DB_URL) as conn:
                conn.execute(
                    "INSERT INTO pool2 (family_key) VALUES (%s)", (f"missing-{family_key}",)
                )
        with state.transaction(DB_URL) as conn:
            conn.execute("DELETE FROM pool1 WHERE family_key = %s", (family_key,))
        with state.connection(DB_URL) as conn:
            left = conn.execute(
                "SELECT count(*) FROM pool2 WHERE family_key = %s", (family_key,)
            ).fetchone()[0]
        assert left == 0
    finally:
        with state.transaction(DB_URL) as conn:
            conn.execute("DELETE FROM pool2 WHERE family_key = %s", (family_key,))
            conn.execute("DELETE FROM pool1 WHERE family_key = %s", (family_key,))
            _delete_round(conn, round_id)


@requires_db
def test_audit_event_unique_constraint(migrated: state.MigrationResult) -> None:
    family_key = f"test-family-{uuid.uuid4()}"
    try:
        with state.transaction(DB_URL) as conn:
            conn.execute(
                "INSERT INTO pool1 (family_key, canonical) VALUES (%s, %s)",
                (family_key, "审核测试"),
            )
            conn.execute(
                """
                INSERT INTO audit_event (family_key, kind, verdict, evidence_hash)
                VALUES (%s, 'human', 'pass', 'hash1')
                """,
                (family_key,),
            )
            with pytest.raises(Exception):
                conn.execute(
                    """
                    INSERT INTO audit_event (family_key, kind, verdict, evidence_hash)
                    VALUES (%s, 'human', 'fail', 'hash1')
                    """,
                    (family_key,),
                )
    finally:
        with state.transaction(DB_URL) as conn:
            conn.execute("DELETE FROM audit_event WHERE family_key = %s", (family_key,))
            conn.execute("DELETE FROM pool1 WHERE family_key = %s", (family_key,))


@requires_db
def test_jsonb_and_timestamptz_roundtrip(migrated: state.MigrationResult) -> None:
    with state.transaction(DB_URL) as conn:
        round_id = _new_round(conn)
    try:
        with state.connection(DB_URL) as conn:
            row = conn.execute(
                "SELECT stats, started_at AT TIME ZONE 'UTC' IS NOT NULL FROM round WHERE id = %s",
                (round_id,),
            ).fetchone()
        assert isinstance(row[0], dict)
        assert row[1] is True
    finally:
        with state.transaction(DB_URL) as conn:
            _delete_round(conn, round_id)
