"""B 线 CLI：``python -m tools.video_route [--limit N] [--no-db] [--json]``。

用法示例::

    # 真机一轮（需要 DATABASE_URL；写 round + video_metric，不切线上）
    python -m tools.video_route

    # 最小 live smoke：只取 5 个候选、不写库
    python -m tools.video_route --limit 5 --no-db

    # 完全离线（测试/无网环境）：用 tests/fixtures 回放
    python -m tools.video_route --limit 5 --no-db --offline --fixture-dir tests/fixtures --json

退出码：0 = 成功（含 degraded）；1 = 运行失败（无候选等）；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from meme_info.bili import BiliSource
from meme_info.models import VideoRouteResult
from meme_info.video_route import VideoRoutePlan, run_video_route

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.video_route",
        description="B 线：候选视频发现 / 在线与新增指标 / 45+10 选片（Phase B）",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="仅取前 N 个候选（smoke/调试；缺省跑完整 300 候选）")
    parser.add_argument("--no-db", action="store_true",
                        help="纯内存模式：不读也不写 Postgres")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    parser.add_argument("--output", type=Path, default=None, help="额外把 JSON 结果写入文件")
    parser.add_argument("--database-url", default=None, help="显式数据库连接串（缺省用 DATABASE_URL）")
    parser.add_argument("--rps", type=float, default=2.0, help="HTTP 限速（默认 2 rps）")
    parser.add_argument("--offline", action="store_true", help="离线回放模式（需要 --fixture-dir）")
    parser.add_argument("--fixture-dir", type=Path, default=FIXTURES, help="离线回放 fixture 目录")
    parser.add_argument("--zones", default=None,
                        help="逗号分隔分区 rid（缺省 119,160,1,4,181,3,5,36）")
    parser.add_argument("--no-newlist", action="store_true", help="关闭 newlist 探索来源")
    parser.add_argument("--no-recent-search", action="store_true", help="关闭按发布时间搜索")
    parser.add_argument("--no-related", action="store_true", help="关闭相关推荐探索")
    parser.add_argument("--quiet", action="store_true", help="只输出一行摘要")
    return parser


def _build_plan(args: argparse.Namespace) -> VideoRoutePlan:
    kwargs: dict[str, Any] = {
        "max_candidates": args.limit,
        "enable_newlist": not args.no_newlist,
        "enable_recent_search": not args.no_recent_search,
        "enable_related": not args.no_related,
    }
    if args.zones:
        kwargs["zones"] = tuple(int(part) for part in args.zones.split(",") if part.strip())
    return VideoRoutePlan(**kwargs)


def _format_summary(result: VideoRouteResult) -> str:
    stats = result.stats or {}
    lines = [
        "B 线单轮结果（Phase B）",
        f"status: {result.status}",
        f"round_id: {result.round_id}",
        f"窗口: {result.window_start} → {result.window_end}",
        f"候选: {stats.get('candidates', len(result.candidates))}"
        f"（常规 {stats.get('routine', '-')} / 探索 {stats.get('exploration', '-')}）",
        f"在线覆盖: {stats.get('online_coverage')}  baseline: {stats.get('baseline')}",
        f"选片: 评论 {len(result.selected_comment)} / 弹幕 {len(result.selected_danmaku)}"
        f"（重叠 {stats.get('selection', {}).get('comment_overlap', '-')}）",
        f"分区: {stats.get('selection', {}).get('comment_zone_counts', {})}",
        f"耗时: {stats.get('elapsed_seconds')}s  错误: {stats.get('errors', 0)}",
    ]
    top = sorted(result.metrics, key=lambda metric: -(metric.hot_score or 0))[:5]
    if top:
        lines.append("hot_video Top5:")
        for metric in top:
            lines.append(
                f"  {metric.hot_score:+.3f}  {metric.bvid}  online={metric.online}"
                f"  {metric.title[:36]}"
            )
    if stats.get("degraded_reason"):
        lines.append(f"degraded_reason: {stats['degraded_reason']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    plan = _build_plan(args)
    fixture_dir = args.fixture_dir
    if args.offline and not fixture_dir.is_dir():
        print(f"错误：fixture 目录不存在 {fixture_dir}", file=sys.stderr)
        return 2
    source = BiliSource(
        offline=bool(args.offline),
        fixture_dir=fixture_dir if args.offline else None,
        max_rps=max(0.2, float(args.rps)),
    )
    try:
        result = run_video_route(
            source,
            plan,
            use_db=not args.no_db,
            database_url=args.database_url,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 顶层把错误转成退出码
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1

    payload = result.to_dict()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.quiet:
        print(
            f"{result.status} round={result.round_id} "
            f"candidates={len(result.candidates)} "
            f"comment={len(result.selected_comment)} danmaku={len(result.selected_danmaku)} "
            f"elapsed={result.stats.get('elapsed_seconds')}s"
        )
    else:
        print(_format_summary(result))
    return 0 if result.status in ("ok", "degraded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
