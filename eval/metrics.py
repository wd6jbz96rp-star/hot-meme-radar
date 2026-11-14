#!/usr/bin/env python3
"""hot_meme_search v2 P0 评测指标（只读）。

用法：
    python eval/metrics.py --db data/hotmeme.db --cycle 7
    python eval/metrics.py --db data/hotmeme.db            # 默认最新已完成 cycle
    python eval/metrics.py --format json | --format md

约束：
- 只用标准库，不联网、不装依赖、不启动服务。
- 数据库以 SQLite URI ``file:<path>?mode=ro`` 只读打开，并设置 ``PRAGMA query_only=ON``。
- 默认库路径按脚本位置推导（<repo>/data/hotmeme.db），可用 ``--db`` / ``HOTMEME_DB`` 覆盖，
  不硬编码生产库绝对路径。

输出：JSON（完整结构）+ Markdown 表格。JSON 字段见 ``build_report``。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("HOTMEME_DB") or (REPO_ROOT / "data" / "hotmeme.db"))
DEFAULT_SEED = REPO_ROOT / "data" / "seed_memes.json"
DEFAULT_GOLDEN = REPO_ROOT / "tests" / "golden" / "memes.json"

# 基线标签快照（手工标注，口径见 docs/pm/EVAL-DESIGN.md）。
# 只覆盖 2026-11-14 cycle#7 Top10；不在表内的条目一律按 unclear 计，避免臆造。
# 扩展标签 recent_meme：覆盖「1–3 年内出现、仍活跃、未常青」的梗；严格 30 天新梗本轮为 0。
LABELS = {
    "抽象": "evergreen",
    "咕咕嘎嘎": "recent_meme",
    "蚌埠住了": "evergreen",
    "哈基米": "recent_meme",
    "嘉豪": "proper_noun",
    "今夕是何年": "unclear",
    "你礼貌吗": "evergreen",
    "麻了": "evergreen",
    "整活": "evergreen",
    "吃瓜": "evergreen",
}
# 纯常青 / 已泛化老梗的细分（用于与 QA-QUALITY 的 60% / 20% 双口径对照）
PURE_EVERGREEN = {"吃瓜", "麻了"}
GENERALIZED_EVERGREEN = {"抽象", "蚌埠住了", "整活", "你礼貌吗"}
# 与 P0-BASELINE.md 一致的证据置信度：A=库内可验证，B=规则/外部年代推断，C=证据不足
CONFIDENCE = {
    "抽象": "A",
    "咕咕嘎嘎": "B",
    "蚌埠住了": "A",
    "哈基米": "B",
    "嘉豪": "B",
    "今夕是何年": "C",
    "你礼貌吗": "A",
    "麻了": "A",
    "整活": "A",
    "吃瓜": "A",
}

TOP10_LIMIT = 10
YEAR_RE = re.compile(r"(?:^|\D)((?:19|20)\d{2})(?:\D|$)")


def parse_year(text: str | None) -> int | None:
    """从 origin 字符串（如 '2023 离谱好笑'）取年份。"""
    if not text:
        return None
    m = YEAR_RE.search(text.strip())
    return int(m.group(1)) if m else None


def connect_ro(path: Path) -> sqlite3.Connection:
    """严格只读打开；不存在则报错，不创建空库。"""
    if not path.is_file():
        raise SystemExit(f"[metrics] 数据库不存在：{path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def resolve_cycle(conn: sqlite3.Connection, cycle: int | None) -> int:
    if cycle is not None:
        row = conn.execute("SELECT id FROM cycle WHERE id=?", (cycle,)).fetchone()
        if row is None:
            raise SystemExit(f"[metrics] cycle#{cycle} 不存在")
        return int(row["id"])
    row = conn.execute(
        "SELECT id FROM cycle WHERE status='done' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM cycle ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("[metrics] cycle 表为空")
    return int(row["id"])


def load_seed(path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    memes = data["memes"] if isinstance(data, dict) else data
    by_name: dict[str, dict] = {}
    alias_to_canonical: dict[str, str] = {}
    for m in memes:
        canonical = str(m.get("canonical") or "").strip()
        if not canonical:
            continue
        origin = str(m.get("origin") or "").strip()
        rec = {
            "canonical": canonical,
            "origin": origin,
            "origin_year": parse_year(origin),
            "type": m.get("type") or "",
        }
        by_name[canonical] = rec
        for alias in m.get("aliases") or []:
            alias = str(alias).strip()
            if alias and alias not in by_name:
                alias_to_canonical[alias] = canonical
    return by_name, alias_to_canonical


def seed_name(name: str, by_name: dict[str, dict], alias_map: dict[str, str]) -> str | None:
    if name in by_name:
        return name
    return alias_map.get(name)


def seed_year_dist(by_name: dict[str, dict], memes: list[dict]) -> dict:
    """种子表按 origin 年份分布；非 2020–2025 单列为 extras，不硬塞进桶。"""
    buckets = {str(y): 0 for y in range(2020, 2026)}
    buckets["未知"] = 0
    extras: dict[str, int] = {}
    for m in memes:
        y = parse_year(str(m.get("origin") or ""))
        if y is None:
            buckets["未知"] += 1
        elif 2020 <= y <= 2025:
            buckets[str(y)] += 1
        else:
            extras[str(y)] = extras.get(str(y), 0) + 1
    total = len(memes)
    classic = sum(1 for m in memes if (y := parse_year(str(m.get("origin") or ""))) and y <= 2023)
    recent = sum(1 for m in memes if (y := parse_year(str(m.get("origin") or ""))) and y >= 2024)
    unknown = buckets["未知"]
    assert classic + recent + unknown == total, (classic, recent, unknown, total)
    return {
        "total": total,
        "by_year": buckets,
        "extras_outside_2020_2025": dict(sorted(extras.items())),
        "classic_le_2023": classic,
        "recent_ge_2024": recent,
        "unknown": unknown,
        "classic_ratio": round(classic / total, 4) if total else 0.0,
        "recent_ratio": round(recent / total, 4) if total else 0.0,
    }


def load_top10(conn: sqlite3.Connection, cycle_id: int) -> list[dict]:
    """榜单 Top10 = rank 1..10（注意：observing 也可能带 rank 进榜）。"""
    rows = conn.execute(
        """
        SELECT s.rank, s.family_key, s.score, s.status, s.is_new, s.sample,
               s.components_json, m.id AS meme_id, m.canonical, m.type,
               m.origin, m.first_seen
        FROM meme_signal s
        LEFT JOIN meme m ON m.id = s.meme_id
        WHERE s.cycle_id = ? AND s.rank BETWEEN 1 AND ?
        ORDER BY s.rank
        """,
        (cycle_id, TOP10_LIMIT),
    ).fetchall()
    out = []
    for r in rows:
        try:
            comp = json.loads(r["components_json"] or "{}")
        except json.JSONDecodeError:
            comp = {}
        reasons = comp.get("reasons") or {}
        out.append(
            {
                "rank": r["rank"],
                "family_key": r["family_key"],
                "canonical": r["canonical"] or r["family_key"],
                "meme_id": r["meme_id"],
                "score": round(float(r["score"]), 2) if r["score"] is not None else None,
                "status": r["status"],
                "is_new": bool(r["is_new"]),
                "sample": r["sample"] or "",
                "type": r["type"] or "",
                "origin_raw": r["origin"] or "",
                "first_seen": r["first_seen"] or "",
                "components": {
                    k: comp.get(k)
                    for k in ("exposure", "engagement", "breadth", "velocity", "meme", "decay")
                },
                "reasons": reasons,
                "seed_hit_db": "seed_hit" in reasons,
            }
        )
    return out


def candidate_evidence(conn: sqlite3.Connection, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT norm, surface, first_seen, df_video, df_zone, total_count "
        f"FROM candidate WHERE norm IN ({placeholders})",
        keys,
    ).fetchall()
    return {
        r["norm"]: {
            "surface": r["surface"],
            "first_seen": r["first_seen"],
            "df_video": r["df_video"],
            "df_zone": r["df_zone"],
            "total_count": r["total_count"],
        }
        for r in rows
    }


def family_alias_rows(conn: sqlite3.Connection, meme_ids: list[int]) -> list[dict]:
    ids = [i for i in meme_ids if i is not None]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT meme_id, surface FROM meme_alias WHERE meme_id IN ({placeholders}) ORDER BY meme_id",
        ids,
    ).fetchall()
    return [{"meme_id": r["meme_id"], "surface": r["surface"]} for r in rows]


def same_family_check(top: list[dict], alias_rows: list[dict]) -> dict:
    keys = [t["family_key"] for t in top]
    by_id = {t["meme_id"]: t["canonical"] for t in top}
    # 同族重复：family_key 重复，或某条目的别名等于另一条目的 canonical
    dup_keys = sorted({k for k in keys if keys.count(k) > 1})
    alias_overlap = []
    canon = {t["canonical"] for t in top}
    for a in alias_rows:
        owner = by_id.get(a["meme_id"])
        if owner and a["surface"] in canon and a["surface"] != owner:
            alias_overlap.append({"owner": owner, "alias": a["surface"]})
    return {
        "distinct_families": len(set(keys)),
        "top_n": len(top),
        "duplicate_family_keys": dup_keys,
        "alias_overlap_pairs": alias_overlap,
        "has_duplicate": bool(dup_keys or alias_overlap),
    }


def age_years(origin_year: int, as_of: dt.date) -> float:
    """年份粒度 origin 的口径：以当年 7 月 1 日为中点估算年龄（年）。"""
    mid = dt.date(origin_year, 7, 1)
    return round((as_of - mid).days / 365.25, 2)


def build_report(
    conn: sqlite3.Connection,
    cycle_id: int,
    seed_path: Path,
    golden_path: Path,
    as_of: dt.date,
) -> dict:
    cycle = conn.execute(
        "SELECT id, started_at, finished_at, status, stats_json FROM cycle WHERE id=?",
        (cycle_id,),
    ).fetchone()
    if cycle is None:
        raise SystemExit(f"[metrics] cycle#{cycle_id} 不存在")
    try:
        stats = json.loads(cycle["stats_json"] or "{}")
    except json.JSONDecodeError:
        stats = {}

    seed_memes = json.loads(seed_path.read_text(encoding="utf-8"))["memes"]
    by_name, alias_map = load_seed(seed_path)
    dist = seed_year_dist(by_name, seed_memes)

    top = load_top10(conn, cycle_id)
    cand = candidate_evidence(conn, [t["family_key"] for t in top])
    alias_rows = family_alias_rows(conn, [t["meme_id"] for t in top])

    for item in top:
        name = item["canonical"]
        sname = seed_name(name, by_name, alias_map) or seed_name(item["family_key"], by_name, alias_map)
        item["seed_name"] = sname
        item["seed_hit_seedfile"] = sname is not None
        item["seed_hit"] = bool(item["seed_hit_db"] or item["seed_hit_seedfile"])
        seed_origin = by_name.get(sname, {}).get("origin") if sname else None
        year = parse_year(item["origin_raw"]) or parse_year(seed_origin)
        item["origin_year"] = year
        item["origin_source"] = (
            "meme.origin" if parse_year(item["origin_raw"]) else ("seed.origin" if year else "unknown")
        )
        item["origin_age_years"] = age_years(year, as_of) if year else None
        c = cand.get(item["family_key"], {})
        item["evidence"] = {
            "sample": item["sample"],
            "df_video": c.get("df_video"),
            "df_zone": c.get("df_zone"),
            "total_count": c.get("total_count"),
            "reasons": item["reasons"],
            "meme_score": item["components"].get("meme"),
            "velocity": item["components"].get("velocity"),
        }
        item["label"] = LABELS.get(name, "unclear")
        item["label_confidence"] = CONFIDENCE.get(name, "C")

    n = len(top)
    seed_hits = sum(1 for t in top if t["seed_hit"])
    years = [t["origin_year"] for t in top if t["origin_year"]]
    ages = [t["origin_age_years"] for t in top if t["origin_age_years"] is not None]
    label_counts: dict[str, int] = {}
    for t in top:
        label_counts[t["label"]] = label_counts.get(t["label"], 0) + 1

    # 候选池（cycle 视图 + 当前表快照分开，避免把累计表当成单轮口径）
    usable_rows = conn.execute(
        "SELECT components_json, is_new FROM meme_signal WHERE cycle_id=?", (cycle_id,)
    ).fetchall()
    non_seed_usable = 0
    for r in usable_rows:
        try:
            reasons = (json.loads(r["components_json"] or "{}").get("reasons") or {})
        except json.JSONDecodeError:
            reasons = {}
        if "seed_hit" not in reasons:
            non_seed_usable += 1
    usable = len(usable_rows)
    table_meme_total = conn.execute("SELECT COUNT(*) FROM meme").fetchone()[0]
    table_candidate_total = conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]
    # 黄金集召回：以该 cycle 的 candidate_cycle 为准（端到端“进候选”口径）
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    positives = golden.get("positives") or []
    negatives = golden.get("negatives") or []
    cand_norms = {
        r["norm"] for r in conn.execute("SELECT norm FROM candidate_cycle WHERE cycle_id=?", (cycle_id,))
    }
    pos_hit = [p for p in positives if p in cand_norms]
    neg_hit = [p for p in negatives if p in cand_norms]
    golden_seed_covered = [
        p for p in positives if seed_name(str(p), by_name, alias_map) is not None
    ]

    # 种子词在单轮候选中出现多少（口径：candidate_cycle 的 norm 与种子 canonical/alias 完全匹配）
    seed_in_cycle = 0
    for norm in cand_norms:
        if seed_name(norm, by_name, alias_map):
            seed_in_cycle += 1

    return {
        "tool": "eval/metrics.py",
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "db": {"path": "", "mode": "ro", "cycle": cycle_id},
        "cycle": {
            "id": cycle["id"],
            "started_at": cycle["started_at"],
            "finished_at": cycle["finished_at"],
            "status": cycle["status"],
            "stats": {
                k: stats.get(k)
                for k in (
                    "videos",
                    "comments",
                    "danmaku",
                    "candidates",
                    "families",
                    "usable",
                    "requests",
                    "elapsed",
                    "errors",
                )
            },
        },
        "top10": top,
        "metrics": {
            "seed_hit_count": seed_hits,
            "seed_hit_rate": round(seed_hits / n, 4) if n else None,
            "seed_hit_rate_db_components": round(
                sum(1 for t in top if t["seed_hit_db"]) / n, 4
            )
            if n
            else None,
            "non_seed_count": n - seed_hits,
            "non_seed_rate": round((n - seed_hits) / n, 4) if n else None,
            "evergreen_count": label_counts.get("evergreen", 0),
            "evergreen_rate": round(label_counts.get("evergreen", 0) / n, 4) if n else None,
            "pure_evergreen_count": sum(1 for t in top if t["label"] == "evergreen" and t["canonical"] in PURE_EVERGREEN),
            "generalized_evergreen_count": sum(
                1 for t in top if t["label"] == "evergreen" and t["canonical"] in GENERALIZED_EVERGREEN
            ),
            "news_count": label_counts.get("news_event", 0),
            "proper_noun_count": label_counts.get("proper_noun", 0),
            "marketing_count": label_counts.get("marketing", 0),
            "new_meme_count": label_counts.get("new_meme", 0),
            "rising_old_count": label_counts.get("rising_old", 0),
            "recent_meme_count": label_counts.get("recent_meme", 0),
            "unclear_count": label_counts.get("unclear", 0),
            "label_counts": dict(sorted(label_counts.items())),
            "origin_year_distribution": {str(y): years.count(y) for y in sorted(set(years))},
            "classic_count_le_2023": sum(1 for y in years if y <= 2023),
            "recent_count_ge_2024": sum(1 for y in years if y >= 2024),
            "avg_origin_age_years": round(sum(ages) / len(ages), 2) if ages else None,
            "avg_origin_age_nominal_years": round(
                sum(as_of.year - y for y in years) / len(years), 2
            )
            if years
            else None,
            "same_family": same_family_check(top, alias_rows),
        },
        "candidate_pool": {
            "families_cycle": stats.get("families"),
            "candidates_cycle": stats.get("candidates"),
            "usable_cycle": usable,
            "non_seed_usable": non_seed_usable,
            "seed_usable": usable - non_seed_usable,
            "meme_table_total_now": table_meme_total,
            "candidate_table_total_now": table_candidate_total,
            "seed_norms_in_cycle": seed_in_cycle,
        },
        "golden": {
            "positives": len(positives),
            "negatives": len(negatives),
            "positive_in_candidates": len(pos_hit),
            "positive_in_candidates_items": pos_hit,
            "recall_end_to_end": round(len(pos_hit) / len(positives), 4) if positives else None,
            "positive_seed_covered": len(golden_seed_covered),
            "positive_seed_covered_items": golden_seed_covered,
            "negative_in_candidates": len(neg_hit),
            "negative_false_positive_rate": round(len(neg_hit) / len(negatives), 4) if negatives else None,
        },
        "seed_audit": dist,
        "label_snapshot": {
            "source": "手工标注快照（docs/pm/EVAL-DESIGN.md）；非 ground truth",
            "labels": LABELS,
            "confidence": CONFIDENCE,
            "pure_evergreen": sorted(PURE_EVERGREEN),
            "generalized_evergreen": sorted(GENERALIZED_EVERGREEN),
        },
    }


def render_markdown(rep: dict) -> str:
    m = rep["metrics"]
    top = rep["top10"]
    lines = []
    lines.append(f"## Top10 · cycle#{rep['cycle']['id']}（{rep['cycle']['started_at']}）")
    lines.append("")
    lines.append("| # | canonical | score | status | seed | origin | age(y) | label | sample | df_video/df_zone |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for t in top:
        age = f"{t['origin_age_years']:.2f}" if t["origin_age_years"] is not None else "?"
        ev = t["evidence"]
        df = f"{ev['df_video']}/{ev['df_zone']}" if ev["df_video"] is not None else "-"
        sample = (t["sample"] or "").replace("|", "\\|")
        if len(sample) > 40:
            sample = sample[:40] + "…"
        lines.append(
            f"| {t['rank']} | {t['canonical']} | {t['score']} | {t['status']} | "
            f"{'Y' if t['seed_hit'] else 'N'} | {t['origin_year'] or '?'} | {age} | {t['label']} | "
            f"{sample} | {df} |"
        )
    lines.append("")
    lines.append("## 指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    rows = [
        ("seed 命中率", f"{m['seed_hit_count']}/{len(top)} = {m['seed_hit_rate']:.0%}"),
        ("非种子占比", f"{m['non_seed_count']}/{len(top)} = {m['non_seed_rate']:.0%}"),
        ("常青占比（evergreen）", f"{m['evergreen_count']}/{len(top)} = {m['evergreen_rate']:.0%}"),
        ("其中纯常青 / 已泛化老梗", f"{m['pure_evergreen_count']} / {m['generalized_evergreen_count']}"),
        ("新闻事件 / 营销 / 专名", f"{m['news_count']} / {m['marketing_count']} / {m['proper_noun_count']}"),
        ("new_meme / recent_meme / rising_old / unclear",
         f"{m['new_meme_count']} / {m['recent_meme_count']} / {m['rising_old_count']} / {m['unclear_count']}"),
        ("origin 年份分布（Top10）", json.dumps(m["origin_year_distribution"], ensure_ascii=False)),
        ("classic(≤2023) / recent(2024+)", f"{m['classic_count_le_2023']} / {m['recent_count_ge_2024']}"),
        ("平均 origin 年龄（年，7/1 中点）", f"{m['avg_origin_age_years']}"),
        ("平均 origin 年龄（名义 年差）", f"{m['avg_origin_age_nominal_years']}"),
        ("同族重复", "无" if not m["same_family"]["has_duplicate"] else json.dumps(m["same_family"], ensure_ascii=False)),
    ]
    for k, v in rows:
        lines.append(f"| {k} | {v} |")
    lines.append("")
    pool = rep["candidate_pool"]
    lines.append("## 候选池 / 黄金集（单轮口径）")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    for k, v in [
        ("cycle 统计 families", pool["families_cycle"]),
        ("cycle 统计 candidates", pool["candidates_cycle"]),
        ("meme_signal 可用数（含分值）", pool["usable_cycle"]),
        ("其中非种子可用数", pool["non_seed_usable"]),
        ("黄金集召回（进候选）", f"{rep['golden']['positive_in_candidates']}/{rep['golden']['positives']} = {rep['golden']['recall_end_to_end']:.1%}"),
        ("黄金集负例误入", f"{rep['golden']['negative_in_candidates']}/{rep['golden']['negatives']}"),
    ]:
        lines.append(f"| {k} | {v} |")
    lines.append("")
    d = rep["seed_audit"]
    lines.append("## 种子表 origin 年份")
    lines.append("")
    lines.append("| 年份 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 未知 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    by = d["by_year"]
    lines.append(
        f"| 条数 | {by['2020']} | {by['2021']} | {by['2022']} | {by['2023']} | {by['2024']} | {by['2025']} | {by['未知']} |"
    )
    extra = d["extras_outside_2020_2025"]
    if extra:
        lines.append("")
        lines.append(f"区间外年份（单列）：{json.dumps(extra, ensure_ascii=False)}")
    lines.append("")
    lines.append(f"总计 {d['total']}；classic(≤2023) {d['classic_le_2023']}；recent(2024+) {d['recent_ge_2024']}；未知 {d['unknown']}。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="hot_meme_search 评测指标（只读）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径（默认 <repo>/data/hotmeme.db）")
    ap.add_argument("--cycle", type=int, default=None, help="cycle id（默认最新 done）")
    ap.add_argument("--seed", default=str(DEFAULT_SEED), help="seed_memes.json 路径")
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="黄金集 memes.json 路径")
    ap.add_argument("--as-of", default=None, help="年龄计算基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--format", choices=["both", "json", "md"], default="both")
    args = ap.parse_args(argv)

    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
    db_path = Path(args.db).expanduser()
    try:
        conn = connect_ro(db_path)
    except sqlite3.Error as exc:
        raise SystemExit(f"[metrics] 只读打开失败：{exc}") from exc
    try:
        cycle_id = resolve_cycle(conn, args.cycle)
        rep = build_report(conn, cycle_id, Path(args.seed), Path(args.golden), as_of)
        rep["db"]["path"] = str(db_path)
    finally:
        conn.close()

    if args.format in ("both", "json"):
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.format == "both":
        print("\n<!-- ---- markdown ---- -->\n")
    if args.format in ("both", "md"):
        print(render_markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
