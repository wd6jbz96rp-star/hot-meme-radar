"""全局配置：所有可调参数集中在此，可用环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    """1/true/yes/on → True；0/false/no/off → False；非法值回 default。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    return default


def _env_bounded_int(name: str, default: int, low: int, high: int) -> int:
    """整数 env；非整数或越界（含 0/负数）都回 default。"""
    value = _env_int(name, default)
    return value if low <= value <= high else default


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
# v2 P4（docs/pm/BOARD.md §17）：评论 45 视频 ×（mode3 1 页 + mode2 3 页）=180 请求，
# 弹幕保持 10 视频、每视频 ≤200 条，只作「梗句扩散」旁证；总请求预算 ≈210–230。
COMMENT_VIDEOS = 45
DANMAKU_VIDEOS = 10
SEARCH_DANMAKU_VIDEOS = 2
SEARCH_COMMENT_VIDEOS = 10
# mode=2 单页只回 3–9 条但可用 next=0/20/40 翻页（P0 实测 4 页 60 条不重复），
# 因此按 mode 配页数；缓存键 (aid, mode, page) 已含页号，命中不请求。
COMMENT_PAGES = {3: 1, 2: 3}
# P4 评论页失败重试：仅对可重试故障（HTTP 412/403、5xx、HTML/空、非 JSON、code=-352）。
# 首次失败后指数退避 0.5/1/2s，最多 3 次重试（4 次尝试）；-404/无评论等业务空不重试。
COMMENT_RETRY_DELAYS = (0.5, 1.0, 2.0)
COMMENT_MAX_ATTEMPTS = len(COMMENT_RETRY_DELAYS) + 1
# 单轮最多重试次数（保护冷轮耗时）：45 视频 180 页的约 22%。
COMMENT_RETRY_BUDGET = 40
# 失败页占比超过该阈值才把 cycle 标 degraded；单页失败不降级整轮。
COMMENT_FAIL_RATE_MAX = 0.20
# 热度评论 mode=3 / 时间序评论 mode=2（`/x/v2/reply/main`）
COMMENT_MODES = (3, 2)
COMMENT_PAGE_SIZE = 20
COMMENT_WINDOW_DAYS = 30        # 评论证据窗口（≤30 天）
COMMENT_RECENT_DAYS = 7         # 增速窗口（≤7 天）
COMMENT_MAX_PER_VIDEO = 40      # 合并两种 mode 后每视频保留条数
COMMENT_GATE_MIN_DOCS = 200     # 本轮评论文档 ≥ 此值才启用评论证据门控
COMMENT_LIKE_HALF_LIFE_DAYS = 14.0  # 评论点赞按年龄衰减的半衰期

# ---- 评论证据入池（P1）----
# 非种子候选满足「≤30 天评论 ≥3、覆盖 ≥2 视频、≤7 天 ≥1」可绕过 MEME_MIN，
# 但只进 observing（不直接 hot）；种子仍走原规则。
COMMENT_BACKED_MIN_RECENT = 3
COMMENT_BACKED_MIN_VIDEOS = 2
COMMENT_BACKED_MIN_7D = 1

# ---- novelty / 新旧榜（P1）----
# novelty：评论活跃度 × 年龄新鲜度（0-1）；种子年代用「已知梗年龄」，非种子用最早评论。
NOVELTY_HALF_LIFE_YEARS = 2.0   # 年龄新鲜度半衰期：2 年后 novelty 打五折
NOVELTY_VOLUME_CAP = 20         # ≤7 天评论 20 条即视为满量（log1p 压缩）
NOVELTY_SPREAD_CAP = 5          # 覆盖 5 个视频即视为满分扩散
SEED_ORIGIN_NEW_YEAR = 2024     # 种子 origin >= 此年份算 new
NON_SEED_NEW_MAX_AGE_DAYS = 90  # 非种子 age_days <= 90 且有 7 天评论才算 new
RANK_N = 30                     # 落库排行池（Top10 展示，列表过滤可读 30 条）
MAX_CORPUS_CHARS = 160_000
MAX_DANMAKU_PER_VIDEO = 200
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
# P1：从 exposure 挪 0.05 给 novelty（评论活跃 × 年龄新鲜度），老梗不再只靠曝光。
WEIGHTS = {
    "exposure": 0.25,
    "engagement": 0.22,
    "breadth": 0.18,
    "velocity": 0.18,
    "meme": 0.12,
    "novelty": 0.05,
}

# ---- LLM 审核发布门（P3，docs/02 §5.1）----
# off 默认：零调用、零行为变化；其余模式只影响 list=new，classic/all 保持规则榜。
LLM_MODES = ("off", "shadow", "demote", "require_approve")
LLM_MODE = (os.getenv("HOTMEME_LLM_MODE", "off").strip().lower() or "off")
if LLM_MODE not in LLM_MODES:   # 非法值按 off 处理，避免配置错字放大风险
    LLM_MODE = "off"
LLM_MAX_ROUNDS = max(1, _env_int("HOTMEME_LLM_MAX_ROUNDS", 3))
# P6：审核池上限（默认 24）与单轮调用预算（默认 24）对齐，保证池内每条都能审到。
# 旧的 50 池 + 18 调用必然「池审不完」，被规则垃圾占满时抽取候选连一次都轮不到；
# 真实 shadow 实测审核单条 ≈ ¥0.0015–0.002，24 条 ≈ ¥0.05，仍在 ≤¥0.07/轮预算内。
LLM_MAX_CALLS = max(0, _env_int("HOTMEME_LLM_MAX_CALLS", 24))
LLM_TARGET = max(1, _env_int("HOTMEME_LLM_TARGET", 10))
LLM_REVIEW_LIMIT = max(1, min(50, _env_int("HOTMEME_LLM_REVIEW_LIMIT", 24)))
LLM_BUDGET_SECONDS = max(0.0, _env_float("HOTMEME_LLM_BUDGET_SECONDS", 60.0))

# ---- LLM 候选抽取（P5，docs/02 §5.2）----
# 默认 0：零调用、零行为变化；开=1 时在 discover 之前用近期评论抽候选。
LLM_EXTRACT_ENABLED = _env_flag("HOTMEME_LLM_EXTRACT", False)
# 抽取预算：3 批 = 450 条评论（200/批）的默认组合，硬上限 3 批（P4a MAX_BATCHES）。
LLM_EXTRACT_MAX_CALLS = _env_bounded_int("HOTMEME_LLM_EXTRACT_MAX_CALLS", 3, 1, 3)
LLM_EXTRACT_MAX_COMMENTS = _env_bounded_int(
    "HOTMEME_LLM_EXTRACT_MAX_COMMENTS", 450, 1, 2000
)

# ---- 数据保留 ----
TEXT_SAMPLE_TTL_DAYS = 30
METRIC_TTL_DAYS = 90
CACHE_TTL_SECONDS = 24 * 3600
COMMENT_CACHE_SECONDS = int(os.getenv("HOTMEME_COMMENT_CACHE_SECONDS", str(30 * 60)))
