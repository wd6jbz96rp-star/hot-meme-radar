"""B站数据源适配层。

已验证（2026-11）：
- `x/frontend/finger/spi` 可直接拿到 buvid3/buvid4，用它请求分区排行榜，无需登录；
- `x/web-interface/ranking/v2?rid=` 返回约 90-100 条带统计的视频；
- `x/v2/reply` 免登录可读热评；
- `comment.bilibili.com/{cid}.xml` 返回 raw-deflate 压缩弹幕；
- `wbi/search/type` 需要 wbi 签名，可用于候选扩散度探测。
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from .http import HttpClient, HttpError, quote
from .zones import zone_name, zone_weight

API = "https://api.bilibili.com"
DANMAKU_URL = "https://comment.bilibili.com/{cid}.xml"
EM_TAG_RE = re.compile(r"</?em[^>]*>")


def clean_title(text: str) -> str:
    return EM_TAG_RE.sub("", text or "").strip()

# wbi 签名混淆表（B 站公开算法）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _stat_value(stat: dict, key: str) -> int:
    try:
        return int(stat.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def parse_ranking(payload: dict, rid: int) -> list[dict]:
    """把排行榜响应转成统一的视频记录。"""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    items = (payload.get("data") or {}).get("list") or []
    videos: list[dict] = []
    for item in items:
        stat = item.get("stat") or {}
        owner = item.get("owner") or {}
        tid = item.get("tid")
        tname = item.get("tname") or ""
        videos.append(
            {
                "aid": item.get("aid"),
                "bvid": item.get("bvid"),
                "cid": item.get("cid"),
                "title": item.get("title") or "",
                "desc": item.get("desc") or "",
                "pubdate": int(item.get("pubdate") or 0),
                "duration": int(item.get("duration") or 0),
                "tid": tid,
                "tname": tname,
                "zone": zone_name(tid if isinstance(tid, int) else rid, tname),
                "owner_mid": owner.get("mid"),
                "owner_name": owner.get("name") or "",
                "view": _stat_value(stat, "view"),
                "danmaku": _stat_value(stat, "danmaku"),
                "reply": _stat_value(stat, "reply"),
                "like": _stat_value(stat, "like"),
                "coin": _stat_value(stat, "coin"),
                "favorite": _stat_value(stat, "favorite"),
                "share": _stat_value(stat, "share"),
                "score": _stat_value(item, "score"),
            }
        )
    return videos


_REPLY_TAG = re.compile(r"<d p=\"([^\"]*)\">(.*?)</d>", re.S)


def _reply_items(data: dict) -> list[dict]:
    """`/x/v2/reply/main` 同时给 `replies` 与 `top_replies`（置顶/热评），按 rpid 去重。"""
    items: list[dict] = []
    seen: set = set()
    for key in ("top_replies", "replies"):
        for item in data.get(key) or []:
            if not isinstance(item, dict):
                continue
            rpid = item.get("rpid")
            if rpid is not None:
                if rpid in seen:
                    continue
                seen.add(rpid)
            items.append(item)
    return items


def parse_comments(payload: dict) -> list[dict]:
    """解析评论响应（兼容旧 `/x/v2/reply` 与 `/x/v2/reply/main`），保留 ctime/like/rpid。"""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    data = payload.get("data") or {}
    comments: list[dict] = []
    for item in _reply_items(data):
        content = item.get("content") or {}
        message = (content.get("message") or "").strip()
        if not message:
            continue
        try:
            ctime = int(item.get("ctime") or 0)
        except (TypeError, ValueError):
            ctime = 0
        try:
            like = int(item.get("like") or 0)
        except (TypeError, ValueError):
            like = 0
        comments.append(
            {
                "text": message,
                "like": like,
                "ctime": ctime,
                "rpid": item.get("rpid"),
            }
        )
    return comments


def parse_comment_page(payload: dict) -> tuple[list[dict], int]:
    """返回 (评论, next 游标)；`is_end` / 缺失 / 不可解析时回 0（不再翻页）。"""
    comments = parse_comments(payload)
    cursor = (payload.get("data") or {}).get("cursor") or {}
    if cursor.get("is_end"):
        return comments, 0
    try:
        return comments, int(cursor.get("next") or 0)
    except (TypeError, ValueError):
        return comments, 0


def parse_danmaku(xml_text: str) -> list[dict]:
    """解析弹幕 XML，返回文本与出现秒数。"""
    results: list[dict] = []
    for match in _REPLY_TAG.finditer(xml_text):
        attrs = match.group(1).split(",")
        text = html_lib.unescape(match.group(2)).strip()
        if not text:
            continue
        try:
            appear_at = float(attrs[0])
        except (IndexError, ValueError):
            appear_at = 0.0
        results.append({"text": text, "at": appear_at})
    return results


def parse_search_square(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    items = ((payload.get("data") or {}).get("trending") or {}).get("list") or []
    return [
        {
            "keyword": item.get("keyword") or item.get("show_name") or "",
            "heat": int(item.get("heat_score") or 0),
        }
        for item in items
        if item.get("keyword") or item.get("show_name")
    ]


def parse_search_videos(payload: dict, limit: int = 20) -> list[dict]:
    """把 B站 搜索结果转成视频记录，供"梗搜索"通道使用。"""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    results = (payload.get("data") or {}).get("result") or []
    videos: list[dict] = []
    for item in results[:limit]:
        bvid = item.get("bvid")
        if not bvid:
            continue
        duration = item.get("duration") or "0:0"
        try:
            minutes, seconds = str(duration).split(":")[-2:]
            duration_seconds = int(minutes) * 60 + int(seconds)
        except (ValueError, IndexError):
            duration_seconds = 0
        try:
            typeid = int(item.get("typeid") or 0)
        except (TypeError, ValueError):
            typeid = 0
        videos.append(
            {
                "aid": item.get("aid"),
                "bvid": bvid,
                "cid": None,
                "title": clean_title(item.get("title")),
                "desc": (item.get("description") or "").strip(),
                "pubdate": int(item.get("pubdate") or 0),
                "duration": duration_seconds,
                "tid": typeid,
                "tname": item.get("typename") or "",
                "zone": "梗搜索",
                "owner_mid": None,
                "owner_name": item.get("author") or "",
                "view": int(item.get("play") or 0),
                "danmaku": int(item.get("video_review") or 0),
                "reply": 0,
                "like": int(item.get("favorites") or 0),
                "coin": 0,
                "favorite": int(item.get("favorites") or 0),
                "share": 0,
                "score": 0,
                "tags": item.get("tag") or "",
                "source": "search",
                "zone_weight": 1.4,
            }
        )
    return videos


class BiliClient:
    def __init__(self, http: HttpClient | None = None, offline: bool | None = None,
                 fixture_dir: Path | None = None):
        self.http = http or HttpClient()
        self.offline = config.OFFLINE if offline is None else offline
        self.fixture_dir = Path(fixture_dir or config.FIXTURE_DIR)
        self._wbi_keys: tuple[str, str] | None = None
        self._bootstrapped = False
        self.errors: list[dict] = []

    # ---------- 基础请求 ----------
    def _record_error(self, source: str, error: Exception) -> None:
        self.errors.append({"source": source, "error": str(error)[:200]})

    def _fixture(self, name: str) -> str | None:
        path = self.fixture_dir / name
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def _api(self, path: str, query: str = "", source: str = "") -> dict:
        suffix = f"?{query}" if query else ""
        payload = self.http.get_json(f"{API}{path}{suffix}")
        if payload.get("code") == -352:
            self.bootstrap(force=True)
            payload = self.http.get_json(f"{API}{path}{suffix}")
        return payload

    def bootstrap(self, force: bool = False) -> None:
        if self._bootstrapped and not force:
            return
        if self.offline:
            payload = self._fixture("spi.json")
            data = (json.loads(payload) if payload else {}).get("data") or {}
        else:
            try:
                data = self.http.get_json(f"{API}/x/frontend/finger/spi").get("data") or {}
            except HttpError as exc:
                self._record_error("spi", exc)
                data = {}
        if data.get("b_3"):
            self.http.set_cookie("buvid3", str(data["b_3"]))
        if data.get("b_4"):
            self.http.set_cookie("buvid4", str(data["b_4"]))
        self._wbi_keys = None
        self._bootstrapped = True

    def _ensure(self) -> None:
        if not self._bootstrapped:
            self.bootstrap()

    # ---------- wbi ----------
    def _wbi_keys_load(self) -> tuple[str, str]:
        if self._wbi_keys:
            return self._wbi_keys
        if self.offline:
            payload = self._fixture("nav.json")
            data = (json.loads(payload) if payload else {}).get("data") or {}
        else:
            data = self.http.get_json(f"{API}/x/web-interface/nav").get("data") or {}
        wbi = data.get("wbi_img") or {}
        img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        self._wbi_keys = (img, sub)
        return self._wbi_keys

    def _mixin_key(self) -> str:
        img, sub = self._wbi_keys_load()
        raw = img + sub
        return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]

    def sign(self, params: dict) -> str:
        mixed = self._mixin_key()
        signed = dict(params)
        signed["wts"] = int(time.time())
        items = sorted(signed.items())
        query = "&".join(
            f"{quote(str(key))}={quote(str(value))}" for key, value in items
        )
        signed["w_rid"] = hashlib.md5((query + mixed).encode("utf-8")).hexdigest()
        return "&".join(
            f"{quote(str(key))}={quote(str(value))}"
            for key, value in sorted(signed.items())
        )

    # ---------- 业务接口 ----------
    def ranking(self, rid: int) -> list[dict]:
        self._ensure()
        if self.offline:
            payload_text = self._fixture(f"ranking_{rid}.json") or self._fixture("ranking_119.json")
            payload = json.loads(payload_text) if payload_text else {"code": -1}
        else:
            try:
                payload = self._api(
                    "/x/web-interface/ranking/v2",
                    f"rid={rid}&type=all",
                    source=f"ranking:{rid}",
                )
            except HttpError as exc:
                self._record_error(f"ranking:{rid}", exc)
                return []
        videos = parse_ranking(payload, rid)
        for video in videos:
            video["zone_weight"] = zone_weight(video.get("tid"), video.get("tname"))
            video["source"] = "zone"
            video["tags"] = ""
        return videos

    def popular(self, size: int = 50) -> list[dict]:
        self._ensure()
        if self.offline:
            payload_text = self._fixture("popular.json")
            payload = json.loads(payload_text) if payload_text else {"code": -1}
        else:
            try:
                payload = self._api("/x/web-interface/popular", f"ps={size}&pn=1",
                                    source="popular")
            except HttpError as exc:
                self._record_error("popular", exc)
                return []
        videos = parse_ranking(payload, 0)
        for video in videos:
            video["zone_weight"] = zone_weight(video.get("tid"), video.get("tname"))
            video["source"] = "popular"
            video["tags"] = ""
        return videos

    def search_videos(self, keyword: str, limit: int = 20) -> list[dict]:
        """B站 视频搜索：用于"热梗合集/梗百科"类视频，显著提升梗的召回。"""
        self._ensure()
        if self.offline:
            payload_text = self._fixture("search_videos.json")
            payload = json.loads(payload_text) if payload_text else {"code": -1}
        else:
            try:
                query = self.sign({"search_type": "video", "keyword": keyword, "page": 1})
                payload = self.http.get_json(
                    f"{API}/x/web-interface/wbi/search/type?{query}",
                    referer="https://search.bilibili.com/",
                )
                if payload.get("code") == -352:
                    self.bootstrap(force=True)
                    query = self.sign({"search_type": "video", "keyword": keyword, "page": 1})
                    payload = self.http.get_json(
                        f"{API}/x/web-interface/wbi/search/type?{query}",
                        referer="https://search.bilibili.com/",
                    )
            except HttpError as exc:
                self._record_error(f"search:{keyword}", exc)
                return []
        return parse_search_videos(payload, limit)

    def cid_for(self, bvid: str) -> int | None:
        """搜索结果不含 cid，抓弹幕前补一次分 P 列表。"""
        if self.offline:
            payload_text = self._fixture("pagelist.json")
            payload = json.loads(payload_text) if payload_text else {"code": -1}
        else:
            try:
                payload = self._api("/x/player/pagelist", f"bvid={bvid}", source="pagelist")
            except HttpError as exc:
                self._record_error("pagelist", exc)
                return None
        pages = payload.get("data") or []
        if isinstance(pages, list) and pages:
            try:
                return int(pages[0].get("cid"))
            except (TypeError, ValueError):
                return None
        return None

    # 视频/评论接口对「无评论」「视频不可见」返回的 code：不是采集故障，不记 error。
    BENIGN_COMMENT_CODES = {-404, 12002, 12061, 12071}

    @contextmanager
    def _without_buvid3(self):
        """评论请求临时去掉 buvid3。

        实测（2026-11，匿名）：带 buvid3 时 `/x/v2/reply/main` 只回 3 条；
        去掉后同 oid/mode/next 回 20 条。buvid3 仍用于其他接口防风控，
        这里只在评论请求期间摘掉、请求后原样放回。
        """
        jar = getattr(getattr(self, "http", None), "jar", None)
        if jar is None:
            yield
            return
        saved = [cookie for cookie in list(jar) if cookie.name == "buvid3"]
        for cookie in saved:
            jar.clear(cookie.domain, cookie.path, cookie.name)
        try:
            yield
        finally:
            for cookie in saved:
                jar.set_cookie(cookie)

    def comment_page(self, aid: int, page: int = 1, mode: int = 3,
                     cursor: int | None = None, ps: int = 20) -> tuple[list[dict], int]:
        """一级评论页：`/x/v2/reply/main`（免 wbi）。

        mode=3 热度序（默认），mode=2 时间序；`next` 为游标（首屏 0），返回 (评论, 下一页游标)。
        单页失败记录错误并返回空列表，不抛异常。
        """
        self._ensure()
        if self.offline:
            # 离线回放：优先 mode 专属 fixture（真实两 mode 返回不同评论集），
            # 回退到通用 reply_main.json / 旧结构 reply.json。
            payload_text = (self._fixture(f"reply_main_mode{mode}.json")
                            or self._fixture("reply_main.json")
                            or self._fixture("reply.json"))
            payload = json.loads(payload_text) if payload_text else {"code": -1}
            return parse_comment_page(payload)
        offset = 0 if page <= 1 else (page - 1) * ps
        next_cursor = offset if cursor is None else int(cursor)
        try:
            with self._without_buvid3():
                payload = self._api(
                    "/x/v2/reply/main",
                    f"type=1&oid={int(aid)}&mode={int(mode)}&next={next_cursor}&ps={int(ps)}",
                    source=f"reply:{mode}",
                )
        except HttpError as exc:
            self._record_error(f"reply:{aid}:{mode}", exc)
            return [], 0
        code = payload.get("code")
        if code not in (0, None) and code not in self.BENIGN_COMMENT_CODES:
            self._record_error(f"reply:{aid}:{mode}", RuntimeError(f"code={code}"))
            return [], 0
        return parse_comment_page(payload)

    def comments(self, aid: int, page: int = 1, mode: int = 3,
                 cursor: int | None = None, ps: int = 20) -> list[dict]:
        """一级评论列表（单页）；见 `comment_page`。"""
        return self.comment_page(aid, page=page, mode=mode, cursor=cursor, ps=ps)[0]

    def danmaku(self, cid: int) -> str:
        self._ensure()
        if self.offline:
            payload_text = self._fixture("danmaku.xml")
            return payload_text or ""
        try:
            raw = self.http.get(DANMAKU_URL.format(cid=cid))
            text = raw.decode("utf-8", errors="replace")
            if "<d p=" not in text[:2000] and "<?xml" not in text[:200]:
                text = self.http._inflate(raw).decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            return text
        except HttpError as exc:
            self._record_error(f"danmaku:{cid}", exc)
            return ""

    def hot_search(self) -> list[dict]:
        if self.offline:
            payload_text = self._fixture("search_square.json")
            payload = json.loads(payload_text) if payload_text else {"code": -1}
        else:
            try:
                payload = self._api(
                    "/x/web-interface/search/square",
                    "limit=20",
                    source="search_square",
                )
            except HttpError as exc:
                self._record_error("search_square", exc)
                return []
        return parse_search_square(payload)

