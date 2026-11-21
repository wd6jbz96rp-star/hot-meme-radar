"""Phase E CLI：把 Phase D 池状态生成为报告 JSON v2（不接 worker、不切线上）。

用法示例::

    # 真实模式：从 Postgres 读 latest round + pool1/pool2 → 报告（只读，不写库）
    python -m tools.build_report --round latest --out /tmp/hotmeme_report_v2.json

    # 指定 round id
    python -m tools.build_report --round 29 --out /tmp/hotmeme_report_v2.json

    # 回放/测试：Phase D 的 PoolRoundResult JSON（--json 落盘）→ 报告，不连库
    python -m tools.build_report --no-db --pool-json /tmp/pool_round.json \
        --out /tmp/hotmeme_report_v2.json --json

    # 也可以从 stdin 读（管道）
    python -m tools.pool_round --no-db --window-json w.json --json \
      | python -m tools.build_report --no-db --pool-json - --out /tmp/hotmeme_report_v2.json

原子写：只有报告序列化 + tmp 写成功后才 ``os.replace`` 覆盖目标文件；失败不碰旧文件。

退出码：0 = 成功；1 = 运行失败；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

# 直接 `python tools/build_report.py` 时，sys.path[0] 是 tools/，会被同目录的
# tools/meme_info.py 遮蔽包 `meme_info`；显式把仓库根放最前（同 meme_info_shadow）。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meme_info.models import now_ts  # noqa: E402
from meme_info.report import (  # noqa: E402
    report_from_db,
    report_from_mapping,
    write_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.build_report",
        description="Phase E：从 Phase D 池状态生成报告 JSON v2（只读）",
    )
    parser.add_argument(
        "--round",
        default="latest",
        help="轮次：latest（默认）或 round id；--no-db 时忽略",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="报告输出路径（原子写）；缺省只打印/摘要，不落盘",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="不连 Postgres：从 --pool-json 读 Phase D PoolRoundResult JSON",
    )
    parser.add_argument(
        "--pool-json",
        default=None,
        help="PoolRoundResult JSON（Phase D --json 输出）；'-' 表示 stdin；仅 --no-db 用",
    )
    parser.add_argument(
        "--database-url", default=None, help="显式数据库连接串（缺省用 DATABASE_URL）"
    )
    parser.add_argument(
        "--now", type=int, default=None, help="固定当前时间戳（调试/可复算）"
    )
    parser.add_argument(
        "--generated-at", default=None, help="覆盖报告 generated_at（调试/回放）"
    )
    parser.add_argument("--json", action="store_true", help="输出完整报告 JSON")
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


def _read_pool_json(source: str) -> Any:
    if source == "-":
        return json.load(sys.stdin)
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"PoolRoundResult JSON 不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _format_summary(report: dict[str, Any]) -> str:
    stats = report.get("stats") or {}
    counts = report.get("counts") or {}
    round_info = report.get("round") or {}
    verification = report.get("verification") or {}
    lines = [
        "Phase E 报告 v2",
        f"schema: {report.get('schema_version')}  status: {report.get('status')}",
        f"round: {round_info.get('id')}"
        f"  窗口: {round_info.get('window_start')} → {round_info.get('window_end')}",
        f"counts: pool1={counts.get('pool1')}"
        f"（其中 pool2={counts.get('pool2')} / 仅 p1={counts.get('pool1_only')}）"
        f"  pool1_out={counts.get('pool1_out')}  观察={counts.get('observations')}",
        f"stats: 评论 {stats.get('comment_count')} / 弹幕 {stats.get('danmaku_count')}"
        f" / family {stats.get('families')}  degraded={stats.get('degraded')}",
        f"LLM: configured={verification.get('configured')}"
        f" calls={verification.get('calls')}",
        f"memes(pool2): {len(report.get('memes') or [])} 条"
        f"   pool1: {len(report.get('pool1') or [])} 条",
    ]
    hot = (report.get("memes") or [])[:5]
    if hot:
        lines.append("heat Top5:")
        for entry in hot:
            lines.append(
                f"  #{entry.get('rank')}  heat={entry.get('heat_score'):6.2f}"
                f"  score={entry.get('score'):6.2f}  {entry.get('canonical')}"
            )
    else:
        lines.append("pool2 为空：新梗榜本轮 0 条（不 top-up、不伪造）")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        round_id = _parse_round(args.round)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.no_db and not args.pool_json:
        print("错误：--no-db 需要 --pool-json（Phase D PoolRoundResult JSON）", file=sys.stderr)
        return 2
    if not args.no_db and args.pool_json:
        print("错误：--pool-json 只能和 --no-db 一起用", file=sys.stderr)
        return 2

    try:
        if args.no_db:
            try:
                payload = _read_pool_json(args.pool_json)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"错误：读取 PoolRoundResult JSON 失败：{exc}", file=sys.stderr)
                return 2
            report = report_from_mapping(
                payload,
                generated_at=args.generated_at,
                now=args.now if args.now is not None else now_ts(),
            )
        else:
            report = report_from_db(
                round_id=round_id,
                url=args.database_url,
                generated_at=args.generated_at,
                now=args.now if args.now is not None else now_ts(),
            )
    except Exception as exc:  # noqa: BLE001 - CLI 顶层把错误转成退出码
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1

    if args.out is not None:
        try:
            size = write_report(report, args.out)
        except Exception as exc:  # noqa: BLE001
            print(f"写报告失败（旧报告未覆盖）：{exc}", file=sys.stderr)
            return 1
    else:
        size = 0

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(_format_summary(report))
        if args.out is not None:
            print(f"已写入 {args.out}（{size} bytes，原子替换）")
    else:
        counts = report.get("counts") or {}
        print(
            f"status={report.get('status')}"
            f" round={(report.get('round') or {}).get('id')}"
            f" pool1={counts.get('pool1')} pool2={counts.get('pool2')}"
            f" memes={len(report.get('memes') or [])}"
            + (f" out={args.out}" if args.out is not None else "")
        )
    if report.get("status") == "degraded":
        print("警告：本轮 degraded，报告保留 pool2 成员、不新增 promote；adapter 将标降级",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
