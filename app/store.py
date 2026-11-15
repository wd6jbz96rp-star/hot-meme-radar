"""只读历史快照仓库（旧 pipeline 已退役）。

只保留 API 需要的读能力：最近 cycle、top/classic 条目、单梗历史、health。
不再提供采集写入、缓存写入、候选/信号写入。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import config


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS cycle(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meme(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT UNIQUE,
    canonical TEXT NOT NULL,
    type TEXT DEFAULT '词',
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meme_signal(
    cycle_id INTEGER NOT NULL,
    meme_id INTEGER NOT NULL,
    family_key TEXT NOT NULL,
    score REAL NOT NULL,
    rank INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    components_json TEXT NOT NULL DEFAULT '{}',
    sample TEXT DEFAULT '',
    is_new INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(cycle_id, meme_id)
);
CREATE TABLE IF NOT EXISTS llm_review(
    cycle_id INTEGER NOT NULL,
    family_key TEXT NOT NULL,
    verdict TEXT NOT NULL,
    meme_prob REAL NOT NULL DEFAULT 0,
    novelty TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    evidence_hash TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    merge_into TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(cycle_id, family_key)
);
CREATE TABLE IF NOT EXISTS source_error(
    cycle_id INTEGER,
    source TEXT,
    error TEXT
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """只建历史读取所需的最小表；已有生产库不会被改结构。"""
    conn.executescript(SCHEMA)
    conn.commit()


def latest_cycle(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM cycle WHERE status IN ('done','degraded') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["stats"] = json.loads(data.pop("stats_json") or "{}")
    except json.JSONDecodeError:
        data["stats"] = {}
    return data


def cycle_stats(conn: sqlite3.Connection, cycle_id: int) -> dict:
    row = conn.execute("SELECT stats_json FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["stats_json"] or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def previous_ranks(conn: sqlite3.Connection, cycle_id: int) -> dict[str, int]:
    row = conn.execute("SELECT id FROM cycle WHERE id < ? ORDER BY id DESC LIMIT 1", (cycle_id,)).fetchone()
    if not row:
        return {}
    rows = conn.execute(
        "SELECT family_key, rank FROM meme_signal WHERE cycle_id=? AND rank>0", (row["id"],)
    ).fetchall()
    return {str(r["family_key"]): int(r["rank"]) for r in rows}


def _signal_item(row: sqlite3.Row, prev: dict[str, int]) -> dict:
    components = {}
    try:
        components = json.loads(row["components_json"] or "{}")
    except json.JSONDecodeError:
        pass
    raw = components.get("raw") if isinstance(components.get("raw"), dict) else {}
    list_type = components.get("list") or raw.get("list") or "classic"
    if list_type not in ("new", "classic"):
        list_type = "classic"
    previous_rank = prev.get(row["family_key"])
    keys = set(row.keys())
    review = None
    if "llm_verdict" in keys and row["llm_verdict"]:
        review = {
            "verdict": row["llm_verdict"],
            "reason": row["llm_reason"] or "",
            "meme_prob": float(row["llm_meme_prob"] or 0),
            "novelty": row["llm_novelty"] or "",
            "origin": row["llm_origin"] or "",
            "merge_into": row["llm_merge_into"] or "",
            "mode": row["llm_mode"] or "",
            "model": row["llm_model"] or "",
        }
    return {
        "meme_id": row["meme_id"],
        "family_key": row["family_key"],
        "canonical": row["canonical"],
        "type": row["type"],
        "score": round(float(row["score"]), 1),
        "rank": int(row["rank"]),
        "previous_rank": previous_rank,
        "delta": (previous_rank - int(row["rank"])) if previous_rank else None,
        "status": row["status"],
        "list": list_type,
        "components": components,
        "sample": row["sample"],
        "first_seen": row["first_seen"],
        "is_new": bool(row["is_new"]),
        "updated_at": row["updated_at"],
        "llm_review": review,
    }


_ITEM_COLUMNS = """s.family_key, s.score, s.rank, s.status, s.components_json, s.sample,
                   s.is_new, s.updated_at, m.id AS meme_id, m.canonical, m.type, m.first_seen,
                   lr.verdict AS llm_verdict, lr.reason AS llm_reason,
                   lr.meme_prob AS llm_meme_prob, lr.novelty AS llm_novelty,
                   lr.origin AS llm_origin, lr.merge_into AS llm_merge_into,
                   lr.mode AS llm_mode, lr.model AS llm_model"""

_ITEM_JOIN = """FROM meme_signal s
                JOIN meme m ON m.id=s.meme_id
                LEFT JOIN llm_review lr
                  ON lr.cycle_id=s.cycle_id AND lr.family_key=s.family_key"""


def top_items(conn: sqlite3.Connection, cycle_id: int, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        f"""SELECT {_ITEM_COLUMNS} {_ITEM_JOIN}
            WHERE s.cycle_id=? AND s.rank>0 ORDER BY s.rank LIMIT ?""",
        (cycle_id, limit),
    ).fetchall()
    prev = previous_ranks(conn, cycle_id)
    return [_signal_item(row, prev) for row in rows]


def list_items(conn: sqlite3.Connection, cycle_id: int, list_type: str,
               limit: int = 10) -> list[dict]:
    """历史榜过滤：只服务 classic/all；new 已下线。"""
    if list_type not in ("classic", "all"):
        return []
    rows = conn.execute(
        f"""SELECT {_ITEM_COLUMNS} {_ITEM_JOIN}
            WHERE s.cycle_id=? ORDER BY s.score DESC""",
        (cycle_id,),
    ).fetchall()
    prev = previous_ranks(conn, cycle_id)
    items: list[dict] = []
    for row in rows:
        item = _signal_item(row, prev)
        if list_type == "classic" and item["list"] != "classic":
            continue
        items.append(item)
    return items[:limit]


def meme_history(conn: sqlite3.Connection, family_key: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        """SELECT s.score, s.rank, s.updated_at, c.finished_at
           FROM meme_signal s LEFT JOIN cycle c ON c.id=s.cycle_id
           WHERE s.family_key=? ORDER BY s.cycle_id DESC LIMIT ?""",
        (family_key, limit),
    ).fetchall()
    return [
        {"score": round(float(r["score"]), 1), "rank": int(r["rank"]),
         "at": r["finished_at"] or r["updated_at"]}
        for r in reversed(rows)
    ]


def health(conn: sqlite3.Connection) -> dict:
    cycle = latest_cycle(conn)
    if not cycle:
        return {"status": "empty", "last_cycle": None}
    errors = conn.execute(
        "SELECT source, error FROM source_error WHERE cycle_id=? LIMIT 20", (cycle["id"],)
    ).fetchall()
    return {
        "status": cycle["status"],
        "last_cycle": {"id": cycle["id"], "started_at": cycle["started_at"],
                       "finished_at": cycle["finished_at"], "stats": cycle["stats"]},
        "errors": [{"source": r["source"], "error": r["error"]} for r in errors],
    }
