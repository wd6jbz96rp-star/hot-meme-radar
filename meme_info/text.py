"""文本归一化、切段、字符串相似度与原文定位。

这里只做「可复现的字符串处理」，不做语义判断。所有用于匹配的归一化都不改变
原文：证据 quote 通过 ``find_quote`` 回映到原文子串，找不到就返回空，绝不编造。
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from collections import Counter
from typing import Iterable

# 只保留 CJK、ASCII 字母数字与常见梗符号；其余视为分隔符。
_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9↑↓←→↖↗↘↙]+")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_MENTION_RE = re.compile(r"@[\w\u4e00-\u9fff-]{1,24}")
_TAG_RE = re.compile(r"<[^>]{1,200}>")
_SPACE_RE = re.compile(r"\s+")
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+"
)
_PUNCT = set(
    "，。！？、；：“”‘’「」『』《》【】（）()[]{}<>…—－-~～·,.;:!?'\"`_|\\/+*=&%$#@^"
)
# 句首/句尾的虚词与代词：这些字符打头的 n-gram 多数是句子碎片，而不是梗。
EDGE_CHARS = set("的了是在我你他她它这那就都也还又很太真不没有会能要把被给让从到和与或而但才只个些什么怎样如此对说看着过很再请别更最")
GENERIC_WORDS = {
    "热梗", "网络热梗", "梗", "烂梗", "什么梗", "梗百科", "梗指南", "梗学堂",
    "合集", "盘点", "视频", "评论", "弹幕", "网友", "大家", "真的", "没有",
    "一个", "这个", "那个", "什么", "怎么", "为什么", "可以", "就是", "不是",
    "现在", "已经", "还是", "但是", "所以", "因为", "如果", "然后", "感觉",
    "觉得", "知道", "喜欢", "谢谢", "哈哈", "哈哈哈", "笑死", "无语",
    # 评论里高频的句子碎片/虚词组合：不是可复用符号。
    "以为", "这是", "那是", "这样", "那样", "那么", "这么", "应该", "可能",
    "我们", "你们", "他们", "自己", "时候", "东西", "事情", "问题", "地方",
    "看到", "听过", "说过", "做过", "来了", "去了", "好的", "行吧", "对吧",
    "是啊", "不至于", "无所谓", "没办法", "有点", "一点", "不是吧", "真的吗",
    # 平台/界面词：不是梗，且极易从评论里高频冒出。
    "b站", "bilibili", "哔哩哔哩", "评论区", "弹幕区", "up", "up主", "主播",
    "粉丝", "点赞", "投币", "收藏", "关注", "三连", "一键三连", "首页", "推荐",
    "妈妈", "爸爸", "哥哥", "姐姐", "弟弟", "妹妹", "老婆", "老公", "宝宝",
    "孩子", "父母", "家人", "朋友", "同学", "老师",
}
REPEAT_RE = re.compile(r"(.)\1{3,}")


# ---------------------------------------------------------------- 清洗
def strip_html(text: str) -> str:
    return _TAG_RE.sub("", text or "")


def clean_title(text: str) -> str:
    """标题清洗：去高亮标签、HTML 实体、零宽字符与多余空白。"""
    value = html.unescape(strip_html(text or ""))
    value = value.replace("\u200b", "").replace("\ufeff", "")
    value = value.replace("&amp;", "&")
    return _SPACE_RE.sub(" ", value).strip()


def normalize_unicode(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _keep_char(char: str) -> bool:
    if char.isalnum():
        return True
    if "\u3400" <= char <= "\u9fff":
        return True
    if char in "↑↓←→↖↗↘↙":
        return True
    return False


def normalize_text(text: str) -> str:
    """匹配用文本：NFKC、去 URL/@/emoji/HTML、小写 ASCII、标点转空格。"""
    value = normalize_unicode(clean_title(text))
    value = _URL_RE.sub(" ", value)
    value = _MENTION_RE.sub(" ", value)
    value = _EMOJI_RE.sub(" ", value)
    out: list[str] = []
    for char in value:
        if _keep_char(char):
            out.append(char.lower() if char.isascii() else char)
        else:
            out.append(" ")
    return _SPACE_RE.sub(" ", "".join(out)).strip()


def normalize_surface(text: str) -> str:
    """梗面归一化：去掉全部标点/空白，保留 CJK/字母/数字/箭头。"""
    value = normalize_unicode(clean_title(text))
    value = _EMOJI_RE.sub("", value)
    chars = [
        char.lower() if char.isascii() else char
        for char in value
        if _keep_char(char)
    ]
    result = "".join(chars)
    result = REPEAT_RE.sub(r"\1\1\1", result)
    return result.strip()


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回归一化文本与每个归一化字符对应的原文下标。

    用于把「归一化后命中」回映成原文子串（证据必须可核对）。
    不做 emoji/URL 删除，只做 NFKC、大小写、去标点/空白，避免下标丢失。
    """
    norm_chars: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text or ""):
        normalized = unicodedata.normalize("NFKC", char)
        for part in normalized:
            if not _keep_char(part):
                continue
            norm_chars.append(part.lower() if part.isascii() else part)
            index_map.append(index)
    return "".join(norm_chars), index_map


def find_quote(text: str, surface: str) -> str:
    """在原文中定位 surface 的原文子串；找不到返回空串。"""
    if not text or not surface:
        return ""
    if surface in text:
        return surface
    target = normalize_surface(surface)
    if not target:
        return ""
    normalized, index_map = normalize_with_map(text)
    position = normalized.find(target)
    if position < 0:
        return ""
    start = index_map[position]
    end = index_map[position + len(target) - 1] + 1
    return text[start:end]


def clip(text: str, limit: int = 80) -> str:
    value = _SPACE_RE.sub(" ", (text or "").strip())
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


# ---------------------------------------------------------------- 切段
def segments(text: str) -> list[str]:
    """按归一化后的 token 切段；中文连续串保持完整。"""
    return [token for token in _TOKEN_RE.findall(normalize_unicode(clean_title(text))) if token]


def char_ngrams(segment: str, min_n: int = 2, max_n: int = 6) -> Iterable[str]:
    length = len(segment)
    for size in range(min_n, min(max_n, length) + 1):
        for start in range(0, length - size + 1):
            yield segment[start : start + size]


# ---------------------------------------------------------------- 判定工具
def is_numeric_like(surface: str) -> bool:
    value = normalize_surface(surface)
    if not value:
        return True
    if value.isdigit():
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?(?:%|w|万|亿|k|千)?", value, re.I):
        return True
    return False


def is_short_ascii(surface: str, limit: int = 3) -> bool:
    value = normalize_surface(surface)
    return bool(value) and value.isascii() and len(value) <= limit


def is_generic(surface: str, extra: Iterable[str] | None = None) -> bool:
    value = normalize_surface(surface)
    if not value or value in GENERIC_WORDS:
        return True
    if extra and value in {normalize_surface(item) for item in extra}:
        return True
    return False


def has_edge_char(surface: str) -> bool:
    value = normalize_surface(surface)
    return bool(value) and (value[0] in EDGE_CHARS or value[-1] in EDGE_CHARS)


def looks_like_sentence(surface: str) -> bool:
    """句子碎片启发式：过长、含明显虚词边界、或包含多个停顿。"""
    value = normalize_surface(surface)
    if len(value) > 12:
        return True
    if len(value) >= 2 and (value[0] in EDGE_CHARS or value[-1] in EDGE_CHARS):
        return True
    return False


def jaccard(a: str, b: str) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def edit_distance_at_most_one(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for left, right in zip(a, b) if left != right) <= 1
    if len(a) > len(b):
        a, b = b, a
    left = right = 0
    skipped = False
    while left < len(a) and right < len(b):
        if a[left] == b[right]:
            left += 1
            right += 1
            continue
        if skipped:
            return False
        skipped = True
        right += 1
    return True


def similar_surface(a: str, b: str) -> bool:
    """同族启发式：完全相等、包含且差 ≤2、短词编辑距离 ≤1、Jaccard ≥0.8。"""
    a, b = normalize_surface(a), normalize_surface(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 2:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short in long and len(long) - len(short) <= 2:
        # 2 字中文泛词（野生/上头/出圈）只允许差 1 字，避免把「野生」
        # 并进「野生狗奶」这类长梗；2 字 ASCII 缩写（MJ）仍允许。
        if len(short) == 2 and not short.isascii() and len(long) - len(short) > 1:
            return False
        return True
    # 2 字词只差一个字并不构成同族（「上头」vs「上课」、「出圈」vs「出道」）。
    if min(len(a), len(b)) >= 3 and len(a) <= 6 and len(b) <= 6 and edit_distance_at_most_one(a, b):
        return True
    return len(a) >= 3 and len(b) >= 3 and jaccard(a, b) >= 0.8


def entropy(counter: Counter) -> float:
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    value = 0.0
    for count in counter.values():
        probability = count / total
        value -= probability * math.log2(probability)
    return value


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def pct(values: list[float]) -> list[float]:
    """批内百分位（并列取平均名次）；空输入返回空。"""
    size = len(values)
    if size == 0:
        return []
    if size == 1:
        return [1.0]
    order = sorted(range(size), key=lambda i: values[i])
    ranks = [0.0] * size
    index = 0
    while index < size:
        end = index
        while end + 1 < size and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2.0 / (size - 1)
        for position in range(index, end + 1):
            ranks[order[position]] = average
        index = end + 1
    return ranks
