"""文本归一化与切分：B站 语料清洗的第一步。"""
from __future__ import annotations

import re
import unicodedata

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\uFE0F\u20E3]"
)
REPEAT_RE = re.compile(r"(.)\1{2,}", re.S)
SEGMENT_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def normalize_text(text: str) -> str:
    """全半角统一、去 emoji/零宽、合并重复字符、小写英文。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(ZERO_WIDTH)
    text = EMOJI_RE.sub(" ", text)
    text = text.lower()

    def collapse(match: re.Match) -> str:
        char = match.group(1)
        keep = 3 if char.isdigit() else 2
        return char * keep

    text = REPEAT_RE.sub(collapse, text)
    return text


def segments(text: str) -> list[str]:
    """按非中英文数字切段，避免 n-gram 跨越标点与句子边界。"""
    return SEGMENT_RE.findall(normalize_text(text))


def normalize_surface(surface: str) -> str:
    """候选/别名的归一化键（与语料归一化保持一致）。"""
    return "".join(segments(surface))


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


def is_numeric_like(text: str) -> bool:
    return bool(text) and all(ch.isdigit() or ch in ".,%+-" for ch in text)


def clip(text: str, limit: int = 60) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def load_common_words(path) -> set[str]:
    words: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                word = normalize_surface(line.strip())
                if word:
                    words.add(word)
    except OSError:
        pass
    return words


def load_lexicon(path) -> dict[str, int]:
    """加载常用词表（word freq），用于剔除普通词与边界错切。"""
    words: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                try:
                    words[parts[0]] = int(parts[1])
                except ValueError:
                    continue
    except OSError:
        pass
    return words
