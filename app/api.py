"""只读 FastAPI 服务：仅暴露 meme_info 报告，不再有历史榜/数据库/采集入口。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from . import config, meme_info_adapter


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="网络热梗动态 Top10", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    path = config.STATIC_DIR / "index.html"
    try:
        return HTMLResponse(path.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("<h1>看板未安装</h1>", status_code=500)


@app.get("/api/meme-info")
async def api_meme_info(limit: int = 10, only: str = "all") -> dict:
    """新梗榜/模块 shadow：只读 /tmp 报告。"""
    if only not in meme_info_adapter.ONLY_VALUES:
        raise HTTPException(status_code=400, detail="only 仅支持 all|publishable")
    return await asyncio.to_thread(meme_info_adapter.load_report, None, limit, only)


@app.get("/api/health")
async def api_health() -> dict:
    try:
        report = await asyncio.to_thread(meme_info_adapter.load_report, None, 1, "all")
    except Exception as exc:  # noqa: BLE001
        return {"scheduler": False, "running": False, "offline": config.OFFLINE,
                "ready": False, "error": str(exc)[:200], "report": None}
    healthy = report.get("status") in ("ok", "stale") and int(report.get("total_memes") or 0) > 0
    return {
        "scheduler": False,
        "running": False,
        "offline": config.OFFLINE,
        "ready": bool(healthy),
        "error": "",
        "report": {
            "status": report.get("status"),
            "generated_at": report.get("generated_at"),
            "age_seconds": report.get("age_seconds"),
            "count": report.get("count"),
        },
    }


@app.post("/api/refresh", deprecated=True)
async def api_refresh(request: Request) -> dict:
    """旧采集入口已退役；新梗榜由 meme_info worker 刷新。"""
    raise HTTPException(
        status_code=410,
        detail="旧采集 pipeline 已退役；新梗榜由 meme_info worker 刷新（见 docs/pm/LIVE-DEPLOY.md）",
    )


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    """兼容 SSE：只发空初始快照 + ping；新梗榜/模块 shadow 走 /api/meme-info 轮询。"""
    async def generator():
        yield 'data: {"status": "empty", "top": []}\n\n'
        while True:
            await asyncio.sleep(20)
            yield ": ping\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
