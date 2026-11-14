#!/usr/bin/env python
"""P5 shadow 运行器：3 轮「LLM 抽取 + shadow 审核」，不启服务、不写生产库。

用法：

    .venv/bin/python tools/shadow_run.py                 # 3 轮，/tmp/shadow.db
    .venv/bin/python tools/shadow_run.py --rounds 3 --db /tmp/shadow.db

行为：
- 强制 `HOTMEME_LLM_MODE=shadow`、`HOTMEME_LLM_EXTRACT=1`、`HOTMEME_OFFLINE=0`、
  `HOTMEME_DISABLE_SCHEDULER=1`、`HOTMEME_MAX_RPS=2`（均可在运行环境覆盖前先设置）；
- 每轮完整 `pipeline.run_cycle`：抽取候选 → 规则榜 → shadow 审核（只观察、不改榜）；
- 每轮 stdout 打印一行 JSON，并汇总写 `--json`（默认 /tmp/shadow_report.json）；
- 没有 key：输出 `not_configured` 并 exit 2，**不伪造任何数据、不发请求**；
- 拒绝使用项目 `data/` 下的生产库；默认/推荐路径 `/tmp/shadow.db`。

成本说明：抽取 token 来自 `run_extraction` 审计，审核 token 来自注入的
`ChatClient.stats` 增量；按 ¥1/¥2 每百万 token 估算，仅量级、非报价。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:   # 允许 `python tools/shadow_run.py` 直接运行
    sys.path.insert(0, str(PROJECT_ROOT))
PROD_DB = (PROJECT_ROOT / "data" / "hotmeme.db").resolve()
DEFAULT_DB = Path("/tmp/shadow.db")
DEFAULT_JSON = Path("/tmp/shadow_report.json")
PRICE_INPUT_PER_MTOK = 1.0
PRICE_OUTPUT_PER_MTOK = 2.0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P5 LLM 抽取 + shadow 审核 3 轮观察")
    parser.add_argument("--rounds", type=int, default=3, help="轮数（默认 3）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="SQLite 路径（默认 /tmp/shadow.db，禁止生产库）")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON,
                        help="汇总 JSON 输出路径（默认 /tmp/shadow_report.json）")
    parser.add_argument("--max-rps", type=float, default=2.0,
                        help="采集限速（默认 2.0，合规线）")
    parser.add_argument("--interval", type=float, default=0.0,
                        help="轮间隔秒数（默认 0；warm 缓存 30 分钟内有效）")
    return parser.parse_args(argv)


def _fail(message: str, *, reason: str, code: int = 2) -> int:
    print(json.dumps({"status": reason, "reason": reason, "message": message},
                     ensure_ascii=False))
    return code


def _check_db(path: Path) -> Path | None:
    resolved = path.expanduser().resolve()
    if resolved == PROD_DB or PROD_DB.parent in resolved.parents:
        return None
    return resolved


def _bootstrap_env(args: argparse.Namespace, db_path: Path) -> None:
    """在 import app.* 之前固定环境（config 模块在 import 时读 env）。"""
    os.environ["HOTMEME_LLM_MODE"] = "shadow"
    os.environ["HOTMEME_LLM_EXTRACT"] = "1"
    os.environ["HOTMEME_DB"] = str(db_path)
    os.environ["HOTMEME_OFFLINE"] = "0"
    os.environ["HOTMEME_DISABLE_SCHEDULER"] = "1"
    os.environ["HOTMEME_MAX_RPS"] = str(max(0.1, float(args.max_rps)))


def _cost(prompt_tokens: int, completion_tokens: int) -> float:
    return round(
        (prompt_tokens * PRICE_INPUT_PER_MTOK
         + completion_tokens * PRICE_OUTPUT_PER_MTOK) / 1_000_000,
        6,
    )


def _extract_summary(stats: dict) -> dict:
    names = (
        "reason", "input", "sampled", "calls", "batches", "cache_hits",
        "candidates", "merged", "added", "known", "known_families",
        "known_blocked", "skipped", "prompt_tokens", "completion_tokens",
        "total_tokens",
    )
    summary = {
        name: stats.get(f"llm_extract_{name}")
        for name in names
    }
    summary["candidates_detail"] = list(stats.get("llm_extract_details") or [])
    return summary


def _review_summary(conn, cycle_id: int, stats: dict, token_delta: dict) -> dict:
    rows = conn.execute(
        "SELECT family_key, verdict, meme_prob, reason FROM llm_review "
        "WHERE cycle_id=? ORDER BY verdict, family_key",
        (cycle_id,),
    ).fetchall()
    counts = Counter(str(row["verdict"]) for row in rows)
    pool_details = list(stats.get("llm_pool_details") or [])
    kind_by_key = {
        detail.get("family_key"): detail.get("pool_kind")
        for detail in pool_details
    }
    reviewed = [
        {
            "meme": row["family_key"],
            "verdict": row["verdict"],
            "meme_prob": row["meme_prob"],
            "reason": (row["reason"] or "")[:160],
            "pool_kind": kind_by_key.get(row["family_key"]),
        }
        for row in rows
    ]
    extract_reviewed = [item for item in reviewed if item["pool_kind"] == "extract"]
    approved = [
        {"meme": row["family_key"], "meme_prob": row["meme_prob"],
         "pool_kind": kind_by_key.get(row["family_key"])}
        for row in rows if row["verdict"] == "approve"
    ]
    rejected = [
        {"meme": row["family_key"], "reason": (row["reason"] or "")[:80],
         "pool_kind": kind_by_key.get(row["family_key"])}
        for row in rows if row["verdict"] == "reject"
    ]
    return {
        "mode": stats.get("llm_mode"),
        "calls": stats.get("llm_calls"),
        "rounds": stats.get("llm_rounds"),
        "pool": stats.get("llm_pool"),
        "pool_extract": stats.get("llm_pool_extract"),
        "pool_comment": stats.get("llm_pool_comment"),
        "pool_score": stats.get("llm_pool_score"),
        "extract_in_new": stats.get("llm_extract_new_total"),
        "extract_admitted": stats.get("llm_extract_admitted_total"),
        "reviewed": stats.get("llm_reviewed"),
        "approved": stats.get("llm_approved"),
        "rejected": stats.get("llm_rejected"),
        "gate_applied": stats.get("llm_gate_applied"),
        "fallback": stats.get("llm_fallback"),
        "fallback_reason": stats.get("llm_fallback_reason"),
        "verdicts": dict(counts),
        "pool_details": pool_details,
        "reviewed_items": reviewed,
        "extract_reviewed": extract_reviewed,
        "extract_reviewed_count": len(extract_reviewed),
        "extract_approved_count": sum(
            1 for item in extract_reviewed if item["verdict"] == "approve"
        ),
        "approved_items": approved,
        "rejected_items": rejected,
        "tokens": token_delta,
    }


def _new_list(conn, cycle_id: int, limit: int = 10) -> list[dict]:
    """new 榜实际展示候选（含 rank=0 的观察项），与 `/api/top?list=new` 同口径。"""
    try:
        from app import store

        rows = store.list_items(conn, cycle_id, "new", limit)
    except Exception:  # noqa: BLE001 - 报告字段缺失不影响 shadow 主流程
        return []
    return [
        {
            "rank": item.get("rank"),
            "meme": item.get("canonical"),
            "score": item.get("score"),
            "status": item.get("status"),
            "llm_verdict": (item.get("llm_review") or {}).get("verdict"),
        }
        for item in rows
    ]


def _top10(snapshot: dict) -> list[dict]:
    items = []
    for item in snapshot.get("top") or []:
        review = item.get("llm_review") or {}
        items.append({
            "rank": item.get("rank"),
            "meme": item.get("canonical"),
            "score": item.get("score"),
            "status": item.get("status"),
            "list": item.get("list"),
            "llm_verdict": review.get("verdict"),
        })
    return items


def _token_delta(before: dict, after: dict) -> dict:
    keys = ("calls", "retries", "errors", "prompt_tokens", "completion_tokens",
            "total_tokens")
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in keys}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.rounds < 1:
        return _fail("--rounds 必须 >= 1", reason="bad_args")

    db_path = _check_db(args.db)
    if db_path is None:
        return _fail(
            f"拒绝使用生产库 {PROD_DB}；请改用 /tmp/shadow.db 或其它路径",
            reason="bad_db",
        )

    _bootstrap_env(args, db_path)

    # app.llm 不 import app.config，可先安全检查 key；不打印 key。
    from app.llm import ChatClient, LlmConfig

    llm_config = LlmConfig.from_env()
    if not llm_config.configured:
        return _fail(
            "LLM key 未配置：export HOTMEME_LLM_API_KEY 或写入 "
            "HOTMEME_LLM_API_KEY_FILE（chmod 600）；未执行任何轮次、未发请求",
            reason="not_configured",
        )

    # key 已配置后才 import 读取 env 的采集/流水线模块。
    from app import pipeline, store
    from app.bili import BiliClient

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = store.connect(db_path)
    store.init_db(conn)
    print(json.dumps({
        "status": "start", "mode": "shadow", "extract": "on",
        "db": str(db_path), "rounds": args.rounds,
        "llm": llm_config.redacted(),
    }, ensure_ascii=False))

    results: list[dict] = []
    totals = {
        "extract_calls": 0, "extract_candidates": 0, "extract_added": 0,
        "extract_merged": 0, "extract_known": 0,
        "review_calls": 0, "approved": 0, "rejected": 0,
        "prompt_tokens": 0, "completion_tokens": 0,
        "elapsed": 0.0,
    }
    try:
        for index in range(1, args.rounds + 1):
            # 每轮新建 ChatClient：它的 max_calls 是「进程内累计」硬上限，
            # 跨轮复用会让第 3 轮把累计 60 次调用误报成审核预算耗尽（P6 实测）。
            reviewer = ChatClient(llm_config)
            before_stats = reviewer.stats_dict()
            started = time.time()
            snapshot = pipeline.run_cycle(conn=conn, client=BiliClient(),
                                          reviewer=reviewer)
            after_stats = reviewer.stats_dict()
            token_delta = _token_delta(before_stats, after_stats)
            cycle = snapshot.get("cycle") or {}
            stats = cycle.get("stats") or {}
            extract = _extract_summary(stats)
            review = _review_summary(conn, int(cycle.get("id") or 0), stats,
                                     token_delta)
            elapsed = round(time.time() - started, 1)
            extract_prompt = int(stats.get("llm_extract_prompt_tokens") or 0)
            extract_completion = int(stats.get("llm_extract_completion_tokens") or 0)
            cost = {
                "extract_cny": _cost(extract_prompt, extract_completion),
                "review_cny": _cost(token_delta.get("prompt_tokens", 0),
                                    token_delta.get("completion_tokens", 0)),
                "price_assumption_cny_per_mtok": {
                    "input": PRICE_INPUT_PER_MTOK,
                    "output": PRICE_OUTPUT_PER_MTOK,
                },
                "note": "量级估算，不是报价；审核 token 为注入 ChatClient 的增量",
            }
            cost["total_cny"] = round(cost["extract_cny"] + cost["review_cny"], 6)
            entry = {
                "round": index,
                "cycle_id": cycle.get("id"),
                "status": cycle.get("status"),
                "elapsed_seconds": elapsed,
                "stats": {
                    key: stats.get(key) for key in (
                        "videos", "comments", "comments_recent", "comments_7d",
                        "requests", "candidates", "families", "usable",
                        "comment_evidence_ready", "comment_degraded",
                    )
                },
                "extract": extract,
                "review": review,
                "top10": _top10(snapshot),
                "new_list": _new_list(conn, int(cycle.get("id") or 0)),
                "cost": cost,
            }
            results.append(entry)
            print(json.dumps(entry, ensure_ascii=False))

            totals["extract_calls"] += int(extract.get("calls") or 0)
            totals["extract_candidates"] += int(extract.get("candidates") or 0)
            totals["extract_added"] += int(extract.get("added") or 0)
            totals["extract_merged"] += int(extract.get("merged") or 0)
            totals["extract_known"] += int(extract.get("known") or 0)
            totals["review_calls"] += int(review.get("calls") or 0)
            totals["approved"] += int(review.get("approved") or 0)
            totals["rejected"] += int(review.get("rejected") or 0)
            totals["prompt_tokens"] += extract_prompt + int(
                token_delta.get("prompt_tokens") or 0)
            totals["completion_tokens"] += extract_completion + int(
                token_delta.get("completion_tokens") or 0)
            totals["elapsed"] += elapsed

            if args.interval > 0 and index < args.rounds:
                time.sleep(args.interval)
    except Exception as exc:  # noqa: BLE001 - 脚本层兜底，保留已完成轮次
        report = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "shadow",
            "db": str(db_path),
            "llm": llm_config.redacted(),
            "status": "failed",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "rounds": results,
            "totals": totals,
        }
        _write_json(args.json, report)
        print(json.dumps({"status": "failed", "error": report["error"],
                          "json": str(args.json)}, ensure_ascii=False))
        return 1
    finally:
        # sqlite3.Connection 没有 `.closed` 属性（P6 崩溃根因）；close() 本身幂等。
        conn.close()

    totals["cost_cny"] = _cost(totals["prompt_tokens"],
                               totals["completion_tokens"])
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "shadow",
        "db": str(db_path),
        "llm": llm_config.redacted(),
        "status": "ok",
        "rounds": results,
        "totals": totals,
    }
    _write_json(args.json, report)
    print(json.dumps({"status": "ok", "rounds": len(results),
                      "json": str(args.json)}, ensure_ascii=False))
    return 0


def _write_json(path: Path, payload: dict) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
