"""全局配置：所有可调参数集中在此，可用环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("HOTMEME_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = Path(os.getenv("HOTMEME_DB", str(DATA_DIR / "hotmeme.db")))
STATIC_DIR = Path(__file__).resolve().parent / "static"
FIXTURE_DIR = Path(os.getenv("HOTMEME_FIXTURES", str(BASE_DIR / "tests" / "fixtures")))

OFFLINE = os.getenv("HOTMEME_OFFLINE", "0") == "1"
DISABLE_SCHEDULER = os.getenv("HOTMEME_DISABLE_SCHEDULER", "0") == "1"
CYCLE_INTERVAL_SECONDS = int(os.getenv("HOTMEME_INTERVAL", "600"))
HTTP_TIMEOUT = float(os.getenv("HOTMEME_TIMEOUT", "12"))
MAX_RPS = float(os.getenv("HOTMEME_MAX_RPS", "2.0"))  # 合规线 2 rps（docs/02 §7）
HOST = os.getenv("HOTMEME_HOST", "127.0.0.1")
PORT = int(os.getenv("HOTMEME_PORT", "8000"))
REFRESH_TOKEN = os.getenv("HOTMEME_TOKEN", "")
USER_AGENT = os.getenv(
    "HOTMEME_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

# ---- 榜单口径 ----
TOP_N = 10
WINDOW_DAYS = 7
HALF_LIFE_HOURS = 48.0
MIN_SCORE = 55.0          # 稳定上榜阈值（0-100 综合分）
OBSERVE_MIN = 45.0        # 观察池阈值（0-100 综合分）
MEME_MIN = 0.45           # 梗性判定分入池线（0-1，docs/01 §6）
CONFIRM_ROUNDS = 2        # 连续 N 轮达标才稳定上榜
NEWCOMER_PERCENTILE = 0.95
MIN_VIDEOS_FOR_RANK = 2   # 至少出现在 2 个视频中才进稳定榜

# ---- 采集量 ----
VIDEOS_PER_ZONE = 25
COMMENT_VIDEOS = 30
DANMAKU_VIDEOS = 50
SEARCH_DANMAKU_VIDEOS = 25
SEARCH_COMMENT_VIDEOS = 10
COMMENT_PAGES = 2
MAX_CORPUS_CHARS = 160_000
MAX_DANMAKU_PER_VIDEO = 400
SEARCH_QUERIES = [
    "网络热梗", "热梗", "梗百科", "什么梗", "年度热梗", "梗合集",
    "2026热梗", "2025热梗", "2024热梗", "热梗盘点", "网络流行语",
    "弹幕梗", "烂梗", "神评论", "搞笑名场面", "梗王", "万物皆可梗",
    "互联网黑话", "热梗挑战", "抽象",
]
SEARCH_RESULTS_PER_QUERY = 8

# ---- 候选发现 ----
MIN_NGRAM = 2
MAX_NGRAM = 6
MIN_COUNT = 3
MIN_DF = 2
MIN_COHESION = 3.0        # log2 PMI 下限
MIN_ENTROPY = 1.0         # 左右邻接熵下限
MAX_CANDIDATES = 1500
MAX_SURFACE_LEN = 12

# ---- 打分权重（合计 1.0）----
WEIGHTS = {
    "exposure": 0.30,
    "engagement": 0.22,
    "breadth": 0.18,
    "velocity": 0.18,
    "meme": 0.12,
}

# ---- 数据保留 ----
TEXT_SAMPLE_TTL_DAYS = 30
METRIC_TTL_DAYS = 90
CACHE_TTL_SECONDS = 24 * 3600
COMMENT_CACHE_SECONDS = int(os.getenv("HOTMEME_COMMENT_CACHE_SECONDS", str(30 * 60)))
