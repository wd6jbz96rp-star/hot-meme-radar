"""候选发现与梗族归并。

两条召回线：
1. **元话语线**（高精度）：搜索到的「梗百科 / 梗指南 / X 是什么梗 / 盘点」视频标题，
   直接给出「当前有人在专门讲这个梗」的证据；
2. **使用线**（高召回）：评论/弹幕里的引号、上下文模式、n-gram 新词与整句重复。

候选必须先有原文证据才能进入输出；证据 quote 由 ``find_quote`` 回映原文，不编造。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .knowledge import Knowledge
from .models import (
    Candidate,
    Doc,
    Evidence,
    Meme,
    MetaMention,
    Usage,
    Video,
    clamp,
    stable_id,
)
from .text import (
    GENERIC_WORDS,
    clean_title,
    clip,
    entropy,
    find_quote,
    has_edge_char,
    is_numeric_like,
    is_short_ascii,
    looks_like_sentence,
    normalize_surface,
    normalize_text,
    segments,
    similar_surface,
)

# 标题里「专门讲梗」的栏目名
META_PREFIXES = ("梗百科", "梗指南", "梗学堂", "梗科普", "白给梗科普", "梗百科大全", "梗辞典", "梗词典")
DEDICATED_SUFFIXES = ("是啥梗", "是什么梗", "什么梗", "这梗", "这个梗")
BAD_META_WORDS = (
    "什么梗", "是啥梗", "热梗", "网络热梗", "烂梗", "梗百科", "梗指南", "梗学堂",
    "盘点", "合集", "总结", "视频", "网友", "新闻", "官方", "外网", "爆火",
    "竟成", "回应", "通报", "发布", "直播", "抽奖", "福利",
)
COMPILATION_WORDS = ("盘点", "合集", "总结", "大盘点", "年度", "top", "TOP", "TOP10")
QUOTE_RE = re.compile(r"[「『“\"《【]([^」』”\"》】\n]{2,24})[」』”\"》】]")
MEME_CONTEXT_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9↑↓←→]{2,12})(?:是什么梗|的梗|梗出处|什么梗|这梗|这个梗|梗吗|梗啊|梗王|梗图)"
)
HASHTAG_RE = re.compile(r"#([^#\n]{2,24})#")
TITLE_PREFIX_RE = re.compile(
    r"^\s*[【\[]\s*(?:" + "|".join(re.escape(word) for word in META_PREFIXES) + r")\s*[】\]]\s*"
)
DEDICATED_RE = re.compile(
    r"(?P<name>[^？?！!。\n]{2,24}?)(?:" + "|".join(re.escape(word) for word in DEDICATED_SUFFIXES) + r")"
)
LEADING_FILLERS = (
    "我怀疑", "我听说", "听说", "据说", "请问", "到底", "你知道", "大家知道",
    "有人知道", "想知道", "好奇", "所以", "那么", "最近", "谁还记得", "还记得",
    "有没有人", "还有人", "请问一下", "我想知道",
)
SUFFIX_TYPES = {
    "文学": "句式", "圣体": "句式", "判官": "句式", "圣经": "句式",
    "定律": "句式", "行为": "句式", "发言": "句式", "宇宙": "句式",
    "含量": "句式", "刺客": "词", "挑战": "句式",
}


# ---------------------------------------------------------------- 标题元话语
def _clean_meta_name(name: str) -> str:
    value = clean_title(name).strip(" \t\r\n“”‘’「」『』《》【】[]()（）\"'")
    value = value.strip("：:，,。.；;、|/ ")
    for filler in LEADING_FILLERS:
        if value.startswith(filler) and len(value) > len(filler) + 1:
            value = value[len(filler):].strip()
            break
    return value.strip()


def _meta_name_ok(name: str, knowledge: Knowledge) -> bool:
    norm = normalize_surface(name)
    if not (1 <= len(norm) <= 20):
        return False
    if len(norm) == 1 and not ("\u4e00" <= norm <= "\u9fff"):
        return False
    if norm in GENERIC_WORDS:
        return False
    if is_numeric_like(norm) or is_short_ascii(norm, 2):
        return False
    if any(word in norm for word in BAD_META_WORDS):
        return False
    # 标题层只做「像不像梗名」的结构判断；通用词/地名/普通短语留给
    # extract_candidates 统一过滤并记 reject reason（便于返工审计）。
    return True


def _split_title_segments(rest: str) -> list[str]:
    parts = re.split(r"[？?！!。\n]+", rest)
    result: list[str] = []
    for part in parts:
        value = _clean_meta_name(part)
        if not value or len(normalize_surface(value)) < 2:
            continue
        # 栏目名后的问句/说明不是梗名（如「xx是同事吗」「为什么希佩…」）。
        if re.search(r"(?:什么|为什么|怎么|是不是|同事|意思|如何|哪些|吗$|呢$)", value):
            continue
        result.append(value)
    return result


def parse_title_meta(title: str, knowledge: Knowledge) -> list[tuple[str, str, float]]:
    """从单条标题抽取 (原始名, 归一化名, 置信度) 列表；不做时间过滤。"""
    value = clean_title(title)
    if not value:
        return []
    found: list[tuple[str, str, float]] = []
    seen: set[str] = set()

    def add(name: str, confidence: float) -> None:
        cleaned = _clean_meta_name(name)
        norm = normalize_surface(cleaned)
        if not norm or norm in seen:
            return
        if not _meta_name_ok(cleaned, knowledge):
            return
        seen.add(norm)
        found.append((cleaned, norm, confidence))

    stripped = TITLE_PREFIX_RE.sub("", value)
    # 1) 明确句式：X 是什么梗 / X 是啥梗 / X 什么梗
    match = DEDICATED_RE.search(stripped)
    if match:
        add(match.group("name"), 0.95)

    # 2) 栏目名后没有「是什么梗」：按问号切段（如「雪豹眉眉？冷静点？」）
    if TITLE_PREFIX_RE.search(value) or stripped != value:
        for segment in _split_title_segments(stripped):
            add(segment, 0.72)

    # 3) 盘点/合集标题的冒号后列表：A、B、C
    if any(word in value for word in COMPILATION_WORDS) and ("：" in value or ":" in value):
        tail = re.split(r"[：:]", value)[-1]
        for part in re.split(r"[、，,|/；;]+", tail):
            add(part, 0.62)

    # 注意：不再从「含梗标题」里泛抽所有引号/话题标签。
    # 实测这会把《原神》《循环歌单》这类作品/栏目名当成梗；宁缺毋滥。
    return found


def extract_meta_mentions(
    videos: Iterable[Video],
    knowledge: Knowledge,
    *,
    now: int,
    max_age_days: int = 365,
) -> list[MetaMention]:
    mentions: list[MetaMention] = []
    seen: set[tuple[str, str]] = set()
    for video in videos:
        if not video.title or not video.bvid:
            continue
        if video.pubdate and now - video.pubdate > max_age_days * 86400:
            continue
        for surface, norm, confidence in parse_title_meta(video.title, knowledge):
            key = (video.bvid, norm)
            if key in seen:
                continue
            seen.add(key)
            # 盘点和引号在标题里往往只是提及，不是「专门讲这个梗」。
            kind = "dedicated" if confidence >= 0.9 else (
                "compilation" if confidence >= 0.6 else "quote"
            )
            mentions.append(
                MetaMention(
                    surface=surface,
                    canonical=surface,
                    norm=norm,
                    bvid=video.bvid,
                    title=video.title,
                    pubdate=video.pubdate,
                    view=video.view,
                    like=video.like,
                    reply=video.reply,
                    danmaku=video.danmaku,
                    owner=video.owner,
                    query=video.query,
                    kind=kind,
                    confidence=confidence,
                )
            )
    mentions.sort(key=lambda item: (item.pubdate, item.view), reverse=True)
    return mentions


# ---------------------------------------------------------------- 文档索引
@dataclass
class PreparedDoc:
    doc: Doc
    norm_text: str
    tokens: list[str]


def prepare_docs(docs: Iterable[Doc], *, max_chars: int = 200_000) -> list[PreparedDoc]:
    prepared: list[PreparedDoc] = []
    total = 0
    for doc in docs:
        text = (doc.text or "").strip()
        if not text:
            continue
        norm = normalize_text(text)
        if not norm:
            continue
        if total + len(norm) > max_chars:
            break
        total += len(norm)
        prepared.append(PreparedDoc(doc=doc, norm_text=norm, tokens=norm.split()))
    return prepared


def _evidence_from_doc(doc: Doc, surface: str, *, limit: int = 120) -> Evidence | None:
    quote = find_quote(doc.text, surface)
    if not quote:
        return None
    return Evidence(
        comment_id=doc.id,
        quote=clip(quote, limit),
        ctime=doc.ctime,
        like=doc.like,
        bvid=doc.bvid,
        kind=doc.kind,
        video_title=doc.video_title,
        zone=doc.zone,
    )


def _usage_from_doc(doc: Doc, text_hit: str | None = None) -> Usage:
    return Usage(
        doc_id=doc.id,
        bvid=doc.bvid,
        zone=doc.zone,
        kind=doc.kind,
        ctime=doc.ctime,
        like=doc.like,
        text=clip(text_hit or doc.text, 120),
        video_title=doc.video_title,
    )


# ---------------------------------------------------------------- 候选抽取
@dataclass
class DiscoveryConfig:
    min_count: int = 3
    min_df: int = 2
    min_cohesion: float = 3.0
    min_entropy: float = 1.0
    min_ngram: int = 2
    max_ngram: int = 6
    max_candidates: int = 2000
    evidence_limit: int = 8
    seed_scan_limit: int = 80


def _new_candidate(
    surface: str,
    *,
    source: str,
    canonical: str = "",
    confidence: float = 0.0,
    known: bool = False,
) -> Candidate:
    norm = normalize_surface(surface)
    return Candidate(
        surface=surface,
        canonical=canonical or surface,
        norm=norm,
        source=source,
        confidence=confidence,
        known=known,
        sources=[source],
    )


def _add_hit(
    candidate: Candidate,
    doc: Doc,
    *,
    surface: str,
    now: int,
    evidence_limit: int,
) -> None:
    evidence = _evidence_from_doc(doc, surface)
    if not evidence:
        return
    candidate.usage.append(_usage_from_doc(doc, evidence.quote))
    if len(candidate.evidence) < evidence_limit:
        candidate.evidence.append(evidence)


def _finalize_candidate(candidate: Candidate, now: int, knowledge: Knowledge) -> Candidate:
    usage = candidate.usage
    seen_docs: set[str] = set()
    unique: list[Usage] = []
    for item in usage:
        if item.doc_id in seen_docs:
            continue
        seen_docs.add(item.doc_id)
        unique.append(item)
    candidate.usage = unique
    candidate.count = max(candidate.count, len(unique))
    candidate.df_video = len({item.bvid for item in unique if item.bvid})
    candidate.df_zone = len({item.zone for item in unique if item.zone})
    candidate.df_up = 0
    times = [item.ctime for item in unique if item.ctime]
    if times:
        candidate.first_seen = min(times)
        candidate.last_seen = max(times)
        candidate.age_days = max(0.0, (now - candidate.first_seen) / 86400.0)
    elif candidate.meta:
        candidate.first_seen = min(item.pubdate for item in candidate.meta if item.pubdate) or 0
        candidate.last_seen = max(item.pubdate for item in candidate.meta if item.pubdate) or 0
        candidate.age_days = max(0.0, (now - candidate.first_seen) / 86400.0) if candidate.first_seen else 999.0
    candidate.comment_7d_count = sum(
        1 for item in unique if item.ctime and now - item.ctime <= 7 * 86400
    )
    candidate.comment_df_video = candidate.df_video
    candidate.like_weighted = sum(
        math.log1p(max(0, item.like)) * math.pow(0.5, max(0, now - item.ctime) / 86400.0 / 14.0)
        for item in unique
        if item.ctime
    )
    candidate.evidence.sort(key=lambda item: (item.like, item.ctime), reverse=True)
    candidate.evidence = candidate.evidence[:8]
    candidate.sources = sorted(set(candidate.sources))
    if not candidate.canonical:
        candidate.canonical = candidate.surface

    # 候选置信度：来源先验 + 证据量 + 元话语。
    prior = 0.0
    if candidate.known:
        prior = 0.85
    elif any(item.kind == "dedicated" for item in candidate.meta):
        prior = 0.75
    elif candidate.meta:
        prior = 0.55
    elif candidate.pattern:
        prior = 0.45
    elif candidate.is_line:
        prior = 0.35
    else:
        prior = 0.22
    volume = min(1.0, math.log1p(len(unique)) / math.log1p(12))
    breadth = min(1.0, candidate.df_video / 5.0)
    candidate.confidence = round(clamp(prior + 0.12 * volume + 0.10 * breadth, 0, 1), 3)
    return candidate


def _merge_duplicate(candidate: Candidate, other: Candidate) -> None:
    if other.known and not candidate.known:
        candidate.known = True
        candidate.canonical = other.canonical
        candidate.confidence = max(candidate.confidence, other.confidence)
    candidate.count = max(candidate.count, other.count)
    candidate.df_video = max(candidate.df_video, other.df_video)
    candidate.df_zone = max(candidate.df_zone, other.df_zone)
    candidate.usage.extend(other.usage)
    candidate.evidence.extend(other.evidence)
    candidate.meta.extend(other.meta)
    candidate.sources = sorted(set(candidate.sources + other.sources))
    candidate.pattern = candidate.pattern or other.pattern
    candidate.quoted = max(candidate.quoted, other.quoted)
    candidate.is_line = candidate.is_line or other.is_line
    candidate.cohesion = max(candidate.cohesion, other.cohesion)
    candidate.entropy = max(candidate.entropy, other.entropy)
    candidate.confidence = max(candidate.confidence, other.confidence)
    if len(other.surface) < len(candidate.surface) and other.source in ("seed", "meta"):
        candidate.surface = other.surface


def _scan_patterns(
    prepared: list[PreparedDoc],
    candidates: dict[str, Candidate],
    knowledge: Knowledge,
    now: int,
    config: DiscoveryConfig,
) -> None:
    for item in prepared:
        doc = item.doc
        text = doc.text
        seen_norms: set[str] = set()

        def add(surface: str, *, pattern: bool = True, quoted: int = 0) -> None:
            norm = normalize_surface(surface)
            if not (2 <= len(norm) <= 24):
                return
            if norm in seen_norms:
                return
            seen_norms.add(norm)
            candidate = candidates.get(norm)
            if candidate is None:
                candidate = _new_candidate(
                    surface,
                    source="pattern",
                    confidence=0.42 if pattern else 0.3,
                )
                candidates[norm] = candidate
            candidate.pattern = candidate.pattern or pattern
            candidate.quoted += quoted
            candidate.sources = sorted(set(candidate.sources + ["pattern"]))
            _add_hit(candidate, doc, surface=surface, now=now, evidence_limit=config.evidence_limit)

        for match in QUOTE_RE.finditer(text):
            surface = match.group(1).strip()
            if 2 <= len(normalize_surface(surface)) <= 20:
                add(surface, quoted=1)
        for match in MEME_CONTEXT_RE.finditer(normalize_text(text)):
            add(match.group(1))
        for match in HASHTAG_RE.finditer(text):
            add(match.group(1))
        if doc.kind == "danmaku":
            for segment in segments(text):
                if 3 <= len(segment) <= 20:
                    add(segment, pattern=True)

    for candidate in candidates.values():
        if candidate.pattern and knowledge.blocked_reason(candidate.surface):
            candidate.confidence = 0.0


def _scan_ngrams(
    prepared: list[PreparedDoc],
    candidates: dict[str, Candidate],
    knowledge: Knowledge,
    now: int,
    config: DiscoveryConfig,
) -> None:
    counts: Counter[str] = Counter()
    doc_freq: Counter[str] = Counter()
    left_neighbors: dict[str, Counter] = defaultdict(Counter)
    right_neighbors: dict[str, Counter] = defaultdict(Counter)
    unigram: Counter[str] = Counter()
    by_id = {item.doc.id: item for item in prepared}
    doc_ids: dict[str, set[str]] = defaultdict(set)
    df_video: dict[str, set[str]] = defaultdict(set)
    df_zone: dict[str, set[str]] = defaultdict(set)
    first_seen: dict[str, int] = {}
    last_seen: dict[str, int] = {}

    for item in prepared:
        doc = item.doc
        seen: set[str] = set()
        for token in item.tokens:
            if len(token) > 60:
                continue
            for char in token:
                unigram[char] += 1
            length = len(token)
            for start in range(length):
                limit = min(config.max_ngram, length - start)
                for size in range(config.min_ngram, limit + 1):
                    gram = token[start : start + size]
                    if gram not in seen:
                        seen.add(gram)
                        counts[gram] += 1
                        doc_freq[gram] += 1
                        doc_ids[gram].add(doc.id)
                        if doc.bvid:
                            df_video[gram].add(doc.bvid)
                        if doc.zone:
                            df_zone[gram].add(doc.zone)
                        if doc.ctime and gram not in first_seen:
                            first_seen[gram] = doc.ctime
                        if doc.ctime:
                            last_seen[gram] = max(last_seen.get(gram, 0), doc.ctime)
                    if start > 0:
                        left_neighbors[gram][token[start - 1]] += 1
                    if start + size < length:
                        right_neighbors[gram][token[start + size]] += 1

    total_chars = sum(unigram.values())
    if total_chars <= 0:
        return
    for gram, count in counts.items():
        if count < config.min_count or doc_freq.get(gram, 0) < config.min_df:
            continue
        if not (config.min_ngram <= len(gram) <= 12):
            continue
        if is_numeric_like(gram) or is_short_ascii(gram, 2):
            continue
        if gram in GENERIC_WORDS or knowledge.is_generic(gram):
            continue
        if has_edge_char(gram) or looks_like_sentence(gram):
            continue
        cohesion = float("inf")
        valid = True
        for split in range(1, len(gram)):
            left, right = gram[:split], gram[split:]
            left_count = counts.get(left) or unigram.get(left, 0)
            right_count = counts.get(right) or unigram.get(right, 0)
            if left_count <= 0 or right_count <= 0:
                valid = False
                break
            pmi = math.log2((count * total_chars) / (left_count * right_count))
            cohesion = min(cohesion, pmi)
        if not valid or cohesion < config.min_cohesion:
            continue
        left_entropy = entropy(left_neighbors[gram])
        right_entropy = entropy(right_neighbors[gram])
        if min(left_entropy, right_entropy) < config.min_entropy:
            continue
        candidate = candidates.get(gram)
        if candidate is None:
            candidate = _new_candidate(gram, source="n-gram", confidence=0.28)
            candidates[gram] = candidate
        candidate.count = max(candidate.count, count)
        candidate.df_video = max(candidate.df_video, len(df_video[gram]))
        candidate.df_zone = max(candidate.df_zone, len(df_zone[gram]))
        candidate.cohesion = max(candidate.cohesion, round(cohesion, 2))
        candidate.entropy = max(candidate.entropy, round(min(left_entropy, right_entropy), 2))
        candidate.first_seen = min(
            [value for value in (candidate.first_seen, first_seen.get(gram, 0)) if value] or [0]
        )
        candidate.last_seen = max(candidate.last_seen, last_seen.get(gram, 0))
        candidate.sources = sorted(set(candidate.sources + ["n-gram"]))
        for doc_id in list(doc_ids[gram])[: config.evidence_limit * 2]:
            prepared_doc = by_id.get(doc_id)
            if prepared_doc is None:
                continue
            _add_hit(
                candidate,
                prepared_doc.doc,
                surface=gram,
                now=now,
                evidence_limit=config.evidence_limit,
            )


def _scan_seeds(
    prepared: list[PreparedDoc],
    candidates: dict[str, Candidate],
    knowledge: Knowledge,
    now: int,
    config: DiscoveryConfig,
) -> None:
    # 只保留在语料中出现过的种子；避免把 148 条种子全部塞进榜。
    for key, entry in knowledge.seed_surfaces():
        if len(key) < 2:
            continue
        hits = 0
        candidate = candidates.get(key)
        for item in prepared:
            if key not in item.norm_text:
                continue
            if candidate is None:
                candidate = _new_candidate(
                    entry.canonical,
                    source="seed",
                    canonical=entry.canonical,
                    confidence=0.9,
                    known=True,
                )
                candidates[key] = candidate
            candidate.known = True
            candidate.canonical = entry.canonical
            candidate.type = entry.type
            candidate.origin_quote = entry.origin or candidate.origin_quote
            candidate.sources = sorted(set(candidate.sources + ["seed"]))
            _add_hit(
                candidate,
                item.doc,
                surface=entry.canonical,
                now=now,
                evidence_limit=config.evidence_limit,
            )
            hits += 1
            if hits >= config.seed_scan_limit:
                break


def _attach_meta(
    candidates: dict[str, Candidate],
    mentions: list[MetaMention],
    knowledge: Knowledge,
) -> None:
    for mention in mentions:
        norm = mention.norm
        candidate = candidates.get(norm)
        if candidate is None:
            candidate = _new_candidate(
                mention.surface,
                source="meta",
                canonical=mention.canonical,
                confidence=mention.confidence,
                known=False,
            )
            candidates[norm] = candidate
        candidate.meta.append(mention)
        candidate.sources = sorted(set(candidate.sources + [f"meta:{mention.kind}"]))
        if mention.kind == "dedicated":
            candidate.confidence = max(candidate.confidence, 0.75)
        else:
            candidate.confidence = max(candidate.confidence, 0.5)
        entry = knowledge.lookup(mention.surface)
        if entry:
            candidate.known = True
            candidate.canonical = entry.canonical
            candidate.type = entry.type


def _candidate_filter_reason(candidate: Candidate, knowledge: Knowledge) -> str:
    """候选入池前的统一过滤；返回空串表示保留。"""
    if len(candidate.norm) < 2 and not (
        candidate.known or any(item.kind == "dedicated" for item in candidate.meta)
    ):
        return "short"
    if len(candidate.norm) > 24:
        return "too_long"
    if is_numeric_like(candidate.norm):
        return "numeric"
    if candidate.norm in GENERIC_WORDS:
        return "generic_word"
    if not candidate.known and knowledge.is_generic(candidate.norm):
        return "lexicon"
    reason = knowledge.blocked_reason(candidate.surface)
    if reason and not candidate.known:
        return reason
    if not candidate.usage and not candidate.meta:
        return "no_evidence"
    return ""


def _count_doc_mentions(prepared: list[PreparedDoc], norm: str) -> int:
    if not norm:
        return 0
    count = 0
    for item in prepared:
        if norm.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(norm) + r"(?![a-z0-9])", item.norm_text):
                count += 1
        elif norm in item.norm_text:
            count += 1
    return count


def extract_candidates(
    docs: list[Doc],
    knowledge: Knowledge,
    *,
    now: int,
    meta_mentions: list[MetaMention] | None = None,
    config: DiscoveryConfig | None = None,
    llm_candidates: list[dict] | None = None,
    audit: dict | None = None,
) -> list[Candidate]:
    config = config or DiscoveryConfig()
    prepared = prepare_docs(docs)
    candidates: dict[str, Candidate] = {}

    _scan_patterns(prepared, candidates, knowledge, now, config)
    _scan_ngrams(prepared, candidates, knowledge, now, config)
    _scan_seeds(prepared, candidates, knowledge, now, config)
    if meta_mentions:
        _attach_meta(candidates, meta_mentions, knowledge)
    if llm_candidates:
        _attach_llm(candidates, llm_candidates, prepared, knowledge, now, config)
    _scan_surfaces(prepared, candidates, now, config)

    # 合并同 norm 的重复候选，再做证据/统计收尾。
    merged: dict[str, Candidate] = {}
    for norm, candidate in candidates.items():
        existing = merged.get(norm)
        if existing is None:
            merged[norm] = candidate
        else:
            _merge_duplicate(existing, candidate)
    audit_names = list((audit or {}).get("names") or [])
    named_audit: dict[str, dict] = {}
    for name in audit_names:
        norm = normalize_surface(name)
        named_audit[name] = {
            "norm": norm,
            "doc_mentions": _count_doc_mentions(prepared, norm),
            "candidate_before_filter": norm in merged,
            "proper_like": knowledge.is_proper_like(name),
            "kept": False,
            "reason": "",
        }

    result: list[Candidate] = []
    blocked_counts: Counter[str] = Counter()
    for candidate in merged.values():
        reason = _candidate_filter_reason(candidate, knowledge)
        if not reason and (
            candidate.norm.isascii()
            and len(candidate.norm) <= 3
            and not (candidate.known or candidate.meta or candidate.pattern)
        ):
            reason = "short_ascii"
        if reason:
            blocked_counts[reason] += 1
            for name, item in named_audit.items():
                if item["norm"] == candidate.norm:
                    item["reason"] = reason
            continue
        _finalize_candidate(candidate, now, knowledge)
        result.append(candidate)
        for name, item in named_audit.items():
            if item["norm"] == candidate.norm:
                item["kept"] = True
                item["reason"] = ""
    result.sort(
        key=lambda item: (
            not item.known,
            not bool(item.meta),
            -item.confidence,
            -item.count,
            item.norm,
        )
    )
    if audit is not None:
        for name, item in named_audit.items():
            if item["kept"] or item["reason"]:
                continue
            if not item["doc_mentions"] and not item["candidate_before_filter"]:
                item["reason"] = "not_present"
            else:
                item["reason"] = knowledge.blocked_reason(name) or "not_extracted"
        audit["blocked_by_reason"] = dict(blocked_counts)
        audit["raw_candidates"] = len(merged)
        audit["kept_candidates"] = len(result)
        audit["named"] = named_audit
    return result[: config.max_candidates]


def _scan_surfaces(
    prepared: list[PreparedDoc],
    candidates: dict[str, Candidate],
    now: int,
    config: DiscoveryConfig,
) -> None:
    """对每个候选（尤其元话语/种子候选）回扫语料，补全使用计数与证据。

    n-gram/pattern 的成员关系只覆盖被抽中的片段；元话语候选（标题给出、评论里
    也可能被使用）必须单独回扫，否则热度会漏掉最重要的一路「真实使用」。
    """
    for candidate in candidates.values():
        surfaces = [candidate.surface]
        if candidate.canonical and candidate.canonical != candidate.surface:
            surfaces.append(candidate.canonical)
        seen_surface: list[str] = []
        for surface in surfaces:
            norm = normalize_surface(surface)
            if norm and norm not in {normalize_surface(item) for item in seen_surface}:
                seen_surface.append(surface)
        if not seen_surface:
            continue
        for item in prepared:
            for surface in seen_surface:
                norm_surface = normalize_surface(surface)
                if norm_surface.isascii():
                    # ASCII 候选必须词边界命中，避免 shi 命中 bushi / english。
                    if not re.search(
                        r"(?<![a-z0-9])" + re.escape(norm_surface) + r"(?![a-z0-9])",
                        item.norm_text,
                    ):
                        continue
                elif norm_surface not in item.norm_text:
                    continue
                _add_hit(
                    candidate,
                    item.doc,
                    surface=surface,
                    now=now,
                    evidence_limit=config.evidence_limit,
                )
                break


def _attach_llm(
    candidates: dict[str, Candidate],
    llm_candidates: list[dict],
    prepared: list[PreparedDoc],
    knowledge: Knowledge,
    now: int,
    config: DiscoveryConfig,
) -> None:
    """接入交接单的 LLM 抽取结果；硬校验 surface 必须是证据原文子串。"""
    by_id = {item.doc.id: item for item in prepared}
    for item in llm_candidates:
        surface = str(item.get("surface") or "").strip()
        norm = normalize_surface(surface)
        if not (2 <= len(norm) <= 20):
            continue
        evidence_ids = item.get("evidence_ids") or []
        hits = 0
        candidate = candidates.get(norm)
        for doc_id in evidence_ids:
            prepared_doc = by_id.get(str(doc_id))
            if prepared_doc is None:
                continue
            quote = find_quote(prepared_doc.doc.text, surface)
            if not quote:
                continue
            if candidate is None:
                candidate = _new_candidate(
                    surface,
                    source="llm_extract",
                    canonical=str(item.get("canonical") or surface),
                    confidence=float(item.get("confidence") or 0.5),
                    known=bool(item.get("known")),
                )
                candidates[norm] = candidate
            _add_hit(
                candidate,
                prepared_doc.doc,
                surface=surface,
                now=now,
                evidence_limit=config.evidence_limit,
            )
            hits += 1
        if candidate is not None and hits:
            candidate.meaning = str(item.get("meaning") or candidate.meaning)[:200]
            candidate.origin_quote = str(item.get("origin_quote") or candidate.origin_quote)[:200]
            candidate.type = str(item.get("type") or candidate.type)
            candidate.sources = sorted(set(candidate.sources + ["llm_extract"]))


# ---------------------------------------------------------------- 梗族归并
def _infer_type(surface: str) -> str:
    for suffix, kind in SUFFIX_TYPES.items():
        if surface.endswith(suffix):
            return kind
    if "↑" in surface or "↓" in surface or "←" in surface or "→" in surface:
        return "符号"
    if surface.isascii():
        return "缩写"
    if len(surface) >= 6:
        return "句式"
    return "词"


def _meme_probability(meme: Meme) -> float:
    score = 0.0
    if meme.known:
        score = max(score, 0.88)
    dedicated = sum(1 for item in meme.meta if item.kind == "dedicated")
    if dedicated:
        score = max(score, 0.74 + min(0.12, 0.04 * (dedicated - 1)))
    elif meme.meta:
        score = max(score, 0.52)
    if any(candidate.pattern for candidate in meme.candidates):
        score += 0.08
    if any(candidate.quoted for candidate in meme.candidates):
        score += 0.04
    adoption = [item for item in meme.usage if item.kind in ("comment", "danmaku")]
    videos = len({item.bvid for item in adoption if item.bvid})
    zones = len({item.zone for item in adoption if item.zone})
    usage_docs = len({item.doc_id for item in adoption})
    if videos >= 2:
        score += 0.06
    if videos >= 5:
        score += 0.04
    if zones >= 2:
        score += 0.03
    if any(candidate.cohesion >= 3.0 and candidate.df_video >= 2 for candidate in meme.candidates):
        score += 0.03
    # 使用线独立入池：还没有「梗百科」解释、但已在多视频/多分区被真实使用的
    # 新表达，不能因为少了元话语就被一刀切掉。
    if videos >= 3 and zones >= 2 and usage_docs >= 8:
        score = max(score, 0.56)
    if videos >= 5 and zones >= 3 and usage_docs >= 15:
        score = max(score, 0.66)
    return round(clamp(score, 0, 1), 3)


def _meme_confidence(meme: Meme) -> float:
    usage = len({item.doc_id for item in meme.usage})
    videos = len({item.bvid for item in meme.usage if item.bvid})
    meta = len({item.bvid for item in meme.meta if item.bvid})
    volume = min(1.0, math.log1p(usage) / math.log1p(20))
    breadth = min(1.0, (videos + 1.5 * meta) / 8.0)
    return round(clamp(0.2 + 0.5 * volume + 0.3 * breadth, 0, 1), 3)


def merge_families(
    candidates: list[Candidate],
    knowledge: Knowledge,
    *,
    now: int,
    meta_mentions: list[MetaMention] | None = None,
) -> list[Meme]:
    """把同源变体合并成梗族。别名/种子优先，其次字符相似度。"""
    if not candidates:
        return []
    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    # 1) 种子/别名强制归并
    forced: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        entry = knowledge.lookup(candidate.surface) or (
            knowledge.lookup(candidate.canonical) if candidate.canonical else None
        )
        if entry:
            key = entry.key
            candidate.known = True
            candidate.canonical = entry.canonical
            candidate.type = entry.type
            if key in forced:
                union(index, forced[key])
            else:
                forced[key] = index

    # 2) 相似度自动归并：按字符倒排做 blocking（只比较共享字符的候选），
    #    避免「首字符不同但包含关系」漏并（如 不烧心 / 吃了不烧心）。
    buckets: dict[str, list[int]] = defaultdict(list)
    for index, candidate in enumerate(candidates):
        for char in dict.fromkeys(candidate.norm):
            buckets[char].append(index)
    forced_indices = set(forced.values())
    for index, candidate in enumerate(candidates):
        if index in forced_indices:
            continue
        peers: set[int] = set()
        for char in dict.fromkeys(candidate.norm):
            peers.update(buckets.get(char, ()))
        for other_index in sorted(peers):
            if other_index >= index:
                continue
            other = candidates[other_index]
            if similar_surface(candidate.norm, other.norm):
                union(index, other_index)
                break

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(candidates)):
        groups[find(index)].append(index)

    memes: list[Meme] = []
    for indices in groups.values():
        group = [candidates[index] for index in indices]
        known_entries = [knowledge.lookup(item.surface) for item in group]
        known_entry = next((entry for entry in known_entries if entry), None)
        meta_all = [item for candidate in group for item in candidate.meta]
        dedicated = [item for item in meta_all if item.kind == "dedicated"]
        if known_entry:
            canonical = known_entry.canonical
            meme_type = known_entry.type
            known = True
            seed_origin = known_entry.origin
            seed_year = known_entry.origin_year
        else:
            best_meta = max(meta_all, key=lambda item: (item.confidence, item.view), default=None)
            if best_meta is not None:
                canonical = best_meta.surface
            else:
                best_candidate = max(group, key=lambda item: (item.count, item.confidence, -len(item.surface)))
                canonical = best_candidate.canonical or best_candidate.surface
            meme_type = _infer_type(canonical)
            known = False
            seed_origin = ""
            seed_year = None
        norm = normalize_surface(canonical)
        if not norm:
            continue
        usage_by_doc: dict[str, Usage] = {}
        evidence_by_id: dict[str, Evidence] = {}
        meta_by_key: dict[tuple[str, str], MetaMention] = {}
        members: list[str] = []
        sources: set[str] = set()
        for candidate in group:
            for item in candidate.usage:
                usage_by_doc.setdefault(item.doc_id, item)
            for item in candidate.evidence:
                evidence_by_id.setdefault(item.comment_id, item)
            for item in candidate.meta:
                meta_by_key.setdefault((item.bvid, item.norm), item)
            for member in [candidate.surface, candidate.canonical]:
                if member and normalize_surface(member) not in {normalize_surface(x) for x in members}:
                    members.append(member)
            sources.update(candidate.sources)
        if meta_mentions:
            for mention in meta_mentions:
                if normalize_surface(mention.norm) == norm:
                    meta_by_key.setdefault((mention.bvid, mention.norm), mention)
        usage = list(usage_by_doc.values())
        evidence = sorted(
            evidence_by_id.values(),
            key=lambda item: (-item.like, -item.ctime, item.comment_id),
        )[:8]
        meta = sorted(
            meta_by_key.values(),
            key=lambda item: (-item.pubdate, -item.view, item.bvid, item.title),
        )
        times = [item.ctime for item in usage if item.ctime] + [item.pubdate for item in meta if item.pubdate]
        first_seen = min(times) if times else 0
        last_seen = max(times) if times else 0
        meme = Meme(
            family_key=norm,
            canonical=canonical,
            norm=norm,
            type=meme_type,
            meaning="",
            origin_quote=evidence[0].quote if evidence else "",
            origin_ref=meta[0].url if meta else "",
            members=members[:24],
            candidates=group,
            evidence=evidence,
            usage=usage,
            meta=meta,
            known=known,
            seed_origin=seed_origin,
            seed_origin_year=seed_year,
            first_seen=first_seen,
            last_seen=last_seen,
            sources=sorted(sources),
        )
        meme.meme_prob = _meme_probability(meme)
        meme.confidence = _meme_confidence(meme)
        if not meme.members:
            meme.members = [canonical]
        memes.append(meme)
    memes.sort(
        key=lambda item: (-item.meme_prob, -item.confidence, -item.last_seen, item.family_key)
    )
    return memes
