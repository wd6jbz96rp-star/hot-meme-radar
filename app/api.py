"""FastAPI 只读服务：新梗榜读 meme_info 报告，经典/全部读历史 SQLite 快照。

Phase 0-3（2026-11-15）已退役旧采集/排名 pipeline：
- 没有 scheduler、没有 ``/api/refresh`` 采集入口；
- ``/api/top`` 只读 ``store`` 中最后一次完成的 cycle；
- 新梗榜完全由 ``GET /api/meme-info`` 提供。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from . import config, meme_info_adapter, store

STATE: dict = {"snapshot": None, "running": False, "error": ""}
TOP_LISTS = ("all", "classic")


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, 50))


def _read_snapshot() -> dict:
    """只读最近一轮完成数据；不触发任何采集。"""
    connection = store.connect()
    try:
        health = store.health(connection)
        cycle = store.latest_cycle(connection)
        if not cycle:
            return {"status": "empty", "cycle": None, "top": [], "health": health}
        return {
            "status": cycle["status"],
            "cycle": cycle,
            "top": store.top_items(connection, int(cycle["id"]), config.TOP_N),
            "health": health,
        }
    finally:
        connection.close()


def _list_items(cycle_id: int, list_type: str, limit: int) -> list[dict]:
    connection = store.connect()
    try:
        return store.list_items(connection, int(cycle_id), list_type, limit)
    finally:
        connection.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    connection = store.connect()
    try:
        store.init_db(connection)
    finally:
        connection.close()
    try:
        STATE["snapshot"] = await asyncio.to_thread(_read_snapshot)
        STATE["error"] = ""
    except Exception as exc:  # noqa: BLE001
        STATE["error"] = str(exc)[:300]
        STATE["snapshot"] = {"status": "empty", "top": [], "cycle": None}
    yield


app = FastAPI(title="网络热梗动态 Top10", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    path = config.STATIC_DIR / "index.html"
    try:
        return HTMLResponse(path.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("<h1>看板未安装</h1>", status_code=500)


@app.get("/api/top")
async def api_top(limit: int = config.TOP_N,
                  list_filter: str = Query("all", alias="list")) -> dict:
    """历史快照榜；``list=classic|all``，不包含旧 new。"""
    if list_filter not in TOP_LISTS:
        raise HTTPException(status_code=400, detail="list 仅支持 classic|all")
    snapshot = STATE["snapshot"]
    if not snapshot or not snapshot.get("top"):
        snapshot = await asyncio.to_thread(_read_snapshot)
        STATE["snapshot"] = snapshot
    if list_filter == "all":
        top = [item for item in snapshot.get("top", []) if item.get("rank", 0) > 0]
    else:
        cycle = snapshot.get("cycle") or {}
        top = await asyncio.to_thread(
            _list_items, int(cycle["id"]), list_filter, _clamp_limit(limit)
        ) if cycle.get("id") else []
    return {
        "status": snapshot.get("status", "empty"),
        "cycle": snapshot.get("cycle"),
        "top": top[: _clamp_limit(limit)],
        "list": list_filter,
        "health": snapshot.get("health", {}),
        "running": False,
        "error": STATE["error"],
    }


@app.get("/api/meme-info")
async def api_meme_info(limit: int = 10, only: str = "all") -> dict:
    """新梗榜/模块 shadow：只读 meme_info 报告，不碰 DB。"""
    if only not in meme_info_adapter.ONLY_VALUES:
        raise HTTPException(status_code=400, detail="only 仅支持 all|publishable")
    return await asyncio.to_thread(meme_info_adapter.load_report, None, limit, only)


@app.get("/api/meme/{family_key}/history")
async def api_history(family_key: str, limit: int = 60) -> dict:
    connection = store.connect()
    try:
        return {"family_key": family_key,
                "history": store.meme_history(connection, family_key, limit)}
    finally:
        connection.close()


@app.get("/api/health")
async def api_health() -> dict:
    connection = store.connect()
    try:
        return {
            "scheduler": False,
            "running": False,
            "error": STATE["error"],
            "offline": config.OFFLINE,
            "ready": bool(STATE["snapshot"] and STATE["snapshot"].get("top")),
            "health": store.health(connection),
        }
    finally:
        connection.close()


@app.post("/api/refresh", deprecated=True)
async def api_refresh(request: Request) -> dict:
    """旧采集入口已退役；新梗榜由 meme_info worker 刷新。"""
    raise HTTPException(
        status_code=410,
        detail="旧采集 pipeline 已退役；新梗榜由 meme_info worker 刷新（见 docs/pm/LIVE-DEPLOY.md）",
    )


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    """保留 SSE 兼容：只推初始快照 + ping，不再有采集广播。"""
    async def generator():
        initial = STATE["snapshot"] or {"status": "empty", "top": []}
        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
        while True:
            await asyncio.sleep(20)
            yield ": ping\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
