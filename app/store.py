"""SQLite 持久化层：表结构、写入与查询。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from . import config


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS cycle(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS video(
    bvid TEXT PRIMARY KEY,
    aid INTEGER,
    cid INTEGER,
    title TEXT,
    desc TEXT,
    owner_mid INTEGER,
    owner_name TEXT,
    tid INTEGER,
    tname TEXT,
    zone TEXT,
    pubdate INTEGER,
    duration INTEGER,
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS video_metric(
    cycle_id INTEGER NOT NULL,
    bvid TEXT NOT NULL,
    view INTEGER, danmaku INTEGER, reply INTEGER,
    like INTEGER, coin INTEGER, favorite INTEGER, share INTEGER,
    rank_pos INTEGER, zone TEXT,
    PRIMARY KEY(cycle_id, bvid)
);
CREATE TABLE IF NOT EXISTS text_sample(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    bvid TEXT,
    kind TEXT,
    text TEXT,
    likes INTEGER DEFAULT 0,
    raw_time REAL DEFAULT 0,
    UNIQUE(bvid, kind, text)
);
CREATE INDEX IF NOT EXISTS idx_text_sample_cycle ON text_sample(cycle_id);
CREATE TABLE IF NOT EXISTS danmaku_cache(
    cid INTEGER PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS comment_cache(
    aid INTEGER NOT NULL,
    page INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(aid, page)
);
CREATE TABLE IF NOT EXISTS candidate(
    norm TEXT PRIMARY KEY,
    surface TEXT,
    first_seen TEXT,
    last_seen TEXT,
    df_video INTEGER DEFAULT 0,
    df_zone INTEGER DEFAULT 0,
    total_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candidate_cycle(
    cycle_id INTEGER NOT NULL,
    norm TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY(cycle_id, norm)
);
CREATE TABLE IF NOT EXISTS meme(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT UNIQUE NOT NULL,
    canonical TEXT NOT NULL,
    type TEXT DEFAULT '词',
    first_seen TEXT,
    last_seen TEXT,
    status TEXT DEFAULT 'observing',
    origin TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meme_alias(
    meme_id INTEGER NOT NULL,
    surface TEXT NOT NULL,
    PRIMARY KEY(meme_id, surface)
);
CREATE TABLE IF NOT EXISTS meme_signal(
    cycle_id INTEGER NOT NULL,
    meme_id INTEGER NOT NULL,
    family_key TEXT NOT NULL,
    score REAL NOT NULL,
    rank INTEGER NOT NULL,
    status TEXT NOT NULL,
    components_json TEXT NOT NULL DEFAULT '{}',
    sample TEXT DEFAULT '',
    is_new INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(cycle_id, meme_id)
);
CREATE INDEX IF NOT EXISTS idx_signal_cycle ON meme_signal(cycle_id, rank);
CREATE TABLE IF NOT EXISTS source_error(
    cycle_id INTEGER,
    source TEXT,
    error TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """兼容旧库：为已存在的 meme_signal 补 is_new 列（幂等）。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(meme_signal)")}
    if "is_new" not in columns:
        conn.execute(
            "ALTER TABLE meme_signal ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0"
        )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


# ---------------- 轮次 ----------------
def start_cycle(conn: sqlite3.Connection, started_at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO cycle(started_at, status) VALUES(?, 'running')",
        (started_at or now_iso(),),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_cycle(conn: sqlite3.Connection, cycle_id: int, status: str, stats: dict) -> None:
    conn.execute(
        "UPDATE cycle SET finished_at=?, status=?, stats_json=? WHERE id=?",
        (now_iso(), status, json.dumps(stats, ensure_ascii=False), cycle_id),
    )
    conn.commit()


def record_error(conn: sqlite3.Connection, cycle_id: int, source: str, error: str) -> None:
    conn.execute(
        "INSERT INTO source_error(cycle_id, source, error) VALUES(?,?,?)",
        (cycle_id, source, error[:300]),
    )
    conn.commit()


def abort_running_cycles(conn: sqlite3.Connection,
                         reason: str = "服务启动时发现未收尾的采集轮次，已标记为 aborted") -> int:
    """把进程中断（Ctrl-C / 重启 / OOM）遗留的 running 轮次收尾，避免监控盲区。

    幂等：没有残留时返回 0；每行补一条 source_error 便于追溯。
    """
    rows = conn.execute("SELECT id FROM cycle WHERE status='running'").fetchall()
    stamp = now_iso()
    for row in rows:
        cycle_id = int(row["id"])
        conn.execute(
            "UPDATE cycle SET status='aborted', finished_at=? WHERE id=?",
            (stamp, cycle_id),
        )
        conn.execute(
            "INSERT INTO source_error(cycle_id, source, error) VALUES(?,?,?)",
            (cycle_id, "lifecycle", reason[:300]),
        )
    conn.commit()
    return len(rows)


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


# ---------------- 视频 ----------------
def upsert_videos(conn: sqlite3.Connection, videos: Iterable[dict], now: str | None = None) -> None:
    stamp = now or now_iso()
    rows = [
        (
            v["bvid"], v.get("aid"), v.get("cid"), v.get("title", ""), v.get("desc", ""),
            v.get("owner_mid"), v.get("owner_name", ""), v.get("tid"), v.get("tname", ""),
            v.get("zone", ""), v.get("pubdate", 0), v.get("duration", 0), stamp,
        )
        for v in videos
        if v.get("bvid")
    ]
    conn.executemany(
        """INSERT INTO video(bvid, aid, cid, title, desc, owner_mid, owner_name, tid, tname,
                             zone, pubdate, duration, first_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(bvid) DO UPDATE SET
             title=excluded.title, desc=excluded.desc,
             cid=COALESCE(excluded.cid, cid),
             owner_name=excluded.owner_name, zone=excluded.zone""",
        rows,
    )
    conn.commit()


def cid_map(conn: sqlite3.Connection, bvids: Iterable[str]) -> dict[str, int]:
    """批量读取已持久化的 cid，避免每轮对搜索视频重复请求 pagelist。"""
    ids = sorted({b for b in bvids if b})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT bvid, cid FROM video WHERE bvid IN ({placeholders}) AND cid IS NOT NULL",
        ids,
    ).fetchall()
    return {r["bvid"]: int(r["cid"]) for r in rows}


def update_video_cid(conn: sqlite3.Connection, bvid: str, cid: int | None) -> None:
    """collect_docs 补到 cid 后回写；`upsert_videos` 的 COALESCE 保证后续轮不被 NULL 覆盖。"""
    if not bvid or not cid:
        return
    conn.execute("UPDATE video SET cid=? WHERE bvid=?", (int(cid), bvid))
    conn.commit()


def insert_metrics(conn: sqlite3.Connection, cycle_id: int, videos: Iterable[dict]) -> None:
    rows = [
        (
            cycle_id, v["bvid"], v.get("view", 0), v.get("danmaku", 0), v.get("reply", 0),
            v.get("like", 0), v.get("coin", 0), v.get("favorite", 0), v.get("share", 0),
            v.get("rank_pos", 0), v.get("zone", ""),
        )
        for v in videos
        if v.get("bvid")
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO video_metric(cycle_id, bvid, view, danmaku, reply, like,
               coin, favorite, share, rank_pos, zone)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def insert_text_samples(conn: sqlite3.Connection, cycle_id: int, bvid: str, kind: str,
                        samples: Iterable[dict]) -> None:
    rows = [
        (cycle_id, bvid, kind, s.get("text", ""), int(s.get("like", 0) or 0),
         float(s.get("at", 0) or 0))
        for s in samples
        if s.get("text")
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO text_sample(cycle_id, bvid, kind, text, likes, raw_time) "
        "VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


# ---------------- 缓存 ----------------
def _fresh(stamp: str, ttl_seconds: int) -> bool:
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    return (datetime.now().astimezone() - when).total_seconds() < ttl_seconds


def get_danmaku_cache(conn: sqlite3.Connection, cid: int) -> bytes | None:
    row = conn.execute(
        "SELECT payload, fetched_at FROM danmaku_cache WHERE cid=?", (cid,)
    ).fetchone()
    if row and _fresh(row["fetched_at"], config.CACHE_TTL_SECONDS):
        return bytes(row["payload"])
    return None


def put_danmaku_cache(conn: sqlite3.Connection, cid: int, payload: bytes) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO danmaku_cache(cid, fetched_at, payload) VALUES(?,?,?)",
        (cid, now_iso(), payload),
    )
    conn.commit()


def get_comment_cache(conn: sqlite3.Connection, aid: int, page: int,
                      ttl_seconds: int | None = None) -> list[dict] | None:
    """返回 TTL 内的评论页；`None` = 未命中/已过期/缓存关闭。"""
    ttl = config.COMMENT_CACHE_SECONDS if ttl_seconds is None else ttl_seconds
    if ttl <= 0:
        return None
    row = conn.execute(
        "SELECT payload, fetched_at FROM comment_cache WHERE aid=? AND page=?", (aid, page)
    ).fetchone()
    if not row or not _fresh(row["fetched_at"], ttl):
        return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, list) else None


def put_comment_cache(conn: sqlite3.Connection, aid: int, page: int,
                      items: list[dict]) -> None:
    if config.COMMENT_CACHE_SECONDS <= 0:
        return
    conn.execute(
        "INSERT OR REPLACE INTO comment_cache(aid, page, fetched_at, payload) VALUES(?,?,?,?)",
        (aid, page, now_iso(), json.dumps(items, ensure_ascii=False)),
    )
    conn.commit()


# ---------------- 候选与梗 ----------------
def count_since(conn: sqlite3.Connection, norm: str, since_iso: str) -> int:
    row = conn.execute(
        """SELECT COALESCE(SUM(cc.count),0) FROM candidate_cycle cc
           JOIN cycle c ON c.id=cc.cycle_id
           WHERE cc.norm=? AND c.started_at>=?""",
        (norm, since_iso),
    ).fetchone()
    return int(row[0] or 0)


def first_seen_map(conn: sqlite3.Connection, family_keys: Iterable[str]) -> dict[str, str]:
    keys = list(family_keys)
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT family_key, first_seen FROM meme WHERE family_key IN ({placeholders})",
        keys,
    ).fetchall()
    return {r["family_key"]: r["first_seen"] for r in rows if r["first_seen"]}


def upsert_candidates(conn: sqlite3.Connection, candidates: Iterable[dict], cycle_id: int) -> None:
    stamp = now_iso()
    conn.executemany(
        """INSERT INTO candidate(norm, surface, first_seen, last_seen, df_video, df_zone, total_count)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(norm) DO UPDATE SET
             surface=excluded.surface, last_seen=excluded.last_seen,
             df_video=excluded.df_video, df_zone=excluded.df_zone,
             total_count=excluded.total_count""",
        [
            (c["norm"], c["surface"], stamp, stamp, c.get("df_video", 0),
             c.get("df_zone", 0), c.get("count", 0))
            for c in candidates
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO candidate_cycle(cycle_id, norm, count) VALUES(?,?,?)",
        [(cycle_id, c["norm"], c.get("count", 0)) for c in candidates],
    )
    conn.commit()


def upsert_memes(conn: sqlite3.Connection, families: Iterable[dict]) -> dict[str, int]:
    stamp = now_iso()
    mapping: dict[str, int] = {}
    for family in families:
        key = family["family_key"]
        row = conn.execute("SELECT id FROM meme WHERE family_key=?", (key,)).fetchone()
        if row:
            meme_id = int(row["id"])
            conn.execute(
                "UPDATE meme SET canonical=?, last_seen=?, type=? WHERE id=?",
                (family["canonical"], stamp, family.get("type", "词"), meme_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO meme(family_key, canonical, type, first_seen, last_seen, status, origin)
                   VALUES(?,?,?,?,?,'observing',?)""",
                (key, family["canonical"], family.get("type", "词"), stamp, stamp,
                 family.get("origin", "")),
            )
            meme_id = int(cur.lastrowid)
        mapping[key] = meme_id
    conn.commit()
    return mapping


def add_aliases(conn: sqlite3.Connection, meme_id: int, surfaces: Iterable[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO meme_alias(meme_id, surface) VALUES(?,?)",
        [(meme_id, s) for s in surfaces if s],
    )
    conn.commit()


def insert_signals(conn: sqlite3.Connection, cycle_id: int, items: Iterable[dict]) -> None:
    stamp = now_iso()
    rows = [
        (
            cycle_id, item["meme_id"], item["family_key"], float(item["score"]),
            int(item["rank"]), item.get("status", "observing"),
            json.dumps(item.get("components", {}), ensure_ascii=False),
            item.get("sample", ""), 1 if item.get("is_new") else 0, stamp,
        )
        for item in items
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO meme_signal(cycle_id, meme_id, family_key, score, rank,
               status, components_json, sample, is_new, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def previous_scores(conn: sqlite3.Connection, before_cycle: int) -> dict[str, float]:
    row = conn.execute(
        "SELECT MAX(id) FROM cycle WHERE id<? AND status IN ('done','degraded')", (before_cycle,)
    ).fetchone()
    if not row or row[0] is None:
        return {}
    prev_id = int(row[0])
    rows = conn.execute(
        "SELECT family_key, score FROM meme_signal WHERE cycle_id=?", (prev_id,)
    ).fetchall()
    return {r["family_key"]: float(r["score"]) for r in rows}


def previous_ranks(conn: sqlite3.Connection, before_cycle: int) -> dict[str, int]:
    row = conn.execute(
        "SELECT MAX(id) FROM cycle WHERE id<? AND status IN ('done','degraded')", (before_cycle,)
    ).fetchone()
    if not row or row[0] is None:
        return {}
    prev_id = int(row[0])
    rows = conn.execute(
        "SELECT family_key, rank FROM meme_signal WHERE cycle_id=?", (prev_id,)
    ).fetchall()
    return {r["family_key"]: int(r["rank"]) for r in rows}


def hype_streak(conn: sqlite3.Connection, family_key: str, threshold: float, rounds: int = 2) -> bool:
    rows = conn.execute(
        "SELECT score FROM meme_signal WHERE family_key=? ORDER BY cycle_id DESC LIMIT ?",
        (family_key, rounds),
    ).fetchall()
    if len(rows) < rounds:
        return False
    return all(float(r["score"]) >= threshold for r in rows)


def top_items(conn: sqlite3.Connection, cycle_id: int, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """SELECT s.family_key, s.score, s.rank, s.status, s.components_json, s.sample,
                  s.is_new, s.updated_at, m.id AS meme_id, m.canonical, m.type, m.first_seen
           FROM meme_signal s JOIN meme m ON m.id=s.meme_id
           WHERE s.cycle_id=? AND s.rank>0 ORDER BY s.rank LIMIT ?""",
        (cycle_id, limit),
    ).fetchall()
    items: list[dict] = []
    prev = previous_ranks(conn, cycle_id)
    for row in rows:
        components = {}
        try:
            components = json.loads(row["components_json"] or "{}")
        except json.JSONDecodeError:
            pass
        previous_rank = prev.get(row["family_key"])
        items.append(
            {
                "meme_id": row["meme_id"],
                "family_key": row["family_key"],
                "canonical": row["canonical"],
                "type": row["type"],
                "score": round(float(row["score"]), 1),
                "rank": int(row["rank"]),
                "previous_rank": previous_rank,
                "delta": (previous_rank - int(row["rank"])) if previous_rank else None,
                "status": row["status"],
                "components": components,
                "sample": row["sample"],
                "first_seen": row["first_seen"],
                "is_new": bool(row["is_new"]),
                "updated_at": row["updated_at"],
            }
        )
    return items


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


def prune(conn: sqlite3.Connection) -> None:
    text_before = (datetime.now().astimezone()
                   - timedelta(days=config.TEXT_SAMPLE_TTL_DAYS)).isoformat(timespec="seconds")
    metric_before_cycle = conn.execute(
        "SELECT COALESCE(MIN(id),0) FROM cycle WHERE started_at>=?",
        ((datetime.now().astimezone() - timedelta(days=config.METRIC_TTL_DAYS)).isoformat(
            timespec="seconds"),),
    ).fetchone()[0]
    conn.execute("DELETE FROM text_sample WHERE id IN (SELECT id FROM text_sample WHERE cycle_id IN "
                 "(SELECT id FROM cycle WHERE started_at<?))", (text_before,))
    conn.execute("DELETE FROM video_metric WHERE cycle_id<?", (metric_before_cycle,))
    conn.execute("DELETE FROM source_error WHERE cycle_id<?", (metric_before_cycle,))
    conn.commit()
