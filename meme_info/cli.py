"""meme_info 命令行：采集 / 发现 / 打分 / 端到端查询 / 评测 / LLM 审核。

所有命令都不写数据库；``query`` 真机验证请用 ``--cache-dir /tmp/... --out /tmp/...``。
无 LLM key 时 ``extract/review`` 明确 not_configured 并 exit 2，不伪造结果。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .bili import BiliSource
from .collect import CollectionPlan, collect_corpus
from .discover import DiscoveryConfig, extract_candidates, extract_meta_mentions, merge_families
from .evaluate import evaluate, render_markdown
from .heat import HeatConfig, HeatModel
from .knowledge import Knowledge
from .models import Corpus, HotMemeReport, as_jsonable, iso, now_ts
from .query import QueryConfig, find_hot_memes, history_from_report, load_history, query_from_corpus
from .verify import ChatClient, SemanticVerifier, extract_candidates as llm_extract
from .verify import review_candidates as llm_review


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: str | None, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if not path:
        print(text)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)


def _report_from_payload(payload: Any) -> dict:
    if isinstance(payload, dict):
        return payload
    return {}


def _corpus_from_payload(payload: Any) -> Corpus:
    if isinstance(payload, dict) and "videos" in payload and "docs" in payload:
        return Corpus.from_dict(payload)
    raise SystemExit("输入不是 Corpus JSON（需要 videos/docs 字段）")


def _source(args) -> BiliSource:
    offline = bool(getattr(args, "offline_fixtures", None))
    fixture_dir = Path(args.offline_fixtures) if offline else None
    return BiliSource(
        cache_dir=getattr(args, "cache_dir", None),
        offline=offline,
        fixture_dir=fixture_dir,
        max_rps=getattr(args, "max_rps", 2.0),
    )


def _plan(args) -> CollectionPlan:
    return CollectionPlan(
        comment_videos=getattr(args, "comment_videos", 30),
        max_requests=getattr(args, "max_requests", 260),
        enable_search=not getattr(args, "no_search", False),
        search_queries=tuple(getattr(args, "search_queries", None) or CollectionPlan().search_queries),
        comment_window_days=getattr(args, "window_days", 30),
        danmaku_videos=getattr(args, "danmaku_videos", 8),
    )


def _query_config(args) -> QueryConfig:
    return QueryConfig(
        plan=_plan(args),
        heat=HeatConfig(),
        discovery=DiscoveryConfig(),
        limit=getattr(args, "limit", 20),
        expand_queries=not getattr(args, "no_expand", False),
        expand_limit=getattr(args, "expand_limit", 10),
        verify=bool(getattr(args, "verify", False)),
        verify_gate=bool(getattr(args, "verify_gate", False)),
        verify_limit=getattr(args, "verify_limit", 12),
    )


def _now(args) -> int:
    value = getattr(args, "now", None)
    if not value:
        return now_ts()
    try:
        return int(value)
    except ValueError:
        from .models import parse_iso

        parsed = parse_iso(value)
        return parsed or now_ts()


def _query_markdown(report: HotMemeReport) -> str:
    lines = [
        f"## 当前热梗 Top{len(report.memes)}（{report.as_of}）",
        "",
        "| # | 梗 | 热度 | 状态 | 元视频 | 使用证据 | 理由 |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, meme in enumerate(report.memes, start=1):
        signals = meme.signals
        lines.append(
            f"| {index} | {meme.canonical} | {meme.score:.1f} | {meme.heat_level} | "
            f"{signals.get('meta_videos', 0)} | {signals.get('organic_docs', 0)} | "
            f"{'；'.join(meme.reasons[:2])} |"
        )
    return "\n".join(lines)


def _print_query_summary(report: HotMemeReport) -> None:
    stats = report.stats
    print(
        f"热点查询完成：{stats.get('videos', 0)} 视频 / {stats.get('comments', 0)} 评论 / "
        f"{stats.get('danmaku', 0)} 弹幕；候选 {stats.get('discovery_candidates', 0)}，"
        f"梗族 {stats.get('families', 0)}，当前 {stats.get('current_families', 0)}；"
        f"请求 {stats.get('requests_total', stats.get('requests', 0))}，耗时 {stats.get('elapsed_total', stats.get('elapsed', '?'))}s"
    )
    if not report.memes:
        print("没有达到证据门槛的当前热梗。")
        return
    print(f"{'#':>2}  {'梗':<18} {'分':>6}  {'状态':<10} {'证据':<20} 理由")
    for index, meme in enumerate(report.memes, start=1):
        signals = meme.signals
        evidence = f"{signals.get('meta_videos', 0)}视频/{signals.get('organic_docs', 0)}评论"
        reason = "；".join(meme.reasons[:2])
        if len(reason) > 60:
            reason = reason[:59] + "…"
        print(
            f"{index:>2}  {meme.canonical[:18]:<18} {meme.score:>6.1f}  "
            f"{meme.status:<10} {evidence:<20} {reason}"
        )


# ---------------------------------------------------------------- 子命令
def cmd_query(args) -> int:
    config = _query_config(args)
    source = _source(args)
    verifier = None
    if config.verify:
        llm = ChatClient(cache_dir=args.cache_dir)
        if not llm.config.configured:
            print("not_configured：未找到 LLM key，--verify 不执行；规则榜照常。", file=sys.stderr)
        else:
            verifier = SemanticVerifier(llm, cache_dir=args.cache_dir)
    history = load_history(args.history)
    report = find_hot_memes(
        source,
        config=config,
        now=_now(args),
        history=history or None,
        verifier=verifier,
    )
    payload = report.to_dict()
    _write_json(args.out, payload)
    if not args.quiet:
        _print_query_summary(report)
    if args.format == "md":
        print(_query_markdown(report))
    return 0


def cmd_collect(args) -> int:
    source = _source(args)
    corpus = collect_corpus(source, _plan(args), now=_now(args))
    _write_json(args.out, corpus.to_dict())
    if not args.quiet:
        stats = corpus.stats
        print(
            f"采集完成：{stats.get('videos')} 视频 / {stats.get('comments')} 评论 / "
            f"{stats.get('danmaku')} 弹幕；请求 {stats.get('requests')}，耗时 {stats.get('elapsed')}s"
        )
        if stats.get("budget_exhausted"):
            print("提示：请求预算已用尽，后续通道被截断。", file=sys.stderr)
    return 0


def cmd_discover(args) -> int:
    corpus = _corpus_from_payload(_read_json(args.corpus))
    now = _now(args)
    knowledge = Knowledge(args.data_dir)
    mentions = extract_meta_mentions(corpus.videos, knowledge, now=now)
    candidates = extract_candidates(
        corpus.docs, knowledge, now=now, meta_mentions=mentions, config=DiscoveryConfig()
    )
    memes = merge_families(candidates, knowledge, now=now, meta_mentions=mentions)
    payload = {
        "generated_at": iso(now),
        "meta_mentions": as_jsonable(mentions),
        "candidates": as_jsonable(candidates),
        "families": [
            {
                "family_key": meme.family_key,
                "canonical": meme.canonical,
                "type": meme.type,
                "members": meme.members,
                "known": meme.known,
                "meme_prob": meme.meme_prob,
                "confidence": meme.confidence,
                "usage": len(meme.usage),
                "meta": len(meme.meta),
                "first_seen": iso(meme.first_seen),
                "last_seen": iso(meme.last_seen),
            }
            for meme in memes
        ],
    }
    _write_json(args.out, payload)
    if not args.quiet:
        print(f"发现完成：{len(mentions)} 条元话语，{len(candidates)} 候选，{len(memes)} 梗族")
    return 0


def cmd_score(args) -> int:
    corpus = _corpus_from_payload(_read_json(args.corpus))
    now = _now(args)
    knowledge = Knowledge(args.data_dir)
    mentions = extract_meta_mentions(corpus.videos, knowledge, now=now)
    candidates = extract_candidates(corpus.docs, knowledge, now=now, meta_mentions=mentions)
    memes = merge_families(candidates, knowledge, now=now, meta_mentions=mentions)
    model = HeatModel(HeatConfig())
    scored = model.score_memes(
        memes, now=now, history=load_history(args.history) or None, knowledge=knowledge
    )
    payload = {
        "generated_at": iso(now),
        "memes": as_jsonable(scored[: args.limit]),
        "all": as_jsonable(scored),
    }
    _write_json(args.out, payload)
    if not args.quiet:
        print(f"打分完成：{len(scored)} 梗族，当前有效 {sum(1 for m in scored if m.status != 'rejected')}")
    return 0


def cmd_eval(args) -> int:
    payload = _read_json(args.pred)
    if args.pool == "all" and isinstance(payload, dict) and "all" in payload:
        predictions = payload["all"]
    elif isinstance(payload, dict) and "memes" in payload:
        predictions = payload["memes"]
    elif isinstance(payload, dict) and "candidates" in payload:
        predictions = payload["candidates"]
    else:
        predictions = payload
    labels = _read_json(args.labels)
    report = evaluate(predictions, labels, k=args.k)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.markdown:
        print("\n" + render_markdown(report))
    if args.out:
        _write_json(args.out, report)
    return 0


def cmd_review(args) -> int:
    payload = _read_json(args.candidates)
    candidates = payload.get("candidates") if isinstance(payload, dict) else payload
    llm = ChatClient(cache_dir=args.cache_dir)
    if not llm.config.configured:
        print("not_configured：没有 LLM key（MEME_INFO_LLM_API_KEY[_FILE]）", file=sys.stderr)
        return 2
    result = llm_review(
        candidates or [],
        llm,
        max_rounds=args.max_rounds,
        target=args.target,
        max_calls=args.max_calls,
        now=_now(args),
    )
    _write_json(args.out, result)
    if not args.quiet:
        print(
            f"审核：approve={len(result['approved'])} reject={len(result['rejected'])} "
            f"unclear={len(result['unclear'])} calls={result['calls']} reason={result['reason']}"
        )
    return 0


def cmd_extract(args) -> int:
    comments = _read_json(args.comments)
    if isinstance(comments, dict):
        comments = comments.get("comments") or comments.get("items") or []
    llm = ChatClient(cache_dir=args.cache_dir)
    if not llm.config.configured:
        print("not_configured：没有 LLM key（MEME_INFO_LLM_API_KEY[_FILE]）", file=sys.stderr)
        return 2
    result = llm_extract(
        comments,
        llm,
        max_calls=args.max_calls,
        max_comments=args.max_comments,
        knowledge=Knowledge(args.data_dir),
        now=_now(args),
    )
    _write_json(args.out, result)
    if not args.quiet:
        print(f"抽取：{len(result['candidates'])} 候选，calls={result['calls']}，reason={result['reason']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.meme_info", description="热梗信息模块 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(item, *, now=True, cache=True):
        item.add_argument("--data-dir", default=None, help="知识库 data/ 目录")
        if now:
            item.add_argument("--now", default=None, help="ISO 时间或 Unix 秒；用于回放")
        if cache:
            item.add_argument("--cache-dir", default=os.getenv("MEME_INFO_CACHE_DIR") or None,
                              help="HTTP 缓存目录（真机验证建议 /tmp）")
        item.add_argument("--quiet", action="store_true")

    query = sub.add_parser("query", help="端到端：采集 + 发现 + 热度 + 报告")
    common(query)
    query.add_argument("--out", default=None)
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--comment-videos", type=int, default=30)
    query.add_argument("--danmaku-videos", type=int, default=8)
    query.add_argument("--max-requests", type=int, default=260)
    query.add_argument("--window-days", type=int, default=30)
    query.add_argument("--no-search", action="store_true")
    query.add_argument("--no-expand", action="store_true")
    query.add_argument("--expand-limit", type=int, default=10)
    query.add_argument("--verify", action="store_true")
    query.add_argument("--verify-gate", action="store_true", help="按 LLM 结论剔除（默认只观察）")
    query.add_argument("--verify-limit", type=int, default=12)
    query.add_argument("--history", default=None)
    query.add_argument("--offline-fixtures", default=None)
    query.add_argument("--max-rps", type=float, default=2.0)
    query.add_argument("--format", choices=["summary", "md"], default="summary")
    query.set_defaults(func=cmd_query)

    collect = sub.add_parser("collect", help="只采集语料，输出 Corpus JSON")
    common(collect)
    collect.add_argument("--out", required=True)
    collect.add_argument("--comment-videos", type=int, default=30)
    collect.add_argument("--danmaku-videos", type=int, default=8)
    collect.add_argument("--max-requests", type=int, default=260)
    collect.add_argument("--window-days", type=int, default=30)
    collect.add_argument("--no-search", action="store_true")
    collect.add_argument("--offline-fixtures", default=None)
    collect.add_argument("--max-rps", type=float, default=2.0)
    collect.set_defaults(func=cmd_collect)

    discover = sub.add_parser("discover", help="从 Corpus 抽候选与梗族")
    common(discover)
    discover.add_argument("--corpus", required=True)
    discover.add_argument("--out", required=True)
    discover.set_defaults(func=cmd_discover)

    score = sub.add_parser("score", help="从 Corpus 打分")
    common(score)
    score.add_argument("--corpus", required=True)
    score.add_argument("--out", required=True)
    score.add_argument("--history", default=None)
    score.add_argument("--limit", type=int, default=50)
    score.set_defaults(func=cmd_score)

    eval_cmd = sub.add_parser("eval", help="评测预测与标签")
    eval_cmd.add_argument("--pred", required=True)
    eval_cmd.add_argument("--labels", required=True)
    eval_cmd.add_argument("--k", type=int, default=10)
    eval_cmd.add_argument("--pool", choices=["memes", "all", "candidates"], default="memes")
    eval_cmd.add_argument("--out", default=None)
    eval_cmd.add_argument("--markdown", action="store_true")
    eval_cmd.set_defaults(func=cmd_eval)

    review = sub.add_parser("review", help="LLM 审核候选（交接单接口）")
    common(review)
    review.add_argument("--candidates", required=True)
    review.add_argument("--out", default=None)
    review.add_argument("--max-calls", type=int, default=20)
    review.add_argument("--max-rounds", type=int, default=2)
    review.add_argument("--target", type=int, default=10)
    review.set_defaults(func=cmd_review)

    extract = sub.add_parser("extract", help="LLM 从评论抽候选（交接单接口）")
    common(extract)
    extract.add_argument("--comments", required=True)
    extract.add_argument("--out", default=None)
    extract.add_argument("--max-calls", type=int, default=3)
    extract.add_argument("--max-comments", type=int, default=450)
    extract.set_defaults(func=cmd_extract)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
