"""HTTP 基础设施：限速、Cookie、重试、压缩解码、CA 回退与文件缓存。

只依赖标准库。默认 2 rps（B站公开接口的合规线），所有请求计数可供报告复现。
"""

from __future__ import annotations

import gzip
import hashlib
import http.cookiejar
import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, kind: str = "http"):
        super().__init__(message)
        self.status = status
        self.kind = kind


def _ca_file() -> str | None:
    candidates = [os.getenv("MEME_INFO_CA"), os.getenv("HOTMEME_CA"), "/etc/ssl/cert.pem"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    try:
        import certifi  # type: ignore

        return certifi.where()
    except Exception:
        paths = ssl.get_default_verify_paths()
        return paths.cafile if paths.cafile and Path(paths.cafile).exists() else None


def build_ssl_context() -> ssl.SSLContext:
    cafile = _ca_file()
    try:
        return ssl.create_default_context(cafile=cafile)
    except Exception:
        return ssl.create_default_context()


class RateLimiter:
    """固定最小间隔（start-to-start），并发下用槽位预约。"""

    def __init__(self, max_rps: float = 2.0):
        self.min_interval = 1.0 / max(0.1, float(max_rps))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> float:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.min_interval
            delay = slot - now
        if delay > 0:
            time.sleep(delay)
        return max(0.0, delay)


class FileCache:
    """按 key 存原始字节的简单缓存；TTL 用文件 mtime 判断。"""

    def __init__(self, directory: str | Path | None):
        self.directory = Path(directory) if directory else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path | None:
        if self.directory is None:
            return None
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.directory / digest[:2] / f"{digest}.bin"

    def get(self, key: str, ttl_seconds: int) -> bytes | None:
        path = self._path(key)
        if path is None or ttl_seconds <= 0:
            return None
        try:
            age = time.time() - path.stat().st_mtime
            if age > ttl_seconds:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def set(self, key: str, body: bytes) -> None:
        path = self._path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(body)
            temporary.replace(path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def endpoint_label(url: str) -> str:
    split = urllib.parse.urlsplit(url)
    path = split.path
    host = split.hostname or ""
    if host == "comment.bilibili.com":
        return "danmaku"
    if path.endswith("/x/player/pagelist"):
        return "pagelist"
    if path.endswith("/x/v2/reply") or path.endswith("/x/v2/reply/main"):
        return "comments"
    if "/ranking/" in path:
        return "ranking"
    if path.endswith("/x/web-interface/popular"):
        return "popular"
    if "/wbi/search/type" in path:
        return "search"
    if path.endswith("/x/frontend/finger/spi"):
        return "spi"
    if path.endswith("/x/web-interface/nav"):
        return "nav"
    if path.endswith("/x/web-interface/search/square"):
        return "hot_search"
    if path == "/":
        return "home"
    return "other"


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_rps: float = 2.0,
        cache_dir: str | Path | None = None,
        user_agent: str = DEFAULT_UA,
        curl_fallback: bool = True,
    ):
        self.timeout = float(timeout)
        self.limiter = RateLimiter(max_rps)
        self.jar = http.cookiejar.CookieJar()
        self.context = build_ssl_context()
        self.cache = FileCache(cache_dir)
        self.user_agent = user_agent
        self.curl_fallback = curl_fallback
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=self.context),
        )
        self.requests = 0
        self.cache_hits = 0
        self.endpoint_counts: Counter[str] = Counter()
        self.errors: list[dict] = []
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 计数
    def _count(self, url: str, cache_hit: bool = False) -> None:
        label = endpoint_label(url)
        with self._lock:
            if cache_hit:
                self.cache_hits += 1
            else:
                self.requests += 1
                self.endpoint_counts[label] += 1

    def set_cookie(self, name: str, value: str, domain: str = ".bilibili.com") -> None:
        if not name or not value:
            return
        cookie = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        self.jar.set_cookie(cookie)

    def cookie_value(self, name: str) -> str:
        for cookie in self.jar:
            if cookie.name == name:
                return cookie.value
        return ""

    @contextmanager
    def without_cookie(self, name: str) -> Iterator[None]:
        """临时从 CookieJar 移除某 cookie（B站评论接口带 buvid3 只回 3 条）。"""
        removed: list[http.cookiejar.Cookie] = []
        for cookie in list(self.jar):
            if cookie.name == name:
                removed.append(cookie)
                try:
                    self.jar.clear(cookie.domain, cookie.path, cookie.name)
                except KeyError:
                    pass
        try:
            yield
        finally:
            for cookie in removed:
                self.jar.set_cookie(cookie)

    # ---------------------------------------------------------------- 编解码
    @staticmethod
    def _decode(response, body: bytes) -> bytes:
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip" or body[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(body)
            except OSError:
                return body
        if encoding == "deflate":
            return HttpClient._inflate(body)
        return body

    @staticmethod
    def _inflate(body: bytes) -> bytes:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
            try:
                return zlib.decompress(body, wbits)
            except zlib.error:
                continue
        return body

    # ---------------------------------------------------------------- 请求
    def get(
        self,
        url: str,
        *,
        referer: str = "",
        headers: dict | None = None,
        retries: int = 2,
        cache_ttl: int = 0,
        cache_key: str | None = None,
    ) -> bytes:
        key = cache_key or url
        if cache_ttl > 0:
            cached = self.cache.get(key, cache_ttl)
            if cached is not None:
                self._count(url, cache_hit=True)
                return cached
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        if referer:
            request_headers["Referer"] = referer
        if headers:
            request_headers.update(headers)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            self.limiter.wait()
            try:
                self._count(url)
                request = urllib.request.Request(url, headers=request_headers)
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = self._decode(response, response.read())
                    if cache_ttl > 0 and body:
                        self.cache.set(key, body)
                    return body
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 412) and self.curl_fallback:
                    fallback = self._curl_get(url, referer)
                    if fallback is not None:
                        if cache_ttl > 0 and fallback:
                            self.cache.set(key, fallback)
                        return fallback
                last_error = exc
                if exc.code < 500 and exc.code not in (403, 412, 429):
                    raise HttpError(f"HTTP {exc.code} {url}", exc.code, "http") from exc
                if attempt < retries:
                    time.sleep(0.8 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(0.8 * (attempt + 1))
        message = f"请求失败 {url}: {last_error}"
        self.errors.append({"url": url, "error": str(last_error)[:200]})
        status = getattr(last_error, "code", None) if isinstance(last_error, urllib.error.HTTPError) else None
        raise HttpError(message, status, "network")

    def _curl_get(self, url: str, referer: str) -> bytes | None:
        if not shutil.which("curl"):
            return None
        command = ["curl", "-sS", "-m", str(int(self.timeout)), "-A", self.user_agent]
        if referer:
            command += ["-e", referer]
        command.append(url)
        self.limiter.wait()
        self._count(url)
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.timeout + 5)
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        return result.stdout

    def get_json(
        self,
        url: str,
        *,
        referer: str = "https://www.bilibili.com/",
        retries: int = 2,
        cache_ttl: int = 0,
        cache_key: str | None = None,
    ) -> dict:
        body = self.get(
            url,
            referer=referer,
            retries=retries,
            cache_ttl=cache_ttl,
            cache_key=cache_key,
        )
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise HttpError(f"响应不是合法 JSON: {url}", kind="bad_json") from exc

    def get_text(
        self,
        url: str,
        *,
        referer: str = "https://www.bilibili.com/",
        retries: int = 2,
        cache_ttl: int = 0,
    ) -> str:
        return self.get(url, referer=referer, retries=retries, cache_ttl=cache_ttl).decode(
            "utf-8", errors="replace"
        )

    def stats(self) -> dict:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "endpoint_counts": dict(self.endpoint_counts),
            "errors": list(self.errors[-20:]),
        }
