"""Phase C CLI：15 分钟窗口评论/弹幕采样与计数。

用法示例::

    # 真机一轮（读最近 round 的 45/10 目标并回写 round 计数；需 DATABASE_URL）
    python -m tools.window_sample --round latest

    # 纯内存（不读也不写 Postgres）；无 DATABASE_URL 不报错
    python -m tools.window_sample --round latest --no-db

    # 用 Phase B 保存的 VideoRouteResult JSON 作为目标（离线/可复算）
    python -m tools.window_sample --no-db --targets-json /tmp/video_route.json \\
        --offline --fixture-dir tests/fixtures --json --output /tmp/window.json

    # 无库时先跑 B 线内存选片（真机、较慢）再采样
    python -m tools.window_sample --no-db --discover --limit 5 --json

退出码：0 = 成功（含 degraded）；1 = 运行失败；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from meme_info.bili import BiliSource
from meme_info.models import iso, now_ts
from meme_info.window import (
    WindowSample,
    WindowSamplePlan,
    run_window_sample,
    targets_from_payload,
    targets_from_video_route,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.window_sample",
        description="Phase C：15 分钟窗口评论/弹幕采样与计数（比例计数输入）",
    )
    parser.add_argument("--round", default="latest",
                        help="要回写/读取的轮次：latest（默认）或 round id")
    parser.add_argument("--no-db", action="store_true",
                        help="纯内存模式：不读也不写 Postgres（无 DATABASE_URL 也不报错）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    parser.add_argument("--output", type=Path, default=None, help="额外把 JSON 结果写入文件")
    parser.add_argument("--database-url", default=None, help="显式数据库连接串（缺省用 DATABASE_URL）")
    parser.add_argument("--limit", type=int, default=None,
                        help="smoke：评论/弹幕各只取前 N 个目标")
    parser.add_argument("--window-minutes", type=int, default=None,
                        help="覆盖窗口长度（缺省用 round 窗口；无 round 时 15 分钟）")
    parser.add_argument("--no-evidence", action="store_true", help="不采 30 天证据")
    parser.add_argument(
        "--text-dedupe-scope",
        choices=("per_video", "global"),
        default="per_video",
        help="评论文本去重范围：per_video（默认，仅同视频）/ global（跨视频）",
    )
    parser.add_argument("--targets-json", type=Path, default=None,
                        help="显式目标：Phase B VideoRouteResult JSON 或 {comment_targets, danmaku_targets}")
    parser.add_argument("--discover", action="store_true",
                        help="--no-db 且无目标时，先用 B 线内存选片（真机较慢）")
    parser.add_argument("--rps", type=float, default=2.0, help="HTTP 限速（默认 2 rps）")
    parser.add_argument("--max-requests", type=int, default=None, help="本轮 HTTP 请求预算")
    parser.add_argument("--offline", action="store_true", help="离线回放模式（需要 --fixture-dir）")
    parser.add_argument("--fixture-dir", type=Path, default=FIXTURES, help="离线回放 fixture 目录")
    parser.add_argument("--now", type=int, default=None,
                        help="固定当前时间戳（调试/可复算；缺省取系统时间）")
    parser.add_argument("--quiet", action="store_true", help="只输出一行摘要")
    return parser


def _parse_round(value: str) -> int | None:
    text = str(value or "latest").strip().lower()
    if text in ("latest", "", "last"):
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise SystemExit(f"--round 只接受 latest 或数字 id：{value}") from exc


def _build_plan(args: argparse.Namespace) -> WindowSamplePlan:
    kwargs: dict[str, Any] = {
        "evidence": not args.no_evidence,
        "text_dedupe_scope": args.text_dedupe_scope,
    }
    if args.window_minutes is not None:
        if args.window_minutes <= 0:
            raise SystemExit("--window-minutes 必须为正整数")
        kwargs["window_minutes"] = int(args.window_minutes)
    if args.max_requests is not None:
        kwargs["max_requests"] = int(args.max_requests)
    if args.limit is not None:
        kwargs["comment_targets"] = max(1, int(args.limit))
        kwargs["danmaku_targets"] = max(1, int(args.limit))
    return WindowSamplePlan(**kwargs)


def _load_targets(
    args: argparse.Namespace,
    plan: WindowSamplePlan,
    source: BiliSource,
    now: int,
) -> tuple[list | None, list | None]:
    if args.targets_json is not None:
        path = args.targets_json
        if not path.is_file():
            print(f"错误：targets JSON 不存在 {path}", file=sys.stderr)
            raise SystemExit(2)
        payload = json.loads(path.read_text(encoding="utf-8"))
        comment_targets, danmaku_targets = targets_from_payload(payload)
        if args.limit:
            comment_targets = comment_targets[: args.limit]
            danmaku_targets = danmaku_targets[: args.limit]
        return comment_targets, danmaku_targets
    if args.discover and args.no_db:
        # 懒 import：只在需要时依赖 B 线（避免无谓加载）
        from meme_info.video_route import VideoRoutePlan, run_video_route

        print("B 线内存选片（--discover）…", file=sys.stderr)
        route = run_video_route(
            source,
            VideoRoutePlan(max_candidates=args.limit),
            now=now,
            use_db=False,
        )
        return targets_from_video_route(route, plan)
    return None, None


def _format_summary(sample: WindowSample) -> str:
    stats = sample.stats or {}
    lines = [
        "Phase C 窗口采样",
        f"status: {sample.status}",
        f"round_id: {sample.round_id}",
        f"窗口: {iso(sample.window_start)} → {iso(sample.window_end)}"
        f"（{stats.get('window_actual_minutes', '-')} 分钟）",
        f"目标: 评论 {stats.get('comment_target_count', 0)} / 弹幕"
        f" {stats.get('danmaku_target_count', 0)}（{stats.get('target_source')}）",
        f"新评论: {len(sample.comments)} / {stats.get('profile', {}).get('comment_goal', '-')}"
        f"（视频 {stats.get('comment_videos', 0)}）",
        f"新弹幕: {len(sample.danmaku)} / {stats.get('profile', {}).get('danmaku_goal', '-')}"
        f"（视频 {stats.get('danmaku_videos', 0)}）",
    ]
    if stats.get("evidence", True):
        lines.append(
            f"30 天证据: 评论 {len(sample.evidence_comments)} / 弹幕"
            f" {len(sample.evidence_danmaku)}（不参与比例）"
        )
    lines.extend(
        [
            f"请求: {stats.get('http_requests', '-')}  耗时: {stats.get('elapsed_seconds')}s",
            f"写库: {'yes' if stats.get('db_written') else 'no'}"
            + (f"（{stats.get('db_note')}）" if stats.get("db_note") else ""),
            f"degraded_reason: {stats.get('degraded_reason') or '-'}",
        ]
    )
    if sample.errors:
        lines.append(f"错误: {len(sample.errors)}（样例 {sample.errors[0]}）")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.offline and not args.fixture_dir.is_dir():
        print(f"错误：fixture 目录不存在 {args.fixture_dir}", file=sys.stderr)
        return 2
    try:
        plan = _build_plan(args)
        round_id = _parse_round(args.round)
    except SystemExit as exc:  # argparse 风格参数错误
        print(str(exc), file=sys.stderr)
        return 2

    now = int(args.now if args.now is not None else now_ts())
    source = BiliSource(
        offline=bool(args.offline),
        fixture_dir=args.fixture_dir if args.offline else None,
        max_rps=max(0.2, float(args.rps)),
    )
    try:
        comment_targets, danmaku_targets = _load_targets(args, plan, source, now)
        window_start = now - plan.window_seconds if args.window_minutes is not None else None
        window_end = now if args.window_minutes is not None else None
        sample = run_window_sample(
            source,
            plan,
            now=now,
            comment_targets=comment_targets,
            danmaku_targets=danmaku_targets,
            window_start=window_start,
            window_end=window_end,
            use_db=not args.no_db,
            database_url=args.database_url,
            round_id=round_id,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 顶层把错误转成退出码
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1

    sample.stats.setdefault("http_requests", getattr(source.http, "requests", 0))
    payload = sample.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(_format_summary(sample))
    else:
        print(
            f"status={sample.status} comments={len(sample.comments)}"
            f" danmaku={len(sample.danmaku)} round_id={sample.round_id}"
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
