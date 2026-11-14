"""HTTP 基础设施：Cookie、限速、重试、压缩解码与 CA 回退。

本机 Python 3.15 的默认 CA 校验会失败，必须在创建 SSLContext 时显式
指定 cafile（优先 /etc/ssl/cert.pem，其次 certifi）。
"""
from __future__ import annotations

import gzip
import http.cookiejar
import shutil
import subprocess
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from . import config


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _ca_file() -> str | None:
    candidates = [os.getenv("HOTMEME_CA"), "/etc/ssl/cert.pem"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    try:
        import certifi  # type: ignore

        return certifi.where()
    except Exception:
        paths = ssl.get_default_verify_paths()
        return paths.cafile if paths.cafile and Path(paths.cafile).exists() else None


def build_context() -> ssl.SSLContext:
    cafile = _ca_file()
    try:
        return ssl.create_default_context(cafile=cafile)
    except Exception:
        return ssl.create_default_context()


class RateLimiter:
    """固定最小间隔限速器，保证对目标站点的请求频率可控。"""

    def __init__(self, max_rps: float = 2.0):
        self.min_interval = 1.0 / max(0.1, max_rps)
        self._last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


class HttpClient:
    def __init__(self, timeout: float | None = None, max_rps: float | None = None):
        self.timeout = timeout or config.HTTP_TIMEOUT
        self.limiter = RateLimiter(max_rps or config.MAX_RPS)
        self.jar = http.cookiejar.CookieJar()
        self.context = build_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=self.context),
        )
        self.requests = 0

    def set_cookie(self, name: str, value: str, domain: str = ".bilibili.com") -> None:
        cookie = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=True,
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

    def _decode(self, response, body: bytes) -> bytes:
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            return self._inflate(body)
        if body[:2] == b"\x1f\x8b":
            return gzip.decompress(body)
        return body

    @staticmethod
    def _inflate(body: bytes) -> bytes:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
            try:
                return zlib.decompress(body, wbits)
            except zlib.error:
                continue
        return body

    def get(self, url: str, referer: str = "", headers: dict | None = None, retries: int = 2) -> bytes:
        req_headers = {
            "User-Agent": config.USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        if referer:
            req_headers["Referer"] = referer
        if headers:
            req_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            self.limiter.wait()
            try:
                self.requests += 1
                request = urllib.request.Request(url, headers=req_headers)
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    return self._decode(response, raw)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 412):
                    fallback = self._curl_get(url, referer)
                    if fallback is not None:
                        return fallback
                raise HttpError(f"HTTP {exc.code} {url}", exc.code) from exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as exc:
                last_error = exc
                time.sleep(0.8 * (attempt + 1))
        raise HttpError(f"请求失败 {url}: {last_error}")

    def _curl_get(self, url: str, referer: str) -> bytes | None:
        """部分接口对 Python TLS 指纹返回 412，用系统 curl 兜底。"""
        if not shutil.which("curl"):
            return None
        command = ["curl", "-sS", "-m", str(int(self.timeout)), "-A", config.USER_AGENT]
        if referer:
            command += ["-e", referer]
        command.append(url)
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.timeout + 5)
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        self.limiter.wait()
        return result.stdout

    def get_json(self, url: str, referer: str = "https://www.bilibili.com/") -> dict:
        body = self.get(url, referer=referer)
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise HttpError(f"响应不是合法 JSON: {url}") from exc

    def get_text(self, url: str, referer: str = "https://www.bilibili.com/") -> str:
        return self.get(url, referer=referer).decode("utf-8", errors="replace")

    def request_count(self) -> int:
        return self.requests


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")
