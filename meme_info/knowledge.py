"""种子词/别名/黑名单知识库（只读 data/，不写库、不依赖 app）。

知识库在模块里只做三件事：
1. 已知梗先验：命中种子/别名时提高候选置信度，并给出 canonical/type/origin；
2. 归并锚点：人工别名优先于自动相似度；
3. 负向过滤：新闻/营销/常青/敏感词不作为「新热梗」。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .text import normalize_surface

ORIGIN_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class SeedEntry:
    canonical: str
    aliases: tuple[str, ...] = ()
    type: str = "词"
    origin: str = ""
    origin_year: int | None = None
    key: str = ""

    @property
    def surfaces(self) -> tuple[str, ...]:
        values = [self.canonical, *self.aliases]
        seen: list[str] = []
        for value in values:
            if value and value not in seen:
                seen.append(value)
        return tuple(seen)


def parse_origin_year(origin: str) -> int | None:
    match = ORIGIN_YEAR_RE.search(origin or "")
    if not match:
        return None
    year = int(match.group(1))
    return year if 1990 <= year <= 2100 else None


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def _read_words(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return set()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


class Knowledge:
    def __init__(self, data_dir: str | Path | None = None):
        base = Path(data_dir or os.getenv("MEME_INFO_DATA_DIR") or DEFAULT_DATA_DIR)
        self.data_dir = base
        payload = _read_json(base / "seed_memes.json", {"memes": []})
        raw_memes = payload.get("memes", []) if isinstance(payload, dict) else payload
        self.seeds: list[SeedEntry] = []
        self._by_key: dict[str, SeedEntry] = {}
        self._alias_to: dict[str, str] = {}
        for item in raw_memes:
            canonical = str(item.get("canonical") or "").strip()
            if not canonical:
                continue
            aliases = tuple(
                str(alias).strip()
                for alias in (item.get("aliases") or [])
                if str(alias).strip()
            )
            entry = SeedEntry(
                canonical=canonical,
                aliases=aliases,
                type=str(item.get("type") or "词"),
                origin=str(item.get("origin") or ""),
                origin_year=parse_origin_year(str(item.get("origin") or "")),
                key=normalize_surface(canonical),
            )
            self.seeds.append(entry)
            for surface in entry.surfaces:
                key = normalize_surface(surface)
                if key:
                    self._by_key.setdefault(key, entry)
        alias_payload = _read_json(base / "alias_map.json", {"map": {}})
        for surface, canonical in (alias_payload.get("map") or {}).items():
            key = normalize_surface(str(surface))
            target = normalize_surface(str(canonical))
            if key and target:
                self._alias_to[key] = target
        block = _read_json(base / "blocklist.json", {})
        self.news: list[str] = [str(item) for item in block.get("news", []) if item]
        self.marketing: list[str] = [str(item) for item in block.get("marketing", []) if item]
        self.evergreen: set[str] = {
            normalize_surface(str(item)) for item in block.get("evergreen", []) if item
        }
        self.sensitive: list[str] = [str(item) for item in block.get("sensitive", []) if item]
        self.common_words: set[str] = {
            normalize_surface(word) for word in _read_words(base / "common_words.txt")
        }
        self._lexicon: set[str] | None = None
        self._lexicon_path = base / "lexicon.txt"

    # ---------------------------------------------------------------- 词表
    @property
    def lexicon(self) -> set[str]:
        if self._lexicon is None:
            self._lexicon = {
                normalize_surface(word) for word in _read_words(self._lexicon_path)
            }
        return self._lexicon

    @property
    def seed_keys(self) -> list[str]:
        return list(self._by_key)

    def lookup(self, surface: str) -> SeedEntry | None:
        key = normalize_surface(surface)
        if not key:
            return None
        entry = self._by_key.get(key)
        if entry:
            return entry
        target = self._alias_to.get(key)
        if target:
            return self._by_key.get(target)
        return None

    def canonical(self, surface: str) -> str:
        key = normalize_surface(surface)
        entry = self.lookup(surface)
        if entry:
            return entry.canonical
        target = self._alias_to.get(key)
        return target or ""

    def is_generic(self, surface: str) -> bool:
        key = normalize_surface(surface)
        if not key:
            return True
        if key in self.common_words:
            return True
        if key in self.lexicon and not self.lookup(surface):
            return True
        return False

    def is_evergreen(self, surface: str) -> bool:
        return normalize_surface(surface) in self.evergreen

    def is_sensitive(self, surface: str) -> bool:
        key = normalize_surface(surface)
        return any(word and normalize_surface(word) in key for word in self.sensitive)

    def blocked_reason(self, surface: str) -> str:
        """返回负向过滤原因；空串表示可继续。"""
        key = normalize_surface(surface)
        if not key:
            return "empty"
        if self.is_sensitive(surface):
            return "sensitive"
        if any(word and normalize_surface(word) in key for word in self.news):
            return "news_like"
        if any(word and normalize_surface(word) in key for word in self.marketing):
            return "marketing"
        if key in self.common_words:
            return "common_word"
        return ""

    def prior(self, surface: str) -> tuple[float, bool]:
        """已知梗先验（0-1）与 known 标记。"""
        entry = self.lookup(surface)
        if entry:
            return 0.9, True
        return 0.0, False

    def seed_surfaces(self) -> Iterable[tuple[str, SeedEntry]]:
        for key, entry in self._by_key.items():
            yield key, entry
