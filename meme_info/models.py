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
    #: B站弹幕唯一 ID（XML ``p`` 属性第 8 段）；老格式拿不到时为空。
    #: ``id`` 保持历史 sha1(text+progress) 兼容，窗口计数去重优先用 ``dmid``。
    dmid: str = ""


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


# ---------------------------------------------------------------- B 线：视频候选/指标
@dataclass
class VideoCandidate:
    """B 线候选视频（多来源去重后的发现结果，尚未测量在线/新增）。

    ``sources`` 保留全部命中来源（如 ``ranking:119``、``popular``、``search:热梗``），
    ``source`` 是主来源；``exploration`` 标记探索来源（newlist / 按发布时间搜索 /
    相关推荐 / 冷门分区），用于 300 候选池的 240 常规 + 60 探索配额。
    """

    bvid: str
    title: str = ""
    aid: int = 0
    cid: int | None = None
    zone: str = ""
    zone_rid: int = 0
    pubdate: int = 0
    view: int = 0
    reply: int = 0
    danmaku: int = 0
    owner: str = ""
    source: str = ""
    sources: list[str] = field(default_factory=list)
    source_rank: int = 0
    exploration: bool = False
    exploration_source: str = ""
    #: 统计字段是否可信：ranking/popular/related 带 stat；search/newlist 需再查 view
    stats_known: bool = False
    #: /x/player/online/total 的当前在线人数（失败为 None）
    online: int | None = None
    error: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""

    @property
    def age_minutes(self) -> float:
        return 0.0


@dataclass
class VideoMetric:
    """单个候选视频的本轮指标，与 Postgres ``video_metric`` 行一一对应。"""

    bvid: str
    cid: int | None = None
    title: str = ""
    zone: str = ""
    pubdate: int = 0
    source: str = ""
    view: int = 0
    reply: int = 0
    danmaku: int = 0
    online: int | None = None
    new_view: int | None = None
    new_reply: int | None = None
    new_danmaku: int | None = None
    #: log1p(新增播放速率)；首见为 log1p(view/(age_minutes+60))
    play_proxy: float | None = None
    hot_score: float = 0.0
    selected_comment: bool = False
    selected_danmaku: bool = False
    error: str = ""
    # ---- 计算明细（不进 DB 列，随 JSON 输出供复算）----
    exploration: bool = False
    exploration_source: str = ""
    age_minutes: float = 0.0
    first_seen: bool = True
    dt_minutes: float | None = None
    stats_known: bool = False
    online_z: float = 0.0
    comment_z: float = 0.0
    danmaku_z: float = 0.0
    play_z: float = 0.0
    comment_rate: float | None = None
    danmaku_rate: float | None = None
    play_rate: float | None = None

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""

    @property
    def danmaku_rank_score(self) -> float:
        """弹幕目标排序分：新增弹幕为主（0.6）+ 视频热度（0.4）。"""
        return 0.6 * self.danmaku_z + 0.4 * self.hot_score


@dataclass
class VideoRouteResult:
    """B 线单轮结果（可 JSON 序列化；含 DB 写入的 round id）。"""

    generated_at: str = ""
    started_at: int = 0
    window_start: int = 0
    window_end: int = 0
    status: str = "ok"
    round_id: int | None = None
    candidates: list[VideoCandidate] = field(default_factory=list)
    metrics: list[VideoMetric] = field(default_factory=list)
    selected_comment: list[str] = field(default_factory=list)
    selected_danmaku: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return as_jsonable(self)


# ---------------------------------------------------------------- C 线：15 分钟窗口
@dataclass
class WindowTarget:
    """Phase C 采样目标（来自本轮 ``video_metric`` 的 selected/兜底，或显式注入）。"""

    bvid: str
    aid: int = 0
    cid: int | None = None
    title: str = ""
    zone: str = ""
    reply: int = 0
    danmaku: int = 0
    hot_score: float = 0.0
    #: selected / fallback_hot / video_route / targets_json / discover / injected
    source: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""


@dataclass
class WindowComment:
    """15 分钟窗口内的新评论（比例计数）；``evidence=True`` 表示 30 天证据。"""

    id: str
    bvid: str
    text: str
    ctime: int = 0
    like: int = 0
    zone: str = ""
    video_title: str = ""
    mode: int = 2
    evidence: bool = False

    def to_comment(self) -> Comment:
        return Comment(
            id=self.id,
            bvid=self.bvid,
            text=self.text,
            ctime=self.ctime,
            like=self.like,
            zone=self.zone,
            video_title=self.video_title,
            mode=self.mode,
        )


@dataclass
class WindowDanmaku:
    """15 分钟窗口内的新弹幕（按 ``dmid`` 去重）；``evidence=True`` 表示 30 天证据。"""

    id: str
    bvid: str
    text: str
    ctime: int = 0
    dmid: str = ""
    zone: str = ""
    video_title: str = ""
    progress: float = 0.0
    evidence: bool = False

    def to_danmaku(self) -> Danmaku:
        return Danmaku(
            id=self.id,
            bvid=self.bvid,
            text=self.text,
            ctime=self.ctime,
            zone=self.zone,
            video_title=self.video_title,
            progress=self.progress,
            dmid=self.dmid,
        )


@dataclass
class WindowSample:
    """Phase C 单轮结果：15 分钟计数样本 + 30 天证据（字段分开，避免混用）。

    ``comments`` / ``danmaku`` 是比例计数样本；``evidence_comments`` /
    ``evidence_danmaku`` 是可信度证据，不参与比例。
    """

    generated_at: str = ""
    started_at: int = 0
    window_start: int = 0
    window_end: int = 0
    status: str = "ok"                # ok / degraded
    round_id: int | None = None
    comments: list[WindowComment] = field(default_factory=list)
    danmaku: list[WindowDanmaku] = field(default_factory=list)
    evidence_comments: list[WindowComment] = field(default_factory=list)
    evidence_danmaku: list[WindowDanmaku] = field(default_factory=list)
    comment_targets: list[WindowTarget] = field(default_factory=list)
    danmaku_targets: list[WindowTarget] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def comments_sampled(self) -> int:
        return len(self.comments)

    @property
    def danmaku_sampled(self) -> int:
        return len(self.danmaku)

    def to_dict(self) -> dict[str, Any]:
        return as_jsonable(self)

    def docs(self, *, evidence: bool = False) -> list[Doc]:
        """转成统一 ``Doc`` 语料（供 Phase D 抽取/计数复用）；默认只给窗口样本。"""
        docs: list[Doc] = []
        comments = self.evidence_comments if evidence else self.comments
        danmaku = self.evidence_danmaku if evidence else self.danmaku
        for comment in comments:
            docs.append(
                Doc(
                    id=comment.id,
                    text=comment.text,
                    kind="comment",
                    bvid=comment.bvid,
                    ctime=comment.ctime,
                    like=comment.like,
                    zone=comment.zone,
                    video_title=comment.video_title,
                    source="bilibili",
                )
            )
        for item in danmaku:
            docs.append(
                Doc(
                    id=item.id,
                    text=item.text,
                    kind="danmaku",
                    bvid=item.bvid,
                    ctime=item.ctime,
                    zone=item.zone,
                    video_title=item.video_title,
                    source="bilibili",
                )
            )
        return docs


# ---------------------------------------------------------------- D 线：family 观察与池状态
@dataclass
class FamilyObservation:
    """单个 family 的本轮观察（Phase D 输入/输出；可 JSON 序列化）。

    计数口径（PM 2026-11-22 复核修正）：
    - ``comment_count`` / ``danmaku_count``：15 分钟窗口内去重评论 id / 弹幕 dmid 命中数；
    - ``video_count``：**15 分钟窗口**内命中该 family 的评论/弹幕所在独立 bvid 数；
    - ``evidence_video_count``：30 天证据里命中的独立 bvid 数（只进 evidence_json/stats，
      不参与 count_total/比例/池2门槛）；
    - ``meta_count``：30 天元话语视频数（不参与比例）；
    - ``count_total = comment_count + danmaku_count + video_count``（比例计数）；
    - ``usage_count = comment_count + danmaku_count``（池2「有使用」门槛，口径统一）。
    """

    family_key: str
    canonical: str
    norm: str = ""
    aliases: list[str] = field(default_factory=list)
    #: 本轮是否出现在活跃语料（窗口/证据/元话语任一命中）或发现结果里
    present: bool = False
    discovered: bool = False
    comment_count: int = 0
    danmaku_count: int = 0
    video_count: int = 0
    #: 30 天证据里命中的独立 bvid 数（展示/证据，不参与比例）
    evidence_video_count: int = 0
    meta_count: int = 0
    count_total: int = 0
    usage_count: int = 0
    # ---- cred_base 五特征（0–1）----
    meta_strength: float = 0.0
    usage_strength: float = 0.0
    spread: float = 0.0
    freshness: float = 0.0
    prior: float = 0.0
    cred_base: float = 0.0
    # ---- 身份/规则输入 ----
    known: bool = False              # 命中 knowledge 种子
    human_confirmed: bool = False    # audit_state == human_pass（Phase F 写入）
    seed_origin: str = ""
    meta_only: bool = False
    explainer_eligible: bool = False
    # ---- 2×2 徽标（只展示，不参与硬门槛）----
    badge_2x2: bool = False
    badge_docs: int = 0
    badge_videos: int = 0
    # ---- 明细（不进 DB 列）----
    zones: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    meta_kinds: list[str] = field(default_factory=list)
    meta_bvids: list[str] = field(default_factory=list)
    #: 独立强元话语视频数（dedicated/compilation 去重 bvid）
    meta_independent: int = 0
    dedicated_meta: int = 0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    latest_evidence_ts: int = 0


@dataclass
class Pool1Row:
    """``pool1`` 一行（读写共用；evidence_json 为 dict 包装，见 pools 模块）。"""

    family_key: str
    canonical: str = ""
    aliases: list[str] = field(default_factory=list)
    status: str = "normal"           # normal / provisional / out
    score: float = 0.0
    count_prev: int = 0
    count_now: int = 0
    zero_streak: int = 0
    audit_state: str = "none"
    audit_hash: str = ""
    audit_adjustment: float = 0.0
    meta_only: bool = False
    explainer_only: bool = False
    first_seen_round: int | None = None
    last_seen_round: int | None = None
    evidence_json: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""
    #: 非 DB 字段：本轮观察是否命中 knowledge 种子（只用于 family_alias.source 判定）
    known: bool = False


@dataclass
class Pool2Row:
    """``pool2`` 一行（heat_score/rank 由 Phase E 计算，本轮可空）。"""

    family_key: str
    round_id: int | None = None
    heat_score: float | None = None
    rank: int | None = None
    reason: str = ""
    explainer_only: bool = False


@dataclass
class FamilyRoundStat:
    """``family_round_stat`` 一行（每 family 每轮一条，含 count=0）。"""

    round_id: int | None
    family_key: str
    comment_count: int = 0
    danmaku_count: int = 0
    video_count: int = 0
    meta_count: int = 0
    count_total: int = 0
    ratio: float | None = None
    score_before: float = 0.0
    score_after: float = 0.0
    delta: float = 0.0
    reason: str = ""


@dataclass
class PoolTransitionRow:
    """``pool_transition`` 一行（池成员变化审计）。"""

    family_key: str
    round_id: int | None = None
    from_pool: str | None = None
    to_pool: str | None = None
    reason: str = ""
    score: float = 0.0


@dataclass
class FamilyAliasRow:
    """``family_alias`` 一行（alias 唯一；source = seed / manual / auto）。"""

    family_key: str
    alias: str
    norm_alias: str
    source: str = "auto"
    confidence: float = 0.5


@dataclass
class PoolRoundResult:
    """Phase D 单轮结果（可 JSON 序列化；纯内存模式也返回完整状态）。"""

    generated_at: str = ""
    round_id: int | None = None
    window_start: int = 0
    window_end: int = 0
    status: str = "ok"                 # ok / degraded
    degraded: bool = False
    observations: list[FamilyObservation] = field(default_factory=list)
    pool1: list[Pool1Row] = field(default_factory=list)
    pool2: list[Pool2Row] = field(default_factory=list)
    stats_rows: list[FamilyRoundStat] = field(default_factory=list)
    transitions: list[PoolTransitionRow] = field(default_factory=list)
    aliases: list[FamilyAliasRow] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return as_jsonable(self)
