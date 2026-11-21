"""Postgres 状态层：连接 / 迁移 / 健康检查 / 事务 helper。

Phase A 只提供状态层基础设施，不实现业务逻辑：
不 import ``app/``、不 import FastAPI、不接 worker/adapter，只依赖标准库 + psycopg（psycopg3）。

角色分工（见 ``docs/pm/REARCHITECTURE.md`` §3.7.1）：
- ``hotmeme_migrator``：建表 / 迁移（DDL）；
- ``hotmeme_worker``：运行时读写业务表（DML）；
- ``hotmeme_admin``：审核 / 池读写（DML）。

连接串来源：
- ``DATABASE_URL``：运行时默认连接（worker）；
- ``ADMIN_DATABASE_URL``：审核连接（admin）；
- ``MIGRATE_DATABASE_URL``：迁移连接（migrator），缺省回退 ``DATABASE_URL``。
显式参数优先于环境变量。

示例::

    from meme_info import state

    with state.transaction() as conn:            # 自动提交/回滚
        conn.execute("INSERT INTO round (...) VALUES (...)")

    info = state.check()
    print(info["server_version"], info["schema_version"])

    state.migrate()                              # 幂等；所需角色 migrator
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import Connection

# ---------------------------------------------------------------- 常量
DATABASE_URL_ENV = "DATABASE_URL"
ADMIN_DATABASE_URL_ENV = "ADMIN_DATABASE_URL"
MIGRATE_DATABASE_URL_ENV = "MIGRATE_DATABASE_URL"

#: 迁移 SQL 目录（仓库根 ``migrations/*.sql``）
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

#: 版本表
SCHEMA_VERSION_TABLE = "schema_version"

#: 8 张业务表（严格对应 §4；schema_version 另计）
BUSINESS_TABLES: tuple[str, ...] = (
    "round",
    "video_metric",
    "pool1",
    "pool2",
    "family_round_stat",
    "audit_event",
    "family_alias",
    "pool_transition",
)

#: ``check`` 检查的全部表 = 8 张业务表 + schema_version
EXPECTED_TABLES: tuple[str, ...] = BUSINESS_TABLES + (SCHEMA_VERSION_TABLE,)

#: 保留天数（§3.7；只做约定，Phase A 不建定时任务）
RETENTION_DAYS: dict[str, int] = {
    "video_metric": 7,
    "family_round_stat": 30,
    "round": 90,
    "audit_event": 90,
    "pool_transition": 90,
}

#: 迁移互斥锁 key（pg_advisory_xact_lock，防并发重复迁移）
MIGRATION_LOCK_KEY = 0x686F746D656D65

#: 连接参数：短超时，避免状态层把 worker 卡死
CONNECT_TIMEOUT_SECONDS = 5

_MIGRATION_FILE_RE = re.compile(r"^(?P<version>\d+)[_-](?P<name>.+)\.sql$")


# ---------------------------------------------------------------- 异常
class StateError(RuntimeError):
    """状态层错误基类。"""


class StateConfigError(StateError):
    """缺少连接串等配置错误。"""


class MigrationError(StateError):
    """迁移文件 / 迁移执行错误。"""


# ---------------------------------------------------------------- 连接串
def database_url(explicit: str | None = None) -> str:
    """运行时可读连接串：显式参数 > ``DATABASE_URL``。"""
    url = explicit or os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise StateConfigError(
            f"缺少数据库连接串：请设置 {DATABASE_URL_ENV} 或显式传入 url"
        )
    return url


def admin_database_url(explicit: str | None = None) -> str:
    """审核/池读写连接串：显式参数 > ``ADMIN_DATABASE_URL`` > ``DATABASE_URL``。"""
    url = explicit or os.environ.get(ADMIN_DATABASE_URL_ENV) or os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise StateConfigError(
            f"缺少数据库连接串：请设置 {ADMIN_DATABASE_URL_ENV} 或 {DATABASE_URL_ENV}"
        )
    return url


def migrate_database_url(explicit: str | None = None) -> str:
    """迁移连接串：显式参数 > ``MIGRATE_DATABASE_URL`` > ``DATABASE_URL``。"""
    url = (
        explicit
        or os.environ.get(MIGRATE_DATABASE_URL_ENV)
        or os.environ.get(DATABASE_URL_ENV)
    )
    if not url:
        raise StateConfigError(
            f"缺少数据库连接串：请设置 {MIGRATE_DATABASE_URL_ENV} 或 {DATABASE_URL_ENV}"
        )
    return url


# ---------------------------------------------------------------- 连接 / 事务
def connect(url: str | None = None, *, autocommit: bool = False) -> Connection:
    """打开一条连接（调用方负责关闭；推荐用 :func:`connection` / :func:`transaction`）。"""
    return psycopg.connect(
        database_url(url),
        autocommit=autocommit,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name="hotmeme-state",
    )


@contextmanager
def connection(url: str | None = None, *, autocommit: bool = True) -> Iterator[Connection]:
    """只读/短连接 context manager；正常退出提交（autocommit 时无事务），异常关闭回滚。"""
    conn = connect(url, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(target: Connection | str | None = None) -> Iterator[Connection]:
    """事务 helper：正常退出提交、异常回滚。

    用法::

        with state.transaction() as conn:                     # 从 DATABASE_URL 新建连接
            ...

        with state.transaction(url) as conn:                  # 指定连接串新建
            ...

        with state.transaction(existing_conn) as conn:        # 复用已有连接（可嵌套）
            ...

    传入 ``Connection`` 时复用该连接（``conn.transaction()`` 在已有事务内退化为
    SAVEPOINT，嵌套安全），不负责关闭；传入 ``None``/URL 时新建并关闭连接。
    """
    if isinstance(target, Connection):
        with target.transaction():
            yield target
        return

    conn = connect(target)
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- SQL 拆分
def split_sql_statements(sql: str) -> list[str]:
    """把迁移 SQL 拆成单条语句。

    psycopg 扩展协议一次只能执行一条语句，因此迁移文件需要按语句拆分。
    支持：单引号字符串（``''`` 转义）、双引号标识符、``--`` 行注释、
    ``/* */`` 块注释（可嵌套）、``$tag$...$tag$`` 美元引用。
    纯注释/空白片段会被丢弃；语句末尾的 ``;`` 不包含在结果中。
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    length = len(sql)

    def flush() -> None:
        text = "".join(buf).strip()
        if text:
            statements.append(text)
        buf.clear()

    while i < length:
        ch = sql[i]

        # -- 行注释
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = length if end == -1 else end  # 保留换行给下一轮
            buf.append(" ")
            continue

        # -- 块注释（PostgreSQL 允许嵌套）
        if ch == "/" and sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < length and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise MigrationError("SQL 块注释未闭合")
            buf.append(" ")
            continue

        # -- 单引号字符串（'' 为转义）
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < length:
                buf.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # -- 双引号标识符（"" 为转义）
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < length:
                buf.append(sql[i])
                if sql[i] == '"':
                    if i + 1 < length and sql[i + 1] == '"':
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # -- 美元引用（$tag$ ... $tag$，tag 可为空）
        if ch == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if match:
                tag = match.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    raise MigrationError(f"SQL 美元引用未闭合：{tag}")
                end += len(tag)
                buf.append(sql[i:end])
                i = end
                continue

        # -- 语句结束
        if ch == ";":
            flush()
            i += 1
            continue

        buf.append(ch)
        i += 1

    flush()
    return statements


# ---------------------------------------------------------------- 迁移发现
@dataclass(frozen=True)
class Migration:
    """一个迁移文件。"""

    version: int
    name: str
    path: Path
    sql: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True)
class MigrationResult:
    """迁移执行结果。"""

    version: int
    applied: tuple[Migration, ...] = ()
    already_current: bool = False

    def summary(self) -> str:
        if not self.applied:
            return f"已是最新：schema_version={self.version}"
        labels = ", ".join(migration.label for migration in self.applied)
        return f"迁移完成：schema_version={self.version}，应用了 {labels}"


def discover_migrations(migrations_dir: Path | str | None = None) -> list[Migration]:
    """读取并排序迁移文件；版本号重复 / 文件名不合法时报错。"""
    directory = Path(migrations_dir) if migrations_dir else MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(f"迁移目录不存在：{directory}")

    migrations: list[Migration] = []
    seen: dict[int, Path] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE_RE.match(path.name)
        if not match:
            raise MigrationError(f"迁移文件名不合法（需 0001_name.sql）：{path.name}")
        version = int(match.group("version"))
        if version in seen:
            raise MigrationError(
                f"迁移版本重复：{path.name} 与 {seen[version].name} 都是 {version}"
            )
        seen[version] = path
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )
    if not migrations:
        raise MigrationError(f"迁移目录没有 *.sql：{directory}")
    migrations.sort(key=lambda item: item.version)
    return migrations


def latest_migration_version(migrations_dir: Path | str | None = None) -> int:
    """磁盘上最新迁移版本（check 的期望版本）。"""
    return discover_migrations(migrations_dir)[-1].version


# ---------------------------------------------------------------- 版本读取
def read_schema_version(conn: Connection) -> int | None:
    """当前 schema_version；版本表不存在时返回 ``None``。"""
    exists = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (SCHEMA_VERSION_TABLE,)
    ).fetchone()[0]
    if not exists:
        return None
    row = conn.execute(
        f"SELECT COALESCE(MAX(version), 0) FROM {SCHEMA_VERSION_TABLE}"
    ).fetchone()
    return int(row[0] or 0)


_CREATE_VERSION_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} (
  version     INT PRIMARY KEY,
  description TEXT,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


# ---------------------------------------------------------------- 迁移
def migrate(
    url: str | None = None,
    *,
    migrations_dir: Path | str | None = None,
    target_version: int | None = None,
) -> MigrationResult:
    """执行未应用的迁移（幂等；重复执行返回 ``already_current=True``）。

    - 连接串缺省用 :func:`migrate_database_url`（``MIGRATE_DATABASE_URL``）；
    - 全部迁移在一个事务内执行：任一失败整体回滚；
    - 用 ``pg_advisory_xact_lock`` 防并发重复迁移；
    - 版本记录写入 ``schema_version``。
    """
    migrations = discover_migrations(migrations_dir)
    if target_version is not None:
        migrations = [item for item in migrations if item.version <= target_version]
        if not migrations:
            raise MigrationError(f"没有 <= {target_version} 的迁移")

    with transaction(migrate_database_url(url)) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        conn.execute(_CREATE_VERSION_TABLE_SQL)
        current = read_schema_version(conn) or 0
        pending = [item for item in migrations if item.version > current]
        if not pending:
            return MigrationResult(version=current, already_current=True)

        for migration in pending:
            statements = split_sql_statements(migration.sql)
            if not statements:
                raise MigrationError(f"迁移没有可执行语句：{migration.path.name}")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, description) "
                "VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                (migration.version, migration.name),
            )
        return MigrationResult(version=pending[-1].version, applied=tuple(pending))


def version_info(url: str | None = None) -> dict[str, Any]:
    """只读：当前 schema version / 磁盘最新版本 / 待应用迁移。"""
    migrations = discover_migrations()
    with connection(database_url(url)) as conn:
        current = read_schema_version(conn)
    latest = migrations[-1].version
    pending = [item.label for item in migrations if (current or 0) < item.version]
    return {
        "schema_version": current,
        "latest_available": latest,
        "pending": pending,
        "up_to_date": current is not None and current >= latest,
    }


# ---------------------------------------------------------------- 健康检查
def table_presence(conn: Connection) -> dict[str, bool]:
    """8 张业务表 + schema_version 是否存在。"""
    rows = conn.execute(
        "SELECT name, to_regclass(name) IS NOT NULL FROM unnest(%s::text[]) AS name",
        (list(EXPECTED_TABLES),),
    ).fetchall()
    found = {str(name): bool(exists) for name, exists in rows}
    return {name: found.get(name, False) for name in EXPECTED_TABLES}


def check(url: str | None = None) -> dict[str, Any]:
    """健康检查：Postgres 版本 / schema version / 表是否存在 / 连接角色。

    返回可直接 ``json.dumps`` 的 dict；``ok`` 为整体结论：
    schema_version 存在、>= 磁盘最新迁移、9 张表齐全。
    """
    required_version = latest_migration_version()
    with connection(database_url(url)) as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0]
        version_string = conn.execute("SELECT version()").fetchone()[0]
        current_user, session_user = conn.execute(
            "SELECT current_user, session_user"
        ).fetchone()
        database = conn.execute("SELECT current_database()").fetchone()[0]
        listen_addresses = conn.execute("SHOW listen_addresses").fetchone()[0]
        timezone = conn.execute("SHOW timezone").fetchone()[0]
        encoding = conn.execute("SHOW server_encoding").fetchone()[0]
        schema_version = read_schema_version(conn)
        tables = table_presence(conn)

    missing = [name for name, exists in tables.items() if not exists]
    ok = (
        schema_version is not None
        and schema_version >= required_version
        and not missing
    )
    return {
        "ok": ok,
        "server_version": str(server_version),
        "version_string": str(version_string),
        "schema_version": schema_version,
        "required_version": required_version,
        "current_user": str(current_user),
        "session_user": str(session_user),
        "database": str(database),
        "listen_addresses": str(listen_addresses),
        "timezone": str(timezone),
        "server_encoding": str(encoding),
        "tables": tables,
        "missing_tables": missing,
    }


def health_check(url: str | None = None) -> dict[str, Any]:
    """兼容命名：等同 :func:`check`。"""
    return check(url)


def healthy(url: str | None = None) -> bool:
    """布尔版健康检查（连接/配置/迁移目录异常都视为不健康，不抛异常）。"""
    try:
        return bool(check(url)["ok"])
    except (psycopg.Error, StateError):
        return False


# ---------------------------------------------------------------- 保留期清理占位
def retention_plan() -> dict[str, int]:
    """各表保留天数（§3.7）。"""
    return dict(RETENTION_DAYS)


def retention_purge_sql() -> list[tuple[str, str]]:
    """返回保留期清理 SQL 占位（Phase A 不调度，不自动执行）。

    后续由 systemd timer / pg_cron 每日调用；执行前先 ``pg_dump -Fc``。
    顺序：先删明细（video_metric / family_round_stat），再删 round 及其审计表。
    """
    return [
        (
            "video_metric",
            "DELETE FROM video_metric WHERE round_id IN ("
            "SELECT id FROM round WHERE started_at < now() - interval '7 days')",
        ),
        (
            "family_round_stat",
            "DELETE FROM family_round_stat WHERE round_id IN ("
            "SELECT id FROM round WHERE started_at < now() - interval '30 days')",
        ),
        (
            "audit_event",
            "DELETE FROM audit_event WHERE round_id IN ("
            "SELECT id FROM round WHERE started_at < now() - interval '90 days')",
        ),
        (
            "pool_transition",
            "DELETE FROM pool_transition WHERE round_id IN ("
            "SELECT id FROM round WHERE started_at < now() - interval '90 days')",
        ),
        (
            "round",
            "DELETE FROM round WHERE started_at < now() - interval '90 days'",
        ),
    ]
