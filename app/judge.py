"""梗性判定：纯规则打分，输出 0-1 与可解释的分项原因。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from .normalize import is_numeric_like, load_common_words, load_lexicon, normalize_surface

SUFFIXES = ("文学", "圣体", "判官", "圣经", "定律", "行为", "发言", "宇宙", "含量", "刺客")
EDGE_CHARS = set("的了是在我你他她它这那就都也还又很太真不没有会能要把被给让从到和与或而但才只个些什么怎样如此对说看着过很再请别更最")
PROPER_SUFFIXES = ("人", "国", "省", "市", "县", "区", "公司", "大学", "医院", "政府", "局", "部", "集团", "平台")
DATE_RE = re.compile(r"(20\d{2}|19\d{2})年?|\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


class Judger:
    def __init__(self, data_dir: Path | None = None):
        base = Path(data_dir or config.DATA_DIR)
        self.common_words = load_common_words(base / "common_words.txt")
        self.lexicon = load_lexicon(base / "lexicon.txt")
        seed_payload = _read_json(base / "seed_memes.json", {"memes": []})
        alias_payload = _read_json(base / "alias_map.json", {"map": {}})
        block = _read_json(base / "blocklist.json", {})

        self.seed_map: dict[str, dict] = {}
        for entry in seed_payload.get("memes", []):
            canonical = entry.get("canonical", "")
            if not canonical:
                continue
            payload = {
                "canonical": canonical,
                "type": entry.get("type", "词"),
                "origin": entry.get("origin", ""),
            }
            for surface in [canonical, *entry.get("aliases", [])]:
                key = normalize_surface(surface)
                if key:
                    self.seed_map.setdefault(key, payload)

        self.alias_map = {normalize_surface(k): v for k, v in alias_payload.get("map", {}).items()}
        self.news_words = [w for w in block.get("news", []) if w]
        self.marketing_words = [w for w in block.get("marketing", []) if w]
        self.evergreen_words = set(block.get("evergreen", []))
        self.sensitive_words = [w for w in block.get("sensitive", []) if w]

    def seed_entry(self, family: dict) -> dict | None:
        candidates = [family.get("canonical", ""), *family.get("members", [])]
        for surface in candidates:
            key = normalize_surface(surface)
            if key in self.seed_map:
                return self.seed_map[key]
        return None

    def judge(self, family: dict, context: dict | None = None) -> tuple[float, dict]:
        context = context or {}
        canonical = family.get("canonical", "")
        norm = normalize_surface(canonical)
        reasons: dict[str, float] = {}

        if not norm:
            return 0.0, {"empty": -1.0}

        for word in self.sensitive_words:
            if word and word in norm:
                return 0.0, {"sensitive": -1.0}

        score = 0.0
        seed = self.seed_entry(family) or self.seed_map.get(norm)
        if seed:
            score += 0.35
            reasons["seed_hit"] = 0.35
        elif norm in self.alias_map:
            score += 0.20
            reasons["alias_hit"] = 0.20

        if family.get("quoted", 0) > 0:
            score += 0.10
            reasons["quoted"] = 0.10

        if any(norm.endswith(suffix) for suffix in SUFFIXES):
            score += 0.15
            reasons["suffix"] = 0.15

        df_video = family.get("df_video", 0)
        df_zone = family.get("df_zone", 0)
        if df_video >= 3 and df_zone >= 2:
            score += 0.15
            reasons["breadth"] = 0.15
        elif df_video >= 2 and df_zone >= 1:
            # 稳定榜口径（docs/02 §4）：跨 2 视频 + 跨分区即构成扩散证据
            score += 0.10
            reasons["breadth_mid"] = 0.10
        elif df_video >= 5:
            score += 0.08
            reasons["breadth_small"] = 0.08

        if family.get("is_line") and family.get("danmaku_df", 0) >= 3:
            score += 0.10
            reasons["danmaku_repeat"] = 0.10

        if (family.get("cohesion", 0) >= 5.0 and family.get("entropy", 0) >= 1.5
                and len(norm) >= 3 and not seed):
            score += 0.10
            reasons["cohesion"] = 0.10

        if family.get("df_search", 0) >= 2:
            score += 0.12
            reasons["meme_source"] = 0.12

        lower = norm
        if any(word in lower for word in self.news_words) or DATE_RE.search(lower):
            score -= 0.40
            reasons["news_like"] = -0.40
        if any(word in lower for word in self.marketing_words):
            score -= 0.30
            reasons["marketing"] = -0.30
        if norm in self.common_words:
            score -= 0.30
            reasons["common_word"] = -0.30
        # 种子是已知梗先验：豁免 lexicon/proper_noun（已知梗很可能同时是
        # 通用词/带专名后缀），只保留 evergreen 的新鲜度降权。
        if norm in self.lexicon and not seed:
            score -= 0.25
            reasons["lexicon"] = -0.25
        if not seed and len(norm) >= 2 and (norm[0] in EDGE_CHARS or norm[-1] in EDGE_CHARS):
            score -= 0.15
            reasons["sentence_fragment"] = -0.15
        if not seed and len(norm) >= 3 and norm.endswith(PROPER_SUFFIXES):
            score -= 0.20
            reasons["proper_noun"] = -0.20
        owner_names = context.get("owner_names") or set()
        if norm in owner_names:
            score -= 0.30
            reasons["owner_name"] = -0.30
        if norm in self.evergreen_words:
            score -= 0.05
            reasons["evergreen"] = -0.05
        if len(norm) < 2 or len(norm) > 12:
            score -= 0.15
            reasons["length"] = -0.15
        if is_numeric_like(norm) and not (seed or norm in self.alias_map):
            score -= 0.30
            reasons["numeric"] = -0.30
        if norm.isascii() and len(norm) <= 3 and not (seed or norm in self.alias_map):
            score -= 0.20
            reasons["short_ascii"] = -0.20

        score = round(max(0.0, min(1.0, score)), 3)
        return score, reasons


def passes_meme_gate(meme_score: float, seed_hit: bool = False) -> bool:
    """0-1 梗性分是否达入池线（docs/01 §6：≥ MEME_MIN 入候选池）。

    种子命中是文档允许的先验豁免。注意本函数只接受 0-1 分，
    不要与 0-100 的 MIN_SCORE / OBSERVE_MIN 混用（历史量纲 bug）。
    """
    return bool(seed_hit) or float(meme_score) >= config.MEME_MIN
