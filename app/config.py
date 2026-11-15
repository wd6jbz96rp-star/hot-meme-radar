"""应用配置：只保留只读服务、历史快照与 meme_info 报告路径。

旧采集/排名 pipeline 已于 2026-11-15 退役，B站/LLM 参数在 `meme_info` 模块内。
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("HOTMEME_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = Path(os.getenv("HOTMEME_DB", str(DATA_DIR / "hotmeme.db")))
STATIC_DIR = Path(__file__).resolve().parent / "static"

# meme_info shadow 报告：worker 原子写 JSON，app 只读展示，不写库。
MEME_INFO_REPORT_PATH = Path(
    os.getenv("HOTMEME_MEME_INFO_REPORT", "/tmp/meme_info_latest.json")
)
MEME_INFO_MAX_AGE_SECONDS = _env_int("HOTMEME_MEME_INFO_MAX_AGE", 3600)

OFFLINE = os.getenv("HOTMEME_OFFLINE", "0") == "1"
HOST = os.getenv("HOTMEME_HOST", "127.0.0.1")
PORT = int(os.getenv("HOTMEME_PORT", "8000"))

# 历史快照展示条数（不是采集参数）。
TOP_N = 10

# store.py 仍保留的历史表 TTL/缓存读取参数（不再写入新业务行）。
TEXT_SAMPLE_TTL_DAYS = 30
METRIC_TTL_DAYS = 90
CACHE_TTL_SECONDS = 24 * 3600
COMMENT_CACHE_SECONDS = _env_int("HOTMEME_COMMENT_CACHE_SECONDS", 30 * 60)
