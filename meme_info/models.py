"""热梗信息模块的数据契约。

所有对外输出都可直接 ``json.dumps``；字段名与 ``docs/pm/ENGINEER-HANDOFF.md``
的 Comment / Evidence / Candidate / Verdict 保持兼容，额外字段只增不改。

设计约束：
- 不依赖 app、SQLite、FastAPI；只用标准库。
- 时间是 Unix 秒（int）；输出报告里另给 ISO 便于人读。
- 证据的 quote 必须是原文子串，模型层不负责编造。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "meme-info/v1"
PROMPT_VERSION = "meme-info/verify-v4"


# ---------------------------------------------------------------- 基础工具
def as_jsonable(value: Any) -> Any:
    """把 dataclass / set / datetime 递归转成可 JSON 序列化的结构。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field_.name: as_jsonable(getattr(value, field_.name))
            for field_ in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): as_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(as_jsonable(item) for item in value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, float):
        return round(value, 4) if value == value else 0.0  # NaN -> 0
    return value


def dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(as_jsonable(value), ensure_ascii=False, indent=indent, sort_keys=False)


def stable_id(*parts: Any, length: int = 16) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def now_ts() -> int:
    return int(time.time())


def iso(ts: int | float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def parse_iso(value: str | None) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp())


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


# ---------------------------------------------------------------- 采集层
@dataclass
class Video:
    bvid: str
    aid: int = 0
    cid: int | None = None
    title: str = ""
    desc: str = ""
    pubdate: int = 0
    duration: int = 0
    tid: int = 0
    tname: str = ""
    zone: str = ""
    owner: str = ""
    owner_mid: int = 0
    view: int = 0
    like: int = 0
    coin: int = 0
    favorite: int = 0
    share: int = 0
    reply: int = 0
    danmaku: int = 0
    source: str = ""          # ranking:<rid> / popular / search:<query> / expansion
    source_rank: int = 0
    query: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""

    @property
    def engagement(self) -> float:
        if self.view <= 0:
            return 0.0
        return (self.like + self.coin + self.favorite + self.reply + self.danmaku) / self.view


@dataclass
class Comment:
    id: str
    bvid: str
    text: str
    ctime: int
    like: int = 0
    zone: str = ""
    video_title: str = ""
    mode: int = 3
    aid: int = 0


@dataclass
class Danmaku:
    id: str
    bvid: str
    text: str
    ctime: int = 0
    zone: str = ""
    video_title: str = ""
    progress: float = 0.0


@dataclass
class Doc:
    """统一语料文档：标题/简介/标签/评论/弹幕。"""

    id: str
    text: str
    kind: str                  # title / desc / tag / comment / danmaku
    bvid: str = ""
    ctime: int = 0
    like: int = 0
    zone: str = ""
    video_title: str = ""
    source: str = ""


# ---------------------------------------------------------------- 证据层
@dataclass
class Evidence:
    comment_id: str
    quote: str
    ctime: int = 0
    like: int = 0
    bvid: str = ""
    kind: str = "comment"
    video_title: str = ""
    zone: str = ""


@dataclass
class Usage:
    """一条「被使用」证据，保留全量计数（Evidence 只留展示样本）。"""

    doc_id: str
    bvid: str
    zone: str = ""
    kind: str = "comment"
    ctime: int = 0
    like: int = 0
    text: str = ""
    video_title: str = ""


@dataclass
class MetaMention:
    """梗的元话语证据：一个专门讲这个梗的视频（梗百科/梗指南/盘点）。"""

    surface: str
    canonical: str
    norm: str
    bvid: str
    title: str
    pubdate: int = 0
    view: int = 0
    like: int = 0
    reply: int = 0
    danmaku: int = 0
    owner: str = ""
    query: str = ""
    kind: str = "dedicated"   # dedicated / compilation / quote / hashtag / expansion
    confidence: float = 0.5

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""


# ---------------------------------------------------------------- 候选/梗
@dataclass
class Candidate:
    """候选梗面（未归并）。字段与交接单 Candidate 兼容。"""

    surface: str
    canonical: str
    norm: str
    source: str = "rule"       # meta / seed / rule / n-gram / danmaku / llm_extract
    type: str = "词"
    meaning: str = ""
    origin_quote: str = ""
    origin_ref: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    meta: list[MetaMention] = field(default_factory=list)
    confidence: float = 0.0
    known: bool = False
    count: int = 0
    df_video: int = 0
    df_zone: int = 0
    df_up: int = 0
    comment_7d_count: int = 0
    comment_df_video: int = 0
    age_days: float = 0.0
    first_seen: int = 0
    last_seen: int = 0
    like_weighted: float = 0.0
    pattern: bool = False
    quoted: int = 0
    is_line: bool = False
    cohesion: float = 0.0
    entropy: float = 0.0
    sources: list[str] = field(default_factory=list)

    @property
    def candidate_id(self) -> str:
        return stable_id(self.norm)

    @property
    def evidence_ids(self) -> list[str]:
        seen: list[str] = []
        for item in self.evidence:
            if item.comment_id and item.comment_id not in seen:
                seen.append(item.comment_id)
        return seen


@dataclass
class Meme:
    """归并后的梗族，是查询与打分的单位。"""

    family_key: str
    canonical: str
    norm: str
    type: str = "词"
    meaning: str = ""
    origin_quote: str = ""
    origin_ref: str = ""
    members: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    meta: list[MetaMention] = field(default_factory=list)
    known: bool = False
    seed_origin: str = ""
    seed_origin_year: int | None = None
    first_seen: int = 0
    last_seen: int = 0
    sources: list[str] = field(default_factory=list)

    # ---- 打分结果（由 heat.HeatModel 写入）----
    score: float = 0.0
    raw_score: float = 0.0
    smoothed_score: float | None = None
    status: str = "candidate"      # hot / rising / observing / rejected
    publishable: bool = False      # hot/rising + 2×2 独立使用；发布门用
    heat_level: str = ""
    confidence: float = 0.0
    meme_prob: float = 0.0
    components: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    rejection: str = ""
    signals: dict[str, Any] = field(default_factory=dict)
    verified: dict[str, Any] | None = None

    @property
    def evidence_ids(self) -> list[str]:
        seen: list[str] = []
        for item in self.evidence:
            if item.comment_id and item.comment_id not in seen:
                seen.append(item.comment_id)
        return seen

    @property
    def age_days(self) -> float:
        if not self.first_seen or not self.last_seen:
            return 0.0
        return max(0.0, (self.last_seen - self.first_seen) / 86400.0)


# ---------------------------------------------------------------- 语料/报告
@dataclass
class Corpus:
    videos: list[Video] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    danmaku: list[Danmaku] = field(default_factory=list)
    docs: list[Doc] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    plan: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return as_jsonable(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Corpus":
        return cls(
            videos=[Video(**item) for item in payload.get("videos", [])],
            comments=[Comment(**item) for item in payload.get("comments", [])],
            danmaku=[Danmaku(**item) for item in payload.get("danmaku", [])],
            docs=[Doc(**item) for item in payload.get("docs", [])],
            stats=payload.get("stats") or {},
            generated_at=payload.get("generated_at") or "",
            plan=payload.get("plan") or {},
        )


@dataclass
class HotMemeReport:
    generated_at: str
    as_of: str
    window_days: int
    memes: list[Meme] = field(default_factory=list)
    observing: list[Meme] = field(default_factory=list)
    candidates: list[Meme] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    prompt_version: str = PROMPT_VERSION
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return as_jsonable(self)
