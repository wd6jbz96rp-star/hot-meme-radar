"""B站公开接口适配器（独立实现，不依赖 app/）。

关键经验（来自项目历史实测，已在新实现中复核）：
- 排行榜/搜索需要 ``buvid3``；评论接口带 ``buvid3`` 时匿名只回 3 条，
  只带 ``buvid4`` 才回 ~20 条，因此评论请求期间临时摘掉 ``buvid3``。
- 搜索接口需要先访问一次首页拿到 ``buvid3`` + ``b_nut``，再做 wbi 签名；
  只调 spi 拿 buvid 会返回 v_voucher/空结果。
- 评论翻页的 ``cursor.next`` 是 opaque 游标，只能原样回传，0 表示结束。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import time
import urllib.parse
from collections import Counter
from pathlib import Path

from .http import HttpClient, HttpError
from .models import Comment, Danmaku, Video
from .text import clean_title, safe_int

API = "https://api.bilibili.com"
HOME = "https://www.bilibili.com/"
SEARCH_REFERER = "https://search.bilibili.com/"

ZONE_NAMES = {
    119: "鬼畜", 160: "生活", 1: "动画", 4: "游戏",
    181: "影视", 3: "音乐", 5: "娱乐", 36: "知识",
}

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_DANMAKU_RE = re.compile(r"<d p=\"([^\"]*)\">(.*?)</d>", re.S)
_SEARCH_TAG_RE = re.compile(r"<[^>]{1,120}>")
_DURATION_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


# ---------------------------------------------------------------- 纯解析
def _owner(item: dict) -> tuple[str, int]:
    owner = item.get("owner") or item.get("upic") or {}
    if not isinstance(owner, dict):
        owner = {}
    name = str(owner.get("name") or item.get("author") or "")
    mid = safe_int(owner.get("mid") or item.get("mid"), 0)
    return name, mid


def _stat(item: dict) -> dict:
    stat = item.get("stat")
    if isinstance(stat, dict):
        return stat
    # 搜索结果没有 stat 对象，统计字段平铺
    return item


def parse_video(item: dict, *, source: str = "", source_rank: int = 0,
                query: str = "", zone: str = "") -> Video:
    owner_name, owner_mid = _owner(item)
    stat = _stat(item)
    duration = item.get("duration", 0)
    if isinstance(duration, str):
        match = _DURATION_RE.match(duration.strip())
        if match:
            duration = safe_int(match.group(1) or 0) * 3600 + safe_int(match.group(2)) * 60 + safe_int(match.group(3))
        else:
            duration = 0
    tid = safe_int(item.get("tid") or item.get("typeid"), 0)
    return Video(
        bvid=str(item.get("bvid") or ""),
        aid=safe_int(item.get("aid") or item.get("id"), 0),
        cid=safe_int(item.get("cid"), 0) or None,
        title=clean_title(str(item.get("title") or "")),
        desc=clean_title(str(item.get("desc") or item.get("description") or "")),
        pubdate=safe_int(item.get("pubdate"), 0),
        duration=safe_int(duration, 0),
        tid=tid,
        tname=str(item.get("tname") or item.get("typename") or ""),
        zone=zone or str(item.get("tname") or item.get("typename") or ""),
        owner=owner_name,
        owner_mid=owner_mid,
        view=safe_int(stat.get("view") or stat.get("play"), 0),
        like=safe_int(stat.get("like"), 0),
        coin=safe_int(stat.get("coin") or stat.get("favorites"), 0),
        favorite=safe_int(stat.get("favorite") or stat.get("favorites"), 0),
        share=safe_int(stat.get("share"), 0),
        reply=safe_int(stat.get("reply") or stat.get("review"), 0),
        danmaku=safe_int(stat.get("danmaku") or stat.get("video_review"), 0),
        source=source,
        source_rank=source_rank,
        query=query,
    )


def parse_ranking(payload: dict, rid: int = 0) -> list[Video]:
    items = ((payload.get("data") or {}).get("list") or [])
    zone = ZONE_NAMES.get(rid, "")
    videos: list[Video] = []
    for index, item in enumerate(items, start=1):
        if not item.get("bvid"):
            continue
        videos.append(
            parse_video(item, source=f"ranking:{rid}", source_rank=index, zone=zone)
        )
    return videos


def parse_popular(payload: dict) -> list[Video]:
    items = ((payload.get("data") or {}).get("list") or [])
    videos: list[Video] = []
    for index, item in enumerate(items, start=1):
        if not item.get("bvid"):
            continue
        videos.append(parse_video(item, source="popular", source_rank=index))
    return videos


def parse_search(payload: dict, keyword: str = "") -> list[Video]:
    data = payload.get("data") or {}
    items = data.get("result") or []
    videos: list[Video] = []
    for index, item in enumerate(items, start=1):
        if not item.get("bvid"):
            continue
        title = _SEARCH_TAG_RE.sub("", str(item.get("title") or ""))
        item = dict(item)
        item["title"] = title
        videos.append(
            parse_video(
                item,
                source=f"search:{keyword}",
                source_rank=index,
                query=keyword,
                zone=str(item.get("typename") or ""),
            )
        )
    return videos


def parse_comment_page(payload: dict, *, bvid: str, aid: int = 0,
                       zone: str = "", video_title: str = "",
                       mode: int = 3) -> tuple[list[Comment], int, bool]:
    data = payload.get("data") or {}
    cursor = data.get("cursor") or {}
    next_cursor = safe_int(cursor.get("next"), 0)
    is_end = bool(cursor.get("is_end")) or next_cursor == 0
    items: list[dict] = []
    for key in ("top_replies", "replies"):
        value = data.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    comments: list[Comment] = []
    seen: set[int] = set()
    for item in items:
        rpid = safe_int(item.get("rpid"), 0)
        if rpid and rpid in seen:
            continue
        content = item.get("content") or {}
        text = str(content.get("message") or "").strip()
        if not text:
            continue
        if rpid:
            seen.add(rpid)
        comments.append(
            Comment(
                id=f"{bvid}:{rpid}" if rpid else f"{bvid}:c:{hashlib.sha1(text.encode()).hexdigest()[:12]}",
                bvid=bvid,
                text=text,
                ctime=safe_int(item.get("ctime"), 0),
                like=safe_int(item.get("like"), 0),
                zone=zone,
                video_title=video_title,
                mode=mode,
                aid=aid,
            )
        )
    return comments, next_cursor, is_end


def parse_danmaku_xml(xml: str, *, bvid: str, zone: str = "",
                      video_title: str = "") -> list[Danmaku]:
    if not xml or "<d " not in xml:
        return []
    result: list[Danmaku] = []
    for match in _DANMAKU_RE.finditer(xml):
        raw_params, raw_text = match.group(1), match.group(2)
        text = html_lib.unescape(raw_text).strip()
        if not text:
            continue
        params = raw_params.split(",")
        progress = 0.0
        try:
            progress = float(params[0])
        except (IndexError, ValueError):
            pass
        ctime = safe_int(params[4], 0) if len(params) > 4 else 0
        result.append(
            Danmaku(
                id=f"{bvid}:d:{hashlib.sha1((raw_text + str(progress)).encode()).hexdigest()[:12]}",
                bvid=bvid,
                text=text,
                ctime=ctime,
                zone=zone,
                video_title=video_title,
                progress=progress,
            )
        )
    return result


def parse_hot_search(payload: dict) -> list[dict]:
    trending = ((payload.get("data") or {}).get("trending") or {})
    items = trending.get("list") or []
    result: list[dict] = []
    for index, item in enumerate(items, start=1):
        keyword = str(item.get("keyword") or item.get("show_name") or "").strip()
        if not keyword:
            continue
        result.append(
            {
                "rank": index,
                "keyword": keyword,
                "heat": safe_int(item.get("heat_score"), 0),
            }
        )
    return result


def parse_wbi_keys(nav_payload: dict) -> tuple[str, str] | None:
    data = nav_payload.get("data") or {}
    wbi = data.get("wbi_img") or {}
    img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    return (img, sub) if img and sub else None


# ---------------------------------------------------------------- 客户端
class BiliSource:
    def __init__(
        self,
        *,
        http: HttpClient | None = None,
        cache_dir: str | Path | None = None,
        max_rps: float = 2.0,
        timeout: float = 15.0,
        offline: bool = False,
        fixture_dir: str | Path | None = None,
        curl_fallback: bool = True,
    ):
        self.http = http or HttpClient(
            max_rps=max_rps, timeout=timeout, cache_dir=cache_dir,
            curl_fallback=curl_fallback,
        )
        self.offline = offline
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self._bootstrapped = False
        self._wbi_keys: tuple[str, str] | None = None
        self.errors: list[dict] = []
        self.search_failures: Counter[str] = Counter()
        self.comment_failures: Counter[str] = Counter()

    # ---------------------------------------------------------------- 基础
    def _record(self, source: str, reason: str) -> None:
        self.errors.append({"source": source, "reason": reason[:240]})

    def _fixture(self, name: str) -> str | None:
        if not self.fixture_dir:
            return None
        path = self.fixture_dir / name
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        if self.offline:
            spi = json.loads(self._fixture("spi.json") or "{}")
            nav = json.loads(self._fixture("nav.json") or "{}")
        else:
            try:
                # 首页种 buvid3/b_nut：搜索接口的风控依赖它。
                self.http.get(HOME, referer="", retries=1)
            except HttpError as exc:
                self._record("home", str(exc))
            try:
                spi = self.http.get_json(
                    f"{API}/x/frontend/finger/spi",
                    referer=HOME,
                    retries=1,
                )
            except HttpError as exc:
                self._record("spi", str(exc))
                spi = {}
            try:
                nav = self.http.get_json(
                    f"{API}/x/web-interface/nav",
                    referer=HOME,
                    retries=1,
                )
            except HttpError as exc:
                self._record("nav", str(exc))
                nav = {}
        data = spi.get("data") or {}
        if data.get("b_3"):
            self.http.set_cookie("buvid3", str(data["b_3"]))
        if data.get("b_4"):
            self.http.set_cookie("buvid4", str(data["b_4"]))
        self._wbi_keys = parse_wbi_keys(nav)
        self._bootstrapped = True

    def _mixin_key(self) -> str:
        if not self._wbi_keys:
            self.bootstrap()
        if not self._wbi_keys:
            return ""
        raw = self._wbi_keys[0] + self._wbi_keys[1]
        return "".join(raw[index] for index in MIXIN_KEY_ENC_TAB if index < len(raw))[:32]

    def sign(self, params: dict) -> str:
        signed = dict(params)
        signed["wts"] = int(time.time())
        mixin = self._mixin_key()
        ordered = "&".join(
            f"{urllib.parse.quote(str(key))}={urllib.parse.quote(str(value))}"
            for key, value in sorted(signed.items())
        )
        signed["w_rid"] = hashlib.md5((ordered + mixin).encode("utf-8")).hexdigest()
        return urllib.parse.urlencode(sorted(signed.items()))

    def _api(
        self,
        path: str,
        query: str = "",
        *,
        source: str = "",
        referer: str = "https://www.bilibili.com/",
        cache_ttl: int = 0,
        retry_on_risk: bool = True,
    ) -> dict:
        self.bootstrap()
        suffix = f"?{query}" if query else ""
        url = f"{API}{path}{suffix}"
        try:
            payload = self.http.get_json(url, referer=referer, cache_ttl=cache_ttl)
        except HttpError as exc:
            self._record(source or path, f"http:{exc.kind}:{exc.status}")
            return {}
        if payload.get("code") == -352 and retry_on_risk:
            self._record(source or path, "code:-352")
            self._bootstrapped = False
            self.bootstrap()
            try:
                payload = self.http.get_json(url, referer=referer, cache_ttl=0)
            except HttpError as exc:
                self._record(source or path, f"http:{exc.kind}:{exc.status}")
                return {}
        if payload.get("code") not in (0, None):
            self._record(source or path, f"code:{payload.get('code')}")
        return payload

    # ---------------------------------------------------------------- 业务
    def ranking(self, rid: int) -> list[Video]:
        if self.offline:
            text = self._fixture(f"ranking_{rid}.json") or self._fixture("ranking_1.json")
            payload = json.loads(text) if text else {}
        else:
            payload = self._api(
                "/x/web-interface/ranking/v2",
                urllib.parse.urlencode({"rid": rid, "type": "all"}),
                source=f"ranking:{rid}",
                cache_ttl=300,
            )
        return parse_ranking(payload, rid)

    def popular(self, size: int = 50) -> list[Video]:
        if self.offline:
            text = self._fixture("popular.json")
            payload = json.loads(text) if text else {}
        else:
            payload = self._api(
                "/x/web-interface/popular",
                urllib.parse.urlencode({"ps": size, "pn": 1}),
                source="popular",
                cache_ttl=300,
            )
        return parse_popular(payload)

    def search_videos(self, keyword: str, limit: int = 20, page: int = 1) -> list[Video]:
        self.bootstrap()
        if self.offline:
            text = self._fixture("search.json")
            payload = json.loads(text) if text else {}
        else:
            query = self.sign(
                {
                    "search_type": "video",
                    "keyword": keyword,
                    "page": page,
                    "order": "totalrank",
                    "duration": 0,
                    "tids": 0,
                    "web_location": 1430654,
                }
            )
            try:
                payload = self.http.get_json(
                    f"{API}/x/web-interface/wbi/search/type?{query}",
                    referer=f"{SEARCH_REFERER}all?keyword={urllib.parse.quote(keyword)}",
                    cache_ttl=1800,
                )
            except HttpError as exc:
                self.search_failures["http"] += 1
                self._record(f"search:{keyword}", f"http:{exc.kind}:{exc.status}")
                return []
            if payload.get("code") != 0:
                self.search_failures[f"code:{payload.get('code')}"] += 1
                self._record(f"search:{keyword}", f"code:{payload.get('code')}")
                return []
            data = payload.get("data") or {}
            if not data.get("result") and data.get("v_voucher"):
                self.search_failures["v_voucher"] += 1
                self._record(f"search:{keyword}", "v_voucher")
                return []
        return parse_search(payload, keyword)[:limit]

    def hot_search(self) -> list[dict]:
        if self.offline:
            text = self._fixture("hot_search.json")
            payload = json.loads(text) if text else {}
        else:
            payload = self._api(
                "/x/web-interface/search/square",
                urllib.parse.urlencode({"limit": 30}),
                source="hot_search",
                cache_ttl=300,
            )
        return parse_hot_search(payload)

    def cid_for(self, bvid: str) -> int | None:
        if not bvid:
            return None
        if self.offline:
            text = self._fixture("pagelist.json")
            payload = json.loads(text) if text else {}
        else:
            payload = self._api(
                "/x/player/pagelist",
                urllib.parse.urlencode({"bvid": bvid}),
                source="pagelist",
                cache_ttl=86400,
            )
        pages = payload.get("data") or []
        if isinstance(pages, list) and pages:
            return safe_int(pages[0].get("cid"), 0) or None
        return None

    def comments(
        self,
        aid: int,
        *,
        bvid: str = "",
        mode: int = 3,
        cursor: int = 0,
        page_size: int = 20,
        zone: str = "",
        video_title: str = "",
    ) -> tuple[list[Comment], int, bool]:
        """拉一页评论；带 buvid3 只回 3 条，所以请求期间摘掉它。"""
        if not aid:
            return [], 0, True
        if self.offline:
            name = "reply_main.json" if mode == 3 else "reply_main_mode2.json"
            text = self._fixture(name) or self._fixture("reply_main.json")
            payload = json.loads(text) if text else {}
        else:
            query = urllib.parse.urlencode(
                {"type": 1, "oid": aid, "mode": mode, "next": cursor, "ps": page_size}
            )
            with self.http.without_cookie("buvid3"):
                try:
                    payload = self.http.get_json(
                        f"{API}/x/v2/reply/main?{query}",
                        referer="https://www.bilibili.com/",
                        cache_ttl=600,
                    )
                except HttpError as exc:
                    self.comment_failures[f"http:{exc.status}"] += 1
                    self._record(f"comments:{bvid}", f"http:{exc.status}")
                    return [], 0, True
        if payload.get("code") not in (0, None):
            code = payload.get("code")
            self.comment_failures[f"code:{code}"] += 1
            self._record(f"comments:{bvid}", f"code:{code}")
            return [], 0, True
        comments, next_cursor, is_end = parse_comment_page(
            payload,
            bvid=bvid,
            aid=aid,
            zone=zone,
            video_title=video_title,
            mode=mode,
        )
        return comments, next_cursor, is_end

    def danmaku(self, cid: int) -> str:
        if not cid:
            return ""
        url = f"https://comment.bilibili.com/{cid}.xml"
        try:
            return self.http.get_text(url, referer="https://www.bilibili.com/", cache_ttl=21600)
        except HttpError as exc:
            self._record(f"danmaku:{cid}", f"http:{exc.kind}")
            return ""

    def stats(self) -> dict:
        return {
            **self.http.stats(),
            "search_failures": dict(self.search_failures),
            "comment_failures": dict(self.comment_failures),
            "errors": list(self.errors[-30:]),
        }
