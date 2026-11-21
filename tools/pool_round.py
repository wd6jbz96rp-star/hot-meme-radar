"""Phase D CLI：family 观察 → score → pool1/pool2（不接 worker、不切线上）。

用法示例::

    # 纯内存（不读也不写 Postgres）：用 Phase C 落盘的 window JSON 复算
    python -m tools.pool_round --no-db --window-json /tmp/window.json --json

    # 写库一轮：读最近 round 的上一轮 pool，计算后写回本轮状态
    python -m tools.pool_round --window-json /tmp/window.json

    # 指定 round id
    python -m tools.pool_round --window-json /tmp/window.json --round 42

退出码：0 = 成功（含 degraded 冻结）；1 = 运行失败；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from meme_info.pools import PoolConfig, run_pool_round, sample_from_json

REPO_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.pool_round",
        description="Phase D：family 计数 → cred/score → pool1/pool2 状态",
    )
    parser.add_argument(
        "--window-json",
        type=Path,
        required=True,
        help="Phase C 的 WindowSample JSON（python -m tools.window_sample --output）",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="纯内存模式：不读也不写 Postgres（无 DATABASE_URL 也不报错）",
    )
    parser.add_argument(
        "--round",
        default="latest",
        help="要写入/读取的轮次：latest（默认）或 round id；纯内存模式可忽略",
    )
    parser.add_argument(
        "--database-url", default=None, help="显式数据库连接串（缺省用 DATABASE_URL）"
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    parser.add_argument("--output", type=Path, default=None, help="额外把 JSON 结果写入文件")
    parser.add_argument(
        "--now", type=int, default=None, help="固定当前时间戳（调试/可复算）"
    )
    parser.add_argument("--quiet", action="store_true", help="只输出一行摘要")
    return parser


def _parse_round(value: str) -> int | None:
    text = str(value or "latest").strip().lower()
    if text in ("latest", "", "last", "none"):
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise SystemExit(f"--round 只接受 latest 或数字 id：{value}") from exc


def _format_summary(result: Any) -> str:
    stats = result.stats or {}
    db_counts = stats.get("db_counts") or {}
    lines = [
        "Phase D family 状态与池",
        f"status: {result.status}  round_id: {result.round_id}",
        f"窗口: {result.window_start} → {result.window_end}",
        f"观察: {stats.get('observations', 0)}"
        f"（发现 {stats.get('discovered', 0)} / 补 0 {stats.get('zero_padded', 0)}）",
        f"pool1: {stats.get('pool1_total', 0)}"
        f"（active {stats.get('pool1_active', 0)}"
        f" / provisional {stats.get('pool1_provisional', 0)}"
        f" / out {stats.get('pool1_out', 0)}）",
        f"pool2: {stats.get('pool2_total', 0)}"
        f"（本轮进 {stats.get('p2_in_normal', 0) + stats.get('p2_in_explainer', 0)}"
        f" / 出 {stats.get('p2_out_low_score', 0) + stats.get('p2_out_other', 0)}"
        f" / explainer {stats.get('explainer_in_pool2', 0)}）",
        f"迁移: {len(result.transitions)}  family_round_stat: {len(result.stats_rows)}",
        f"新进 pool1: {stats.get('new_pool1', 0)}"
        f"  撤出 pool1: {stats.get('p1_exit', 0)}"
        f"  未达门槛跳过: {stats.get('skipped_no_pool', 0)}",
    ]
    if result.degraded:
        lines.append(
            f"degraded 冻结: {stats.get('frozen', 0)}"
            f"（新 family 跳过 {stats.get('degraded_new_skipped', 0)}）"
        )
    lines.append(
        f"写库: {'yes' if stats.get('db_written') else 'no'}"
        + (
            f"  行数 {db_counts}"
            if stats.get("db_written")
            else ""
        )
    )
    top = sorted(result.pool2, key=lambda row: -(row.heat_score or 0))[:5]
    if top:
        lines.append("pool2（本轮先不排名，heat 由 Phase E 计算）:")
        for row in top:
            lines.append(f"  {row.family_key}  reason={row.reason}")
    top1 = sorted(
        [row for row in result.pool1 if row.status != "out"],
        key=lambda row: -row.score,
    )[:8]
    if top1:
        lines.append("pool1 active Top8:")
        for row in top1:
            lines.append(
                f"  {row.score:6.2f}  {row.status:11s} {row.family_key}"
                f"  now={row.count_now} prev={row.count_prev} zero={row.zero_streak}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        round_id = _parse_round(args.round)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    window_path: Path = args.window_json
    if not window_path.is_file():
        print(f"错误：window JSON 不存在 {window_path}", file=sys.stderr)
        return 2
    try:
        sample = sample_from_json(window_path)
    except Exception as exc:  # noqa: BLE001 - CLI 顶层转退出码
        print(f"错误：解析 window JSON 失败：{exc}", file=sys.stderr)
        return 2
    try:
        result = run_pool_round(
            sample,
            now=args.now,
            use_db=not args.no_db,
            database_url=args.database_url,
            round_id=round_id,
            config=PoolConfig(),
        )
    except Exception as exc:  # noqa: BLE001 - CLI 顶层把错误转成退出码
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(_format_summary(result))
    else:
        print(
            f"status={result.status} round={result.round_id}"
            f" pool1={len(result.pool1)} pool2={len(result.pool2)}"
            f" transitions={len(result.transitions)}"
            f" db={'yes' if (result.stats or {}).get('db_written') else 'no'}"
        )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not args.quiet and not args.json:
            print(f"已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
