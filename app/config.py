"""只读服务配置：静态目录、端口、meme_info 报告路径。"""
from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
MEME_INFO_REPORT_PATH = Path(
    os.getenv("HOTMEME_MEME_INFO_REPORT", "/tmp/meme_info_latest.json")
)
MEME_INFO_MAX_AGE_SECONDS = _env_int("HOTMEME_MEME_INFO_MAX_AGE", 3600)
OFFLINE = os.getenv("HOTMEME_OFFLINE", "0") == "1"
HOST = os.getenv("HOTMEME_HOST", "127.0.0.1")
PORT = int(os.getenv("HOTMEME_PORT", "8000"))
