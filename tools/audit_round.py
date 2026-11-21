"""Phase F CLI：审核队列 / LLM 审核一轮 / 单 family 审核流水（不接 worker）。

用法示例::

    # 查看审核队列（默认只读 Admin DSN；不调用 LLM）
    python -m tools.audit_round list --limit 20

    # LLM 审核一轮（默认关闭：MEME_INFO_VERIFY!=1 时 0 调用 0 写入，直接返回）
    MEME_INFO_VERIFY=1 python -m tools.audit_round run --limit 5
    MEME_INFO_VERIFY=1 python -m tools.audit_round run --limit 5 --dry-run --json

    # 查看某 family 的 pool1 状态 + audit_event 流水
    python -m tools.audit_round show 某梗

连接串：``--database-url`` > ``ADMIN_DATABASE_URL`` > ``DATABASE_URL``（不打印连接串）。
退出码：0 = 成功（含 LLM 关闭的空操作）；1 = 运行失败；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from meme_info import audit as audit_mod

REPO_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.audit_round",
        description="Phase F：审核队列 / LLM 审核 / audit_event 流水",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="显式审核连接串（缺省 ADMIN_DATABASE_URL；不打印）",
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="列出审核队列（只读）")
    list_parser.add_argument("--limit", type=int, default=20, help="队列上限（默认 20）")
    list_parser.add_argument("--json", action="store_true", help="输出完整 JSON")

    run_parser = sub.add_parser("run", help="LLM 审核一轮（默认关闭）")
    run_parser.add_argument("--limit", type=int, default=5, help="单轮最多审核条数（默认 5）")
    run_parser.add_argument(
        "--dry-run", action="store_true", help="只输出计划，不写 audit_event/pool1"
    )
    run_parser.add_argument("--now", type=int, default=None, help="固定当前时间戳（调试）")
    run_parser.add_argument("--json", action="store_true", help="输出完整 JSON")

    show_parser = sub.add_parser("show", help="查看单 family 状态与审核流水")
    show_parser.add_argument("family_key", help="family_key（pool1 主键）")
    show_parser.add_argument("--events-limit", type=int, default=20, help="流水条数上限")
    show_parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    return parser


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _format_candidate(row: dict[str, Any]) -> str:
    flags = []
    if row.get("explainer_only"):
        flags.append("explainer")
    if row.get("meta_only"):
        flags.append("meta-only")
    if row.get("evidence_strong"):
        flags.append("strong-evidence")
    latest = row.get("latest_event") or {}
    latest_text = (
        f"{latest.get('kind')}:{latest.get('verdict')}" if latest else "never"
    )
    return (
        f"P{row.get('priority')} score={row.get('score')} "
        f"status={row.get('status')} pool={row.get('pool')} "
        f"audit={row.get('audit_state')} last={latest_text} "
        f"key={row.get('family_key')} ({row.get('priority_reason')})"
        + (f" [{'/'.join(flags)}]" if flags else "")
    )


def _list_queue(args: argparse.Namespace) -> int:
    from meme_info import state

    url = audit_mod.default_audit_url(args.database_url)
    with state.connection(url, autocommit=True) as conn:
        candidates = audit_mod.build_audit_queue(conn, limit=max(0, int(args.limit)))
        stats = audit_mod.audit_stats(conn)
    payload = {
        "ok": True,
        "schema_version": audit_mod.AUDIT_SCHEMA_VERSION,
        "llm_enabled": audit_mod.llm_enabled(),
        "queue": [candidate.to_dict() for candidate in candidates],
        "stats": stats,
    }
    if args.json:
        _print_json(payload)
        return 0
    print(
        f"审核队列 {len(candidates)} 条（llm_enabled={payload['llm_enabled']}；"
        f"audit_event={stats['events']}；已审 family={stats['audited_families']}）"
    )
    for candidate in candidates:
        print("  " + _format_candidate(candidate.to_dict()))
    return 0


def _run_round(args: argparse.Namespace) -> int:
    if not audit_mod.llm_enabled():
        payload = {
            "ok": True,
            "enabled": False,
            "reason": "llm_disabled",
            "message": "LLM 审核默认关闭：设置 MEME_INFO_VERIFY=1 且配置 key 后才会调用",
            "audited": 0,
            "results": [],
        }
        if args.json:
            _print_json(payload)
        else:
            print(payload["message"])
        return 0
    from meme_info import state

    url = audit_mod.default_audit_url(args.database_url)
    with state.transaction(url) as conn:
        result = audit_mod.run_audit_round(
            conn,
            limit=max(1, int(args.limit)),
            dry_run=bool(args.dry_run),
            now=args.now,
        )
    if args.json:
        _print_json(result)
        return 0
    print(
        f"LLM 审核：queue={result['queue']} audited={result['audited']} "
        f"skipped={result['skipped']} errors={len(result['errors'])} "
        f"dry_run={result['dry_run']}"
    )
    for item in result.get("results") or []:
        print(
            f"  {item.get('family_key')} → {item.get('verdict')} "
            f"score={item.get('score_after')} state={item.get('audit_state')}"
        )
    if result.get("errors"):
        for error in result["errors"]:
            print(f"  ! {error.get('family_key')}: {error.get('error')}")
    return 0


def _show(args: argparse.Namespace) -> int:
    from meme_info import state

    url = audit_mod.default_audit_url(args.database_url)
    with state.connection(url, autocommit=True) as conn:
        row = audit_mod.load_pool1_row(conn, args.family_key)
        if row is None:
            print(json.dumps({"ok": False, "error": "family 不存在"}, ensure_ascii=False))
            return 1
        events = audit_mod.list_audit_events(
            conn, args.family_key, limit=int(args.events_limit)
        )
        in_pool2 = args.family_key in audit_mod.load_pool2_keys(conn)
    payload = {
        "ok": True,
        "family_key": row.family_key,
        "canonical": row.canonical,
        "status": row.status,
        "score": round(row.score, 3),
        "pool": "pool2" if in_pool2 else "pool1",
        "audit_state": row.audit_state,
        "audit_hash": row.audit_hash,
        "audit_adjustment": row.audit_adjustment,
        "evidence_hash": audit_mod.family_evidence_hash(row),
        "evidence": row.evidence_json,
        "events": events,
    }
    if args.json:
        _print_json(payload)
        return 0
    print(
        f"{row.family_key} ({row.canonical}) score={payload['score']} "
        f"status={row.status} pool={payload['pool']} "
        f"audit={row.audit_state} adj={row.audit_adjustment:+.0f}"
    )
    for event in events:
        print(
            f"  {event['created_at']} {event['kind']}:{event['verdict']} "
            f"Δ{event['delta']:+.0f} op={event['operator'] or '-'} "
            f"model={event['model'] or '-'} hash={event['evidence_hash'][:8]} "
            f"reason={event['reason'][:80]}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _list_queue(args)
        if args.command == "run":
            return _run_round(args)
        if args.command == "show":
            return _show(args)
    except audit_mod.AuditConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except audit_mod.AuditError as exc:
        print(f"审核错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 边界
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
