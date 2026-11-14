"""候选梗发现：n-gram 新词发现（凝固度 + 邻接熵）+ 模式抽取 + 梗族归并。

不依赖任何第三方 NLP 库，纯 Python 实现。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import config
from .normalize import (
    clip,
    is_numeric_like,
    load_common_words,
    load_lexicon,
    normalize_surface,
    normalize_text,
    segments,
)

QUOTE_RE = re.compile(r"[「『“\"《【]([^」』”\"》】\n]{2,20})[」』”\"》】]")
MEME_CONTEXT_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,10})(?:是什么梗|的梗|梗出处|什么梗|这梗|这个梗|梗吗|梗啊|梗王|梗图)"
)
HASHTAG_RE = re.compile(r"#([^#\n]{2,20})#")
SUFFIX_TYPES = {
    "文学": "句式",
    "圣体": "句式",
    "判官": "句式",
    "圣经": "句式",
    "定律": "句式",
    "行为": "句式",
    "发言": "句式",
    "宇宙": "句式",
    "含量": "句式",
    "刺客": "词",
}


@dataclass
class TextDoc:
    text: str
    bvid: str = ""
    zone: str = ""
    kind: str = "comment"
    likes: int = 0
    owner: str = ""
    norm_text: str = ""
    segs: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    surface: str
    norm: str
    count: int
    df_video: int
    df_zone: int
    df_up: int = 0
    kinds: set[str] = field(default_factory=set)
    zones: set[str] = field(default_factory=set)
    cohesion: float = 0.0
    entropy: float = 0.0
    quoted: int = 0
    danmaku_df: int = 0
    pattern: bool = False
    is_line: bool = False
    seed_key: str = ""

    def weight(self) -> float:
        return self.count * math.log1p(self.df_video) * (1.0 + min(self.quoted, 5) * 0.2)


def prepare(docs: list[TextDoc]) -> list[TextDoc]:
    total = 0
    kept: list[TextDoc] = []
    for doc in docs:
        doc.norm_text = normalize_text(doc.text)
        doc.segs = segments(doc.text)
        length = sum(len(s) for s in doc.segs)
        if total + length > config.MAX_CORPUS_CHARS:
            continue
        total += length
        kept.append(doc)
    return kept


def _entropy(counter: Counter) -> float:
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    value = 0.0
    for count in counter.values():
        probability = count / total
        value -= probability * math.log2(probability)
    return value


def _extract_patterns(docs: list[TextDoc]) -> tuple[Counter, Counter, Counter]:
    counts: Counter = Counter()
    df: Counter = Counter()
    quoted: Counter = Counter()
    for doc in docs:
        seen: set[str] = set()
        text = doc.norm_text or " ".join(doc.segs)
        for match in QUOTE_RE.finditer(text):
            surface = normalize_surface(match.group(1))
            if 2 <= len(surface) <= 20:
                counts[surface] += 1
                quoted[surface] += 1
                seen.add(surface)
        for match in MEME_CONTEXT_RE.finditer(text):
            surface = normalize_surface(match.group(1))
            if 2 <= len(surface) <= 20:
                counts[surface] += 1
                seen.add(surface)
        for match in HASHTAG_RE.finditer(text):
            surface = normalize_surface(match.group(1))
            if 2 <= len(surface) <= 20:
                counts[surface] += 1
                seen.add(surface)
        for surface in seen:
            df[surface] += 1
    return counts, df, quoted


def _extract_danmaku_lines(docs: list[TextDoc]) -> tuple[Counter, Counter]:
    counts: Counter = Counter()
    df: Counter = Counter()
    for doc in docs:
        if doc.kind != "danmaku":
            continue
        seen: set[str] = set()
        for seg in doc.segs:
            if 3 <= len(seg) <= 20:
                counts[seg] += 1
                seen.add(seg)
        for surface in seen:
            df[surface] += 1
    return counts, df


def discover(docs: list[TextDoc], data_dir=None) -> list[Candidate]:
    """从语料中抽取候选梗面。"""
    docs = prepare(docs)
    if not docs:
        return []

    base_dir = data_dir or config.DATA_DIR
    common_words = load_common_words(base_dir / "common_words.txt")
    lexicon = load_lexicon(base_dir / "lexicon.txt")
    protected: set[str] = set()
    try:
        seed_payload = json.loads((base_dir / "seed_memes.json").read_text(encoding="utf-8"))
        for entry in seed_payload.get("memes", []):
            for surface in [entry.get("canonical", ""), *entry.get("aliases", [])]:
                key = normalize_surface(surface)
                if key:
                    protected.add(key)
    except (OSError, json.JSONDecodeError):
        pass

    def generic(surface: str) -> bool:
        if surface in common_words:
            return True
        return surface in lexicon and surface not in protected

    # ---- 1. n-gram 词频（含单字） ----
    uni: Counter = Counter()
    counts: Counter = Counter()
    for doc in docs:
        for seg in doc.segs:
            length = len(seg)
            if length > 60:
                continue
            for i, char in enumerate(seg):
                uni[char] += 1
                limit = min(config.MAX_NGRAM, length - i)
                for n in range(2, limit + 1):
                    counts[seg[i : i + n]] += 1

    total_chars = sum(uni.values())
    if total_chars <= 0:
        return []

    # ---- 2. 凝固度（PMI）过滤 ----
    survivors: dict[str, int] = {}
    cohesion_map: dict[str, float] = {}
    for gram, count in counts.items():
        if count < config.MIN_COUNT:
            continue
        best = float("inf")
        for split in range(1, len(gram)):
            left, right = gram[:split], gram[split:]
            left_count = counts.get(left) or (uni.get(left, 0) if len(left) == 1 else 0)
            right_count = counts.get(right) or (uni.get(right, 0) if len(right) == 1 else 0)
            if left_count <= 0 or right_count <= 0:
                best = -1.0
                break
            pmi = math.log2((count * total_chars) / (left_count * right_count))
            best = min(best, pmi)
        if best >= config.MIN_COHESION:
            survivors[gram] = count
            cohesion_map[gram] = best

    if not survivors:
        return []

    # ---- 3. 左右邻接熵 + 文档频率 ----
    left_neighbors: dict[str, Counter] = defaultdict(Counter)
    right_neighbors: dict[str, Counter] = defaultdict(Counter)
    df_video: Counter = Counter()
    df_zone: dict[str, set] = defaultdict(set)
    df_up: dict[str, set] = defaultdict(set)
    danmaku_df: Counter = Counter()

    for doc in docs:
        seen: set[str] = set()
        for seg in doc.segs:
            length = len(seg)
            if length > 60:
                continue
            for i in range(length):
                limit = min(config.MAX_NGRAM, length - i)
                for n in range(2, limit + 1):
                    gram = seg[i : i + n]
                    if gram not in survivors:
                        continue
                    if i > 0:
                        left_neighbors[gram][seg[i - 1]] += 1
                    if i + n < length:
                        right_neighbors[gram][seg[i + n]] += 1
                    seen.add(gram)
        for gram in seen:
            df_video[gram] += 1
            df_zone[gram].add(doc.zone or "其他")
            if doc.owner:
                df_up[gram].add(doc.owner)
            if doc.kind == "danmaku":
                danmaku_df[gram] += 1

    # ---- 4. 模式与弹幕整句 ----
    pattern_counts, pattern_df, quoted = _extract_patterns(docs)
    line_counts, line_df = _extract_danmaku_lines(docs)

    merged: dict[str, Candidate] = {}

    def merge(surface: str, count: int, df: int, pattern: bool = False,
              quoted_hits: int = 0) -> None:
        if not surface or len(surface) > 20:
            return
        if is_numeric_like(surface):
            return
        existing = merged.get(surface)
        if existing:
            existing.count += count
            existing.df_video = max(existing.df_video, df)
            existing.pattern = existing.pattern or pattern
            existing.quoted = max(existing.quoted, quoted_hits)
            return
        merged[surface] = Candidate(
            surface=surface,
            norm=surface,
            count=count,
            df_video=df,
            df_zone=len(df_zone.get(surface, ())),
            quoted=quoted_hits,
            pattern=pattern,
        )

    for gram, count in survivors.items():
        if len(gram) < config.MIN_NGRAM:
            continue
        if generic(gram):
            continue
        entropy = min(_entropy(left_neighbors[gram]), _entropy(right_neighbors[gram]))
        if entropy < config.MIN_ENTROPY:
            continue
        if df_video.get(gram, 0) < config.MIN_DF:
            continue
        candidate = Candidate(
            surface=gram,
            norm=gram,
            count=count,
            df_video=df_video[gram],
            df_zone=len(df_zone[gram]),
            df_up=len(df_up[gram]),
            kinds={"n-gram"},
            zones=set(df_zone[gram]),
            cohesion=cohesion_map.get(gram, config.MIN_COHESION),
            entropy=entropy,
            danmaku_df=danmaku_df.get(gram, 0),
        )
        merged[gram] = candidate

    for surface, count in pattern_counts.items():
        if count < 2 and pattern_df.get(surface, 0) < 1:
            continue
        if generic(surface):
            continue
        merge(surface, count, pattern_df.get(surface, 0), pattern=True,
              quoted_hits=quoted.get(surface, 0))

    for surface, count in line_counts.items():
        if generic(surface):
            continue
        if line_df.get(surface, 0) < config.MIN_DF:
            continue
        if surface not in merged:
            merged[surface] = Candidate(
                surface=surface,
                norm=surface,
                count=count,
                df_video=line_df[surface],
                df_zone=len(df_zone.get(surface, ())),
                df_up=len(df_up.get(surface, ())),
                kinds={"danmaku-line"},
                zones=set(df_zone.get(surface, ())),
                danmaku_df=line_df[surface],
                is_line=True,
            )

    ranked = sorted(merged.values(), key=lambda c: c.weight(), reverse=True)
    return ranked[: config.MAX_CANDIDATES]


# ---------------- 梗族归并 ----------------
def _jaccard(a: str, b: str) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _edit_distance_at_most_one(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    if len(a) > len(b):
        a, b = b, a
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1
    return True


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < 2 or len(b) < 2:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short in long and len(long) - len(short) <= 2:
        return True
    if len(a) <= 6 and len(b) <= 6 and _edit_distance_at_most_one(a, b):
        return True
    return len(a) >= 3 and len(b) >= 3 and _jaccard(a, b) >= 0.8


def merge_families(candidates: list[Candidate], seed_map: dict, alias_map: dict) -> list[dict]:
    """把同源变体合并成梗族；种子词典优先决定 canonical 与类型。"""
    families: list[dict] = []
    index: dict[str, list[dict]] = defaultdict(list)

    for candidate in candidates:
        norm = candidate.norm
        forced = alias_map.get(norm) or seed_map.get(norm, {}).get("canonical")
        target: dict | None = None

        if forced:
            target = next((f for f in families if f["family_key"] == normalize_surface(forced)), None)
            if target is None:
                target = {
                    "family_key": normalize_surface(forced),
                    "canonical": forced,
                    "type": seed_map.get(norm, {}).get("type", "词"),
                    "origin": seed_map.get(norm, {}).get("origin", ""),
                    "members": [],
                    "score_hint": 0.0,
                }
                families.append(target)
        else:
            for bucket in index.get(norm[:1], []):
                if _similar(norm, bucket["family_key"]):
                    target = bucket
                    break

        created = False
        if target is None:
            target = {
                "family_key": norm,
                "canonical": candidate.surface,
                "type": _infer_type(candidate.surface),
                "origin": "",
                "members": [],
                "score_hint": 0.0,
            }
            families.append(target)
            created = True

        target["members"].append(candidate)
        target["score_hint"] += candidate.weight()
        if created:
            index[norm[:1]].append(target)

    result: list[dict] = []
    for family in families:
        members: list[Candidate] = family["members"]
        if not members:
            continue
        total_count = sum(m.count for m in members)
        df_video = max(m.df_video for m in members)
        df_zone = max(m.df_zone for m in members)
        df_up = max(m.df_up for m in members)
        quoted = max(m.quoted for m in members)
        danmaku_df = max(m.danmaku_df for m in members)
        cohesion = max(m.cohesion for m in members)
        entropy = max(m.entropy for m in members)
        is_line = any(m.is_line for m in members)
        surfaces = [m.surface for m in members]
        zones: set[str] = set()
        for member in members:
            zones |= member.zones
        seed_entry = seed_map.get(normalize_surface(family["canonical"]))
        if seed_entry is None:
            for member in members:
                if member.seed_key:
                    seed_entry = seed_map.get(member.seed_key)
                    break
        result.append(
            {
                "family_key": family["family_key"],
                "canonical": family["canonical"],
                "type": (seed_entry or {}).get("type") or family["type"],
                "origin": (seed_entry or {}).get("origin", ""),
                "count": total_count,
                "df_video": df_video,
                "df_zone": df_zone,
                "df_up": df_up,
                "zones": sorted(zones),
                "quoted": quoted,
                "danmaku_df": danmaku_df,
                "cohesion": round(cohesion, 2),
                "entropy": round(entropy, 2),
                "is_line": is_line,
                "df_search": 0,
                "seed_hit": bool(seed_entry),
                "members": surfaces,
                "hint": family["score_hint"],
            }
        )
    return result


def _infer_type(surface: str) -> str:
    for suffix, kind in SUFFIX_TYPES.items():
        if surface.endswith(suffix):
            return kind
    if surface.isascii():
        return "缩写"
    if len(surface) >= 6:
        return "句式"
    return "词"


def sample_text(surface: str, docs: list[TextDoc], limit: int = 60) -> str:
    """挑一条包含该候选、点赞最高或最短的原文作为代表用法。"""
    best = ""
    best_like = -1
    for doc in docs:
        if surface and surface in doc.text:
            score = doc.likes if doc.kind == "comment" else 0
            if score > best_like or (score == best_like and len(doc.text) < len(best or "x" * 10**6)):
                best = doc.text
                best_like = score
    return clip(best, limit)
