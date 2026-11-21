"""状态层 CLI：``python -m tools.migrate_state {migrate,check,version}``。

用法::

    python -m tools.migrate_state migrate              # 应用迁移（幂等）
    python -m tools.migrate_state check                # 健康检查（ok=false 时退出码 1）
    python -m tools.migrate_state version              # 只看 schema version
    python -m tools.migrate_state check --json         # 机器可读输出
    python -m tools.migrate_state migrate --database-url postgresql://...

连接串优先级：
- ``migrate``：``--database-url`` > ``MIGRATE_DATABASE_URL`` > ``DATABASE_URL``
- ``check`` / ``version``：``--database-url`` > ``DATABASE_URL``
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from meme_info import state

EXIT_OK = 0
EXIT_FAIL = 1


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _format_check(info: dict[str, Any]) -> str:
    lines = [
        f"ok: {'true' if info['ok'] else 'false'}",
        f"Postgres 版本: {info['server_version']}",
        f"schema_version: {info['schema_version']}（需要 {info['required_version']}）",
        f"连接角色: {info['current_user']}（session_user={info['session_user']}）",
        f"数据库: {info['database']}",
        f"监听地址: {info['listen_addresses']}",
        f"编码 / 时区: {info['server_encoding']} / {info['timezone']}",
        "表（%d）: %s"
        % (
            len(info["tables"]),
            "  ".join(
                f"{name}={'ok' if exists else 'MISSING'}"
                for name, exists in info["tables"].items()
            ),
        ),
    ]
    if info["missing_tables"]:
        lines.append("缺失表: " + ", ".join(info["missing_tables"]))
    return "\n".join(lines)


def cmd_migrate(args: argparse.Namespace) -> int:
    result = state.migrate(args.database_url)
    if args.json:
        _print_json(
            {
                "version": result.version,
                "already_current": result.already_current,
                "applied": [migration.label for migration in result.applied],
            }
        )
    else:
        print(result.summary())
        info = state.check(state.database_url(args.database_url))
        print(_format_check(info))
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    info = state.check(args.database_url)
    if args.json:
        _print_json(info)
    else:
        print(_format_check(info))
    return EXIT_OK if info["ok"] else EXIT_FAIL


def cmd_version(args: argparse.Namespace) -> int:
    info = state.version_info(state.database_url(args.database_url))
    if args.json:
        _print_json(info)
    else:
        print(
            "schema_version=%s（磁盘最新 %s）；pending=%s"
            % (
                info["schema_version"],
                info["latest_available"],
                ", ".join(info["pending"]) or "无",
            )
        )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.migrate_state",
        description="hot_meme_search Postgres 状态层：迁移 / 健康检查 / 版本",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="应用未执行的迁移（幂等）")
    migrate.add_argument("--database-url", default=None, help="覆盖 MIGRATE_DATABASE_URL/DATABASE_URL")
    migrate.add_argument("--json", action="store_true", help="JSON 输出")
    migrate.set_defaults(func=cmd_migrate)

    check = subparsers.add_parser("check", help="健康检查：版本 / schema version / 表 / 角色")
    check.add_argument("--database-url", default=None, help="覆盖 DATABASE_URL")
    check.add_argument("--json", action="store_true", help="JSON 输出")
    check.set_defaults(func=cmd_check)

    version = subparsers.add_parser("version", help="只读：当前 schema version 与待应用迁移")
    version.add_argument("--database-url", default=None, help="覆盖 DATABASE_URL")
    version.add_argument("--json", action="store_true", help="JSON 输出")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except state.StateError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:  # psycopg 连接/权限错误等
        print(f"失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
