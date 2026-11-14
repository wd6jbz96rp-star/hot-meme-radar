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
    mode INTEGER NOT NULL DEFAULT 3,
    page INTEGER NOT NULL,
    next_cursor INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(aid, mode, page)
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
CREATE INDEX IF NOT EXISTS idx_llm_review_cycle ON llm_review(cycle_id);
"""


COMMENT_CACHE_DDL = """CREATE TABLE IF NOT EXISTS comment_cache(
    aid INTEGER NOT NULL,
    mode INTEGER NOT NULL DEFAULT 3,
    page INTEGER NOT NULL,
    next_cursor INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(aid, mode, page)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """兼容旧库（幂等）：补 meme_signal.is_new；comment_cache 加 mode 进主键。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(meme_signal)")}
    if "is_new" not in columns:
        conn.execute(
            "ALTER TABLE meme_signal ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0"
        )
    _migrate_comment_cache(conn)
    _migrate_llm_review(conn)


def _migrate_llm_review(conn: sqlite3.Connection) -> None:
    """P3 审计表幂等迁移：老库无此表由 SCHEMA 建；缺 merge_into 的补列。

    表本身用 `CREATE TABLE IF NOT EXISTS`，重复 init_db 不丢数据；
    这里只处理「表已存在但列不全」的早期草稿库。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_review)")}
    if columns and "merge_into" not in columns:
        conn.execute("ALTER TABLE llm_review ADD COLUMN merge_into TEXT NOT NULL DEFAULT ''")


def _migrate_comment_cache(conn: sqlite3.Connection) -> None:
    """旧表主键 (aid,page) 无法区分 mode：重建为 (aid,mode,page)，旧行视为 mode=3。

    先 rename 再建新表再搬运；已迁移的库检测到 mode 列直接返回，可重复执行。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(comment_cache)")}
    if not columns:
        return
    if "mode" not in columns:
        conn.execute("ALTER TABLE comment_cache RENAME TO comment_cache_legacy")
        conn.executescript(COMMENT_CACHE_DDL)
        conn.execute(
            """INSERT OR IGNORE INTO comment_cache(aid, mode, page, fetched_at, payload)
               SELECT aid, 3, page, fetched_at, payload FROM comment_cache_legacy"""
        )
        conn.execute("DROP TABLE comment_cache_legacy")
        return
    # P1：缓存页携带 next 游标，warm 轮命中缓存才能正确判断是否继续翻页（0 不请求）。
    if "next_cursor" not in columns:
        conn.execute(
            "ALTER TABLE comment_cache ADD COLUMN next_cursor INTEGER NOT NULL DEFAULT 0"
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
    """`raw_time`：评论落 `ctime`（Unix 秒），弹幕回退到 `at`（视频内秒数）。

    P0 前这里取 `s.get("at")`，评论没有 `at` 字段 → raw_time 恒 0（已修）。
    """
    rows = []
    for s in samples:
        if not s.get("text"):
            continue
        raw_time = s.get("ctime")
        if raw_time is None:
            raw_time = s.get("at")
        try:
            raw_time = float(raw_time or 0)
        except (TypeError, ValueError):
            raw_time = 0.0
        rows.append(
            (cycle_id, bvid, kind, s.get("text", ""), int(s.get("like", 0) or 0), raw_time)
        )
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


def _normalize_comment_items(items: Iterable[dict] | None) -> list[dict]:
    """评论缓存/落库统一形状，保证 `ctime` 一定在（缓存也要带时间）。"""
    rows: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            ctime = int(item.get("ctime") or 0)
        except (TypeError, ValueError):
            ctime = 0
        try:
            like = int(item.get("like") or 0)
        except (TypeError, ValueError):
            like = 0
        rows.append({"text": text, "like": like, "ctime": ctime,
                     "rpid": item.get("rpid")})
    return rows


def _comment_cache_entry(conn: sqlite3.Connection, aid: int, mode: int, page: int,
                         ttl_seconds: int | None) -> tuple[list[dict], int] | None:
    """返回 (评论页, next 游标)；`None` = 未命中/已过期/缓存关闭。键 = (aid,mode,page)。"""
    ttl = config.COMMENT_CACHE_SECONDS if ttl_seconds is None else ttl_seconds
    if ttl <= 0:
        return None
    row = conn.execute(
        "SELECT payload, next_cursor, fetched_at FROM comment_cache "
        "WHERE aid=? AND mode=? AND page=?",
        (aid, mode, page),
    ).fetchone()
    if not row or not _fresh(row["fetched_at"], ttl):
        return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return _normalize_comment_items(data), int(row["next_cursor"] or 0)


def get_comment_cache(conn: sqlite3.Connection, aid: int, mode: int = 3, page: int = 1,
                      ttl_seconds: int | None = None) -> list[dict] | None:
    """返回 TTL 内的评论页；`None` = 未命中/已过期/缓存关闭。"""
    entry = _comment_cache_entry(conn, aid, mode, page, ttl_seconds)
    return entry[0] if entry else None


def get_comment_page_cache(conn: sqlite3.Connection, aid: int, mode: int = 3, page: int = 1,
                           ttl_seconds: int | None = None) -> tuple[list[dict], int] | None:
    """评论页 + 下一页游标（P1：warm 轮据此决定是否继续翻页，命中即不请求）。"""
    return _comment_cache_entry(conn, aid, mode, page, ttl_seconds)


def put_comment_cache(conn: sqlite3.Connection, aid: int, mode: int = 3, page: int = 1,
                      items: list[dict] | None = None,
                      next_cursor: int = 0) -> None:
    if config.COMMENT_CACHE_SECONDS <= 0:
        return
    try:
        cursor = max(0, int(next_cursor or 0))
    except (TypeError, ValueError):
        cursor = 0
    conn.execute(
        "INSERT OR REPLACE INTO comment_cache"
        "(aid, mode, page, next_cursor, fetched_at, payload) VALUES(?,?,?,?,?,?)",
        (aid, mode, page, cursor, now_iso(),
         json.dumps(_normalize_comment_items(items), ensure_ascii=False)),
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


def insert_llm_reviews(conn: sqlite3.Connection, cycle_id: int,
                       reviews: Iterable[dict]) -> None:
    """P3 审核审计：同一 (cycle, family) 重跑时覆盖，不重复累积。

    字段与 docs/pm/P2-LLM-DESIGN.md §7 的 log 对齐；`mode` 记录当时开关，
    便于事后区分 shadow 观察值与 demote/require_approve 的发布决定。
    """
    stamp = now_iso()
    rows = [
        (
            cycle_id, str(r["family_key"]), str(r.get("verdict", "unclear")),
            float(r.get("meme_prob") or 0), str(r.get("novelty", "")),
            str(r.get("origin", "")), str(r.get("reason") or "")[:500],
            str(r.get("evidence_hash", "")), str(r.get("mode", "")),
            str(r.get("model", "")), str(r.get("prompt_version", "")),
            str(r.get("merge_into", "")), str(r.get("created_at") or stamp),
        )
        for r in reviews
        if r.get("family_key")
    ]
    conn.executemany(
        """INSERT INTO llm_review(cycle_id, family_key, verdict, meme_prob, novelty,
               origin, reason, evidence_hash, mode, model, prompt_version,
               merge_into, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(cycle_id, family_key) DO UPDATE SET
             verdict=excluded.verdict, meme_prob=excluded.meme_prob,
             novelty=excluded.novelty, origin=excluded.origin,
             reason=excluded.reason, evidence_hash=excluded.evidence_hash,
             mode=excluded.mode, model=excluded.model,
             prompt_version=excluded.prompt_version,
             merge_into=excluded.merge_into, created_at=excluded.created_at""",
        rows,
    )
    conn.commit()


def llm_review_map(conn: sqlite3.Connection, cycle_id: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM llm_review WHERE cycle_id=?", (cycle_id,)
    ).fetchall()
    return {
        row["family_key"]: {
            "verdict": row["verdict"],
            "meme_prob": float(row["meme_prob"] or 0),
            "novelty": row["novelty"],
            "origin": row["origin"],
            "reason": row["reason"],
            "merge_into": row["merge_into"],
            "mode": row["mode"],
            "model": row["model"],
        }
        for row in rows
    }


def cycle_stats(conn: sqlite3.Connection, cycle_id: int) -> dict:
    row = conn.execute("SELECT stats_json FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["stats_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def previous_approved_cycle(conn: sqlite3.Connection, before_cycle: int) -> int | None:
    """找上一轮确有 approve 的 cycle，供 require_approve 0 通过时沿用。

    不限定上一轮的模式：demote/shadow 的 approve 也是已审核结论；沿用时会再按
    `verdict == approve` 过滤（见 `list_items`），不会把未审/拒绝项带回新梗榜。
    """
    rows = conn.execute(
        "SELECT id, stats_json FROM cycle WHERE id<? AND status IN ('done','degraded') "
        "ORDER BY id DESC", (before_cycle,)
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["stats_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or int(data.get("llm_approved") or 0) <= 0:
            continue
        # 降级轮（gate_applied=false）的 approve 没有真正发布，不能当历史结果。
        if not data.get("llm_gate_applied", True):
            continue
        approved = conn.execute(
            """SELECT COUNT(*) FROM llm_review lr
               JOIN meme_signal s
                 ON s.cycle_id=lr.cycle_id AND s.family_key=lr.family_key
               WHERE lr.cycle_id=? AND lr.verdict='approve'""",
            (int(row["id"]),),
        ).fetchone()[0]
        if int(approved or 0) > 0:
            return int(row["id"])
    return None


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


def _signal_item(row: sqlite3.Row, prev: dict[str, int]) -> dict:
    """meme_signal 行 → API/榜单条目（含 P1 list、P3 llm_review）。"""
    components = {}
    try:
        components = json.loads(row["components_json"] or "{}")
    except json.JSONDecodeError:
        pass
    raw = components.get("raw") if isinstance(components.get("raw"), dict) else {}
    # P1 新旧榜：components.list 优先，旧数据回退 raw.list，再回退 classic（不猜）。
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
        f"""SELECT {_ITEM_COLUMNS}
           {_ITEM_JOIN}
           WHERE s.cycle_id=? AND s.rank>0 ORDER BY s.rank LIMIT ?""",
        (cycle_id, limit),
    ).fetchall()
    prev = previous_ranks(conn, cycle_id)
    return [_signal_item(row, prev) for row in rows]


def list_items(conn: sqlite3.Connection, cycle_id: int, list_type: str,
               limit: int = 10) -> list[dict]:
    """新旧榜过滤（P1/P3）：`list` = new/classic。

    - 包含 rank=0 的 comment_backed 观察项（它们不占主榜名次，只在对应列表曝光）；
    - `list=new` 叠加 P3 发布门：cycle.stats.llm_mode 为 demote/require_approve 时，
      按 llm_review.verdict 过滤；`llm_new_list_cycle` 存在时改读上一轮已审核结果
      （require_approve 0 通过的沿用路径），源 cycle 为 0 则返回空榜；
    - `classic` 不叠加任何 LLM 过滤，保持 P1 行为。
    """
    stats = cycle_stats(conn, cycle_id)
    carried = False
    if list_type == "new" and "llm_new_list_cycle" in stats:
        try:
            source = int(stats.get("llm_new_list_cycle") or 0)
        except (TypeError, ValueError):
            source = 0
        if source <= 0:
            return []
        cycle_id = source
        stats = cycle_stats(conn, cycle_id)
        carried = True

    rows = conn.execute(
        f"""SELECT {_ITEM_COLUMNS}
           {_ITEM_JOIN}
           WHERE s.cycle_id=? ORDER BY s.score DESC""",
        (cycle_id,),
    ).fetchall()
    prev = previous_ranks(conn, cycle_id)
    mode = str(stats.get("llm_mode") or "off")
    # 降级（无 key/超时/预算/异常）时 gate_applied=False：新梗榜必须回规则榜，
    # 不能被 require_approve 的「只留 approve」误伤成空榜。
    gate_applied = bool(stats.get("llm_gate_applied", True))
    items = []
    for row in rows:
        item = _signal_item(row, prev)
        if item["list"] != list_type:
            continue
        if list_type == "new":
            verdict = (item.get("llm_review") or {}).get("verdict")
            if carried:
                # 沿用上一轮时只带 approved，绝不把未审/unclear 冒充严格榜。
                if verdict != "approve":
                    continue
            elif gate_applied and mode == "require_approve" and verdict != "approve":
                continue
            elif gate_applied and mode == "demote" and verdict in ("reject", "merge"):
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
