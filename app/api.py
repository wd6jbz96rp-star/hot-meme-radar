"""FastAPI 服务：榜单 API、SSE 推送与看板。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from . import config, meme_info_adapter, pipeline, store

STATE: dict = {"snapshot": None, "running": False, "error": ""}
SUBSCRIBERS: set[asyncio.Queue] = set()
RUN_LOCK = asyncio.Lock()
_COLLECT_TASK: asyncio.Task | None = None
_SCHEDULER_TASK: asyncio.Task | None = None
TOP_LISTS = ("all", "new", "classic")


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, 50))


def _list_items(snapshot: dict, list_type: str, limit: int) -> list[dict]:
    """按需读该轮的新/旧榜条目（含不占主榜名次的 comment_backed 观察项）。"""
    cycle = snapshot.get("cycle") or {}
    cycle_id = cycle.get("id")
    if not cycle_id:
        return []
    connection = store.connect()
    try:
        return store.list_items(connection, int(cycle_id), list_type, limit)
    finally:
        connection.close()


def _llm_summary(snapshot: dict) -> dict:
    """`/api/top` 回显本轮 LLM 发布门状态（off 时也存在，便于监控统一取字段）。"""
    stats = (snapshot.get("cycle") or {}).get("stats") or {}
    return {
        "mode": stats.get("llm_mode", "off"),
        "calls": stats.get("llm_calls", 0),
        "rounds": stats.get("llm_rounds", 0),
        "approved": stats.get("llm_approved", 0),
        "rejected": stats.get("llm_rejected", 0),
        "reviewed": stats.get("llm_reviewed", 0),
        "shortfall": stats.get("llm_shortfall", 0),
        "gate_applied": bool(stats.get("llm_gate_applied", False)),
        "fallback": bool(stats.get("llm_fallback", False)),
        "fallback_reason": stats.get("llm_fallback_reason", ""),
        "new_list_cycle": stats.get("llm_new_list_cycle"),
    }


async def _broadcast(payload: dict) -> None:
    for queue in list(SUBSCRIBERS):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def _collection_active() -> bool:
    return bool(
        STATE["running"] or (_COLLECT_TASK is not None and not _COLLECT_TASK.done())
    )


async def _run_collection(reason: str) -> None:
    """进程内唯一采集入口：scheduler 与 /api/refresh 共用，RUN_LOCK 兜底串行。"""
    async with RUN_LOCK:
        STATE["running"] = True
        try:
            snapshot = await asyncio.to_thread(pipeline.run_cycle)
            STATE["snapshot"] = snapshot
            STATE["error"] = ""
            await _broadcast(snapshot)
        except Exception as exc:  # noqa: BLE001 - 单轮失败要复位状态、继续服务
            STATE["error"] = str(exc)[:300]
        finally:
            STATE["running"] = False


def _start_collection(reason: str) -> bool:
    """同步占位后再创建任务：检查与置位无 await 间隔，双跑窗口被消除。"""
    global _COLLECT_TASK
    if _collection_active():
        return False
    STATE["running"] = True
    _COLLECT_TASK = asyncio.create_task(_run_collection(reason))
    return True


async def _scheduler() -> None:
    while True:
        _start_collection("scheduler")  # 手动刷新在跑时本轮 tick 跳过，不排队
        await asyncio.sleep(config.CYCLE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SCHEDULER_TASK
    connection = store.connect()
    store.init_db(connection)
    try:
        aborted = store.abort_running_cycles(connection)
        if aborted:
            STATE["error"] = f"启动时回收 {aborted} 个未收尾的采集轮次"
    finally:
        connection.close()
    try:
        STATE["snapshot"] = await asyncio.to_thread(pipeline.snapshot)
    except Exception as exc:  # noqa: BLE001
        STATE["error"] = str(exc)[:300]
        STATE["snapshot"] = {"status": "empty", "top": [], "cycle": None}
    if not config.DISABLE_SCHEDULER and not config.OFFLINE:
        _SCHEDULER_TASK = asyncio.create_task(_scheduler())
    try:
        yield
    finally:
        if _SCHEDULER_TASK:
            _SCHEDULER_TASK.cancel()
            try:
                await _SCHEDULER_TASK
            except asyncio.CancelledError:
                pass
            _SCHEDULER_TASK = None


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
    """榜单 API：`?list=new|classic|all`（默认 all，保持旧行为兼容）。"""
    if list_filter not in TOP_LISTS:
        raise HTTPException(status_code=400, detail="list 仅支持 new|classic|all")
    snapshot = STATE["snapshot"]
    if not snapshot or not snapshot.get("top"):
        snapshot = await asyncio.to_thread(pipeline.snapshot)
        STATE["snapshot"] = snapshot
    if list_filter == "all":
        top = [item for item in snapshot.get("top", []) if item.get("rank", 0) > 0]
    else:
        top = await asyncio.to_thread(
            _list_items, snapshot, list_filter, _clamp_limit(limit)
        )
    return {
        "status": snapshot.get("status", "empty"),
        "cycle": snapshot.get("cycle"),
        "list": list_filter,
        "top": top[: _clamp_limit(limit)],
        "health": snapshot.get("health", {}),
        "llm": _llm_summary(snapshot),
        "running": STATE["running"],
        "error": STATE["error"],
    }


@app.get("/api/meme-info")
async def api_meme_info(limit: int = 10, only: str = "all") -> dict:
    """只读 shadow：回显 meme_info 模块报告（不写库、不影响 /api/top）。

    ``only=all|publishable``；``publishable`` 只筛「hot/rising + 2×2 证据」标记。
    ``status=missing|invalid|stale|ok`` 始终 200，坏文件不 500。
    """
    if only not in meme_info_adapter.ONLY_VALUES:
        raise HTTPException(status_code=400, detail="only 仅支持 all|publishable")
    return await asyncio.to_thread(
        meme_info_adapter.load_report, None, limit, only
    )


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
        scheduler_running = _SCHEDULER_TASK is not None and not _SCHEDULER_TASK.done()
        return {"scheduler": scheduler_running,
                "running": STATE["running"],
                "error": STATE["error"],
                "offline": config.OFFLINE,
                "ready": bool(STATE["snapshot"] and STATE["snapshot"].get("top")),
                "health": store.health(connection)}
    finally:
        connection.close()


@app.post("/api/refresh")
async def api_refresh(request: Request) -> dict:
    token = request.headers.get("x-token") or request.query_params.get("token")
    if config.REFRESH_TOKEN and token != config.REFRESH_TOKEN:
        raise HTTPException(status_code=403, detail="token 无效")
    if _collection_active():
        raise HTTPException(status_code=409, detail="已有采集任务在跑")
    if config.OFFLINE:
        raise HTTPException(status_code=400, detail="离线模式不可刷新")
    if not _start_collection("manual"):
        raise HTTPException(status_code=409, detail="已有采集任务在跑")
    return {"started": True}


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    SUBSCRIBERS.add(queue)

    async def generator():
        try:
            initial = STATE["snapshot"] or {"status": "empty", "top": []}
            yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            SUBSCRIBERS.discard(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
