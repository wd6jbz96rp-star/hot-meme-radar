"""可选的 LLM 语义审核 / 候选抽取。

核心查询与打分不依赖 LLM：没有 key、超时、坏 JSON 都只降级，不影响规则榜。
这里提供两个交接单接口：
- ``extract_candidates(comments, llm)``：从评论抽候选，硬校验 surface 必须是原文子串；
- ``review_candidates(candidates, llm)``：对候选做 meme/current 判定与 approve/reject。

安全：key 只从环境变量或 key 文件读取，不打印、不写报告；请求失败不抛出到调用方。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .http import FileCache, build_ssl_context
from .knowledge import Knowledge
from .models import (
    PROMPT_VERSION,
    Candidate,
    Comment,
    Meme,
    clamp,
    iso,
    stable_id,
)
from .text import find_quote, normalize_surface

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 45.0
DEFAULT_MAX_CALLS = 40

_JSON_RETRY_HINT = (
    "上一次输出不是合法 JSON 对象。请只输出一个 JSON 对象，不要 Markdown、不要解释。"
)

VERIFY_PROMPT_VERSION = "meme-info/verify-v2"
EXTRACT_PROMPT_VERSION = "meme-info/extract-v1"

VERIFY_SCHEMA = {
    "type": "object",
    "required": ["is_meme", "is_current", "heat", "confidence", "reason"],
    "properties": {
        "is_meme": {"type": "boolean"},
        "is_current": {"type": "boolean"},
        "heat": {"type": "number"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
}

EXTRACT_SCHEMA = {
    "type": "object",
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["surface", "canonical", "type", "confidence"],
                "properties": {
                    "surface": {"type": "string"},
                    "canonical": {"type": "string"},
                    "type": {"type": "string"},
                    "meaning": {"type": "string"},
                    "origin_quote": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


class LlmError(RuntimeError):
    def __init__(self, kind: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _read_key_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


@dataclass(frozen=True)
class LlmConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    max_calls: int = DEFAULT_MAX_CALLS
    temperature: float = 0.0
    max_tokens: int = 900

    @classmethod
    def from_env(cls) -> "LlmConfig":
        key = _env("MEME_INFO_LLM_API_KEY") or _env("HOTMEME_LLM_API_KEY")
        if not key:
            key = _read_key_file(
                _env("MEME_INFO_LLM_API_KEY_FILE")
                or _env("HOTMEME_LLM_API_KEY_FILE")
                or os.path.expanduser("~/.hotmeme_llm_key")
            )
        try:
            timeout = float(_env("MEME_INFO_LLM_TIMEOUT", str(DEFAULT_TIMEOUT)))
            max_calls = int(_env("MEME_INFO_LLM_MAX_CALLS", str(DEFAULT_MAX_CALLS)))
        except ValueError:
            timeout, max_calls = DEFAULT_TIMEOUT, DEFAULT_MAX_CALLS
        return cls(
            base_url=(_env("MEME_INFO_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/") or DEFAULT_BASE_URL),
            api_key=key,
            model=_env("MEME_INFO_LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
            timeout=timeout,
            max_calls=max_calls,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def redacted(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "configured": self.configured,
            "max_calls": self.max_calls,
        }


@dataclass
class LlmStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "errors": self.errors[-8:],
        }


def _strip_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _first_json_object(text: str) -> str | None:
    value = _strip_fence(text)
    if value.startswith("{") and value.endswith("}"):
        return value
    start = value.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return None


def _validate(payload: Any, schema: dict, path: str = "$") -> None:
    if not isinstance(payload, dict):
        raise LlmError("schema", f"{path} 不是对象")
    for key in schema.get("required", []):
        if key not in payload:
            raise LlmError("schema", f"{path} 缺少 {key}")
    for key, rule in (schema.get("properties") or {}).items():
        if key not in payload:
            continue
        value = payload[key]
        expected = rule.get("type")
        if expected == "boolean" and not isinstance(value, bool):
            raise LlmError("schema", f"{path}.{key} 不是 bool")
        if expected == "number" and not isinstance(value, (int, float)):
            raise LlmError("schema", f"{path}.{key} 不是 number")
        if expected == "string" and not isinstance(value, str):
            raise LlmError("schema", f"{path}.{key} 不是 string")
        if expected == "array" and not isinstance(value, list):
            raise LlmError("schema", f"{path}.{key} 不是数组")


class ChatClient:
    def __init__(self, config: LlmConfig | None = None, *, cache_dir: str | Path | None = None):
        self.config = config or LlmConfig.from_env()
        self.cache = FileCache(cache_dir)
        self.stats = LlmStats()
        self.context = build_ssl_context()

    def __repr__(self) -> str:  # pragma: no cover - 防泄露
        return f"ChatClient(model={self.config.model!r}, configured={self.config.configured})"

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "User-Agent": "meme-info/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout, context=self.context) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    def chat_json(self, messages: list[dict], *, schema: dict | None = None,
                  max_tokens: int | None = None, cache_ttl: int = 7 * 86400,
                  cache_key: str | None = None) -> dict:
        if not self.config.configured:
            raise LlmError("not_configured", "未配置 LLM key")
        if self.stats.calls >= self.config.max_calls:
            raise LlmError("budget", "LLM 调用预算已用完")
        key = cache_key or stable_id(
            self.config.model,
            json.dumps(messages, ensure_ascii=False, sort_keys=True),
        )
        if cache_ttl > 0:
            cached = self.cache.get(key, cache_ttl)
            if cached is not None:
                try:
                    return json.loads(cached.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        last_error: Exception | None = None
        for attempt in range(2):
            if self.stats.calls >= self.config.max_calls:
                raise LlmError("budget", "LLM 调用预算已用完")
            payload = {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": max_tokens or self.config.max_tokens,
                "response_format": {"type": "json_object"},
            }
            self.stats.calls += 1
            try:
                response = self._post(payload)
            except urllib.error.HTTPError as exc:
                status = exc.code
                self.stats.errors.append(f"http:{status}")
                if status in (401, 403):
                    raise LlmError("auth", f"LLM 鉴权失败 HTTP {status}", status=status) from exc
                if status == 429 and attempt == 0:
                    time.sleep(2.0)
                    continue
                if status >= 500 and attempt == 0:
                    time.sleep(1.0)
                    continue
                raise LlmError("http", f"LLM HTTP {status}", status=status) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.stats.errors.append("network")
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise LlmError("network", f"LLM 网络错误: {exc}") from exc
            usage = response.get("usage") or {}
            try:
                self.stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.stats.completion_tokens += int(usage.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                pass
            choices = response.get("choices") or []
            content = ""
            if choices and isinstance(choices[0], dict):
                content = (choices[0].get("message") or {}).get("content") or ""
            try:
                parsed = json.loads(_first_json_object(content) or "")
                if schema:
                    _validate(parsed, schema)
                self.cache.set(key, json.dumps(parsed, ensure_ascii=False).encode("utf-8"))
                return parsed
            except (json.JSONDecodeError, LlmError, TypeError) as exc:
                self.stats.errors.append("bad_json")
                last_error = exc
                if attempt == 0:
                    messages = messages + [{"role": "user", "content": _JSON_RETRY_HINT}]
                    continue
                raise LlmError("bad_json", f"LLM 输出不是合法 JSON: {exc}") from exc
        raise LlmError("error", f"LLM 调用失败: {last_error}")


@dataclass
class Verification:
    is_meme: bool
    is_current: bool
    heat: float
    confidence: float
    reason: str
    evidence_ids: list[str] = field(default_factory=list)
    model: str = ""
    prompt_version: str = VERIFY_PROMPT_VERSION
    cached: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_meme": self.is_meme,
            "is_current": self.is_current,
            "heat": round(self.heat, 1),
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "evidence_ids": self.evidence_ids,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "cached": self.cached,
            "error": self.error,
        }


def _evidence_packet(meme: Meme, *, limit: int = 6) -> list[dict]:
    packet: list[dict] = []
    for item in meme.meta[:4]:
        packet.append(
            {
                "id": f"video:{item.bvid}",
                "kind": "video",
                "text": item.title,
                "time": iso(item.pubdate),
                "views": item.view,
                "likes": item.like,
                "query": item.query,
            }
        )
    for item in meme.evidence[:limit]:
        packet.append(
            {
                "id": item.comment_id,
                "kind": item.kind,
                "text": item.quote,
                "time": iso(item.ctime),
                "likes": item.like,
                "video": item.video_title,
            }
        )
    return packet


def _build_verify_messages(meme: Meme, now: int, packet: list[dict]) -> list[dict]:
    system = (
        "你是中文网络热梗审核员。你只判断“候选是否是一个当前热梗”，"
        "并且只能依据用户给出的证据，不得用你的记忆补造事实。\n"
        "操作性定义：网络热梗是网络社群中自发产生、以可复制形式被反复模仿使用、"
        "需要出处或语境才能完整理解、且有可观测兴衰曲线的符号单元。\n"
        "不算梗：新闻事件、政策/赛事、人名/机构/作品名、普通词、营销话题、纯情绪词。\n"
        "is_current 要求最近约 90 天仍有证据（视频发布日期或评论时间）。\n"
        "证据不足时 confidence 必须 ≤0.5，不要为了给结论而拔高。\n"
        "只输出一个 JSON 对象，不要 Markdown。"
    )
    content = {
        "today": iso(now),
        "candidate": {
            "canonical": meme.canonical,
            "members": meme.members[:8],
            "type": meme.type,
            "known": meme.known,
            "usage": {
                "docs_30d": meme.signals.get("usage_docs"),
                "videos": meme.signals.get("usage_videos"),
                "docs_7d": meme.signals.get("usage_7d"),
            },
        },
        "evidence": packet,
        "output_schema": {
            "is_meme": "boolean",
            "is_current": "boolean",
            "heat": "number 0-100",
            "confidence": "number 0-1",
            "reason": "中文理由，≤160 字，必须引用证据",
            "evidence_ids": ["证据 id"],
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(content, ensure_ascii=False, indent=2)},
    ]


class SemanticVerifier:
    def __init__(self, llm: ChatClient | None = None, *,
                 cache_dir: str | Path | None = None):
        self.llm = llm or ChatClient()
        if cache_dir is not None:
            self.llm.cache = FileCache(cache_dir)
        self.stats = LlmStats()

    def verify_meme(self, meme: Meme, *, now: int) -> Verification:
        packet = _evidence_packet(meme)
        messages = _build_verify_messages(meme, now, packet)
        cache_key = stable_id(
            VERIFY_PROMPT_VERSION,
            self.llm.config.model,
            meme.family_key,
            json.dumps(packet, ensure_ascii=False, sort_keys=True),
        )
        before = self.llm.stats.calls
        try:
            payload = self.llm.chat_json(
                messages,
                schema=VERIFY_SCHEMA,
                cache_key=cache_key,
                max_tokens=500,
            )
        except LlmError as exc:
            return Verification(
                is_meme=False,
                is_current=False,
                heat=0.0,
                confidence=0.0,
                reason=f"审核不可用：{exc.kind}",
                error=exc.kind,
                model=self.llm.config.model,
            )
        cached = self.llm.stats.calls == before
        evidence_ids = [str(item) for item in (payload.get("evidence_ids") or [])][:20]
        allowed = {item["id"] for item in packet}
        evidence_ids = [item for item in evidence_ids if item in allowed]
        return Verification(
            is_meme=bool(payload.get("is_meme")),
            is_current=bool(payload.get("is_current")),
            heat=round(clamp(float(payload.get("heat") or 0), 0, 100), 1),
            confidence=round(clamp(float(payload.get("confidence") or 0), 0, 1), 3),
            reason=str(payload.get("reason") or "")[:240],
            evidence_ids=evidence_ids,
            model=self.llm.config.model,
            prompt_version=VERIFY_PROMPT_VERSION,
            cached=cached,
        )

    def __call__(self, meme: Meme, now: int | None = None) -> dict:
        return self.verify_meme(meme, now=now or int(time.time())).as_dict()


def _normalize_candidate(item: Any) -> dict:
    if isinstance(item, Meme):
        return {
            "candidate_id": item.family_key,
            "canonical": item.canonical,
            "surface": item.canonical,
            "members": item.members,
            "usage_videos": len({usage.bvid for usage in item.usage if usage.bvid}),
            "usage_docs": len({usage.doc_id for usage in item.usage}),
            "meme": item,
        }
    if isinstance(item, Candidate):
        return {
            "candidate_id": item.candidate_id,
            "canonical": item.canonical,
            "surface": item.surface,
            "members": [item.surface],
            "usage_videos": item.df_video,
            "usage_docs": len(item.usage),
            "meme": None,
        }
    payload = dict(item or {})
    payload.setdefault("candidate_id", stable_id(payload.get("canonical") or payload.get("surface") or ""))
    payload.setdefault("canonical", payload.get("surface") or "")
    payload.setdefault("surface", payload.get("canonical") or "")
    payload.setdefault("members", [])
    payload.setdefault("usage_videos", 0)
    payload.setdefault("usage_docs", 0)
    payload["meme"] = None
    return payload


def review_candidates(
    candidates: list[Any],
    llm: ChatClient | SemanticVerifier | None,
    *,
    max_rounds: int = 2,
    target: int = 10,
    max_calls: int = 20,
    now: int | None = None,
) -> dict:
    """交接单接口：审核候选，返回 approved/rejected/merged/rounds/calls。"""
    now = now or int(time.time())
    verifier = llm if isinstance(llm, SemanticVerifier) else SemanticVerifier(llm) if llm else None
    if verifier is None or not verifier.llm.config.configured:
        return {
            "approved": [],
            "rejected": [],
            "merged": [],
            "unclear": [],
            "rounds": 0,
            "calls": 0,
            "fallback_used": True,
            "reason": "not_configured",
            "verdicts": [],
        }
    normalized = [_normalize_candidate(item) for item in candidates]
    approved: list[dict] = []
    rejected: list[dict] = []
    unclear: list[dict] = []
    calls = 0
    rounds = 0
    for item in normalized:
        if calls >= max_calls or len(approved) >= target:
            break
        rounds = max(rounds, 1)
        meme = item.get("meme")
        if meme is None:
            # 无 Meme 对象时构造最小证据包
            meme = Meme(
                family_key=normalize_surface(item["canonical"]),
                canonical=item["canonical"],
                norm=normalize_surface(item["canonical"]),
                members=item.get("members") or [],
                usage=[],
                meta=[],
                evidence=[],
            )
        before = verifier.llm.stats.calls
        verification = verifier.verify_meme(meme, now=now)
        calls += max(0, verifier.llm.stats.calls - before)
        verdict = {
            "candidate_id": item["candidate_id"],
            "canonical": item["canonical"],
            "verdict": "unclear",
            "meme_prob": round(verification.heat / 100.0, 3),
            "novelty": 0.0,
            "origin": "",
            "reason": verification.reason,
            "evidence_ids": verification.evidence_ids,
            "merge_into": None,
            "llm": verification.as_dict(),
        }
        evidence_ok = item["usage_videos"] >= 2 and item["usage_docs"] >= 2
        if verification.error:
            verdict["verdict"] = "unclear"
            unclear.append(verdict)
        elif not verification.is_meme:
            verdict["verdict"] = "reject"
            rejected.append(verdict)
        elif not verification.is_current:
            verdict["verdict"] = "reject"
            verdict["reason"] = verification.reason or "证据显示不是近期热梗"
            rejected.append(verdict)
        elif not evidence_ok:
            verdict["verdict"] = "unclear"
            verdict["reason"] = (verification.reason + "；证据不足 2 评论/2 视频")[:240]
            unclear.append(verdict)
        else:
            verdict["verdict"] = "approve"
            approved.append(verdict)
        if len(approved) >= target:
            break
    return {
        "approved": approved,
        "rejected": rejected,
        "merged": [],
        "unclear": unclear,
        "rounds": rounds,
        "calls": calls,
        "fallback_used": bool(unclear and not approved),
        "reason": "ok" if approved else ("shortfall" if unclear else "no_approve"),
        "verdicts": approved + rejected + unclear,
    }


def _build_extract_messages(comments: list[dict], prompt_version: str) -> list[dict]:
    system = (
        "你是中文网络热梗候选抽取器。从给定评论中找出可能正在被模仿使用的梗/句式/谐音/缩写/名场面。"
        "只抽取评论原文里出现的字符串，不得编造；普通词、新闻、人名、纯情绪词不要。"
        "surface 必须是某条评论的原文子串，长度 2–12 个字（ASCII 缩写可 2–10）。"
        "evidence_ids 必须来自输入评论的 id。最多 8 个候选。只输出 JSON。"
    )
    payload = {
        "comments": comments,
        "output_schema": {
            "candidates": [
                {
                    "surface": "原文子串",
                    "canonical": "归一化写法",
                    "type": "词|句式|谐音|缩写|名场面",
                    "meaning": "一句话含义",
                    "origin_quote": "出处/语境原文",
                    "confidence": 0.0,
                    "evidence_ids": ["评论 id"],
                }
            ]
        },
        "prompt_version": prompt_version,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def extract_candidates(
    comments: list[Comment] | list[dict],
    llm: ChatClient | None,
    *,
    max_calls: int = 3,
    max_comments: int = 450,
    prompt_version: str = EXTRACT_PROMPT_VERSION,
    knowledge: Knowledge | None = None,
    now: int | None = None,
) -> dict:
    """交接单接口：LLM 从评论抽候选；无 key/失败返回空候选 + reason。"""
    now = now or int(time.time())
    client = llm or ChatClient()
    if not client.config.configured:
        return {
            "candidates": [],
            "reason": "not_configured",
            "calls": 0,
            "batches": 0,
            "sampled": 0,
            "prompt_version": prompt_version,
            "tokens": client.stats.as_dict(),
        }
    normalized: list[Comment] = []
    for item in comments:
        if isinstance(item, Comment):
            normalized.append(item)
        elif isinstance(item, dict):
            normalized.append(
                Comment(
                    id=str(item.get("id") or stable_id(item.get("text") or "")),
                    bvid=str(item.get("bvid") or ""),
                    text=str(item.get("text") or ""),
                    ctime=int(item.get("ctime") or 0),
                    like=int(item.get("like") or 0),
                    zone=str(item.get("zone") or ""),
                    video_title=str(item.get("video_title") or ""),
                )
            )
    normalized.sort(key=lambda item: (item.like, item.ctime), reverse=True)
    sampled = normalized[: max(1, max_comments)]
    batches = [
        sampled[index : index + 200] for index in range(0, len(sampled), 200)
    ][: max(1, max_calls)]
    candidates: list[dict] = []
    by_id = {item.id: item for item in sampled}
    seen: set[str] = set()
    calls = 0
    errors: list[str] = []
    for batch in batches:
        if not batch:
            continue
        comments_payload = [
            {"id": item.id, "text": item.text[:300], "likes": item.like, "time": item.ctime}
            for item in batch
        ]
        before = client.stats.calls
        try:
            payload = client.chat_json(
                _build_extract_messages(comments_payload, prompt_version),
                schema=EXTRACT_SCHEMA,
                max_tokens=1200,
                cache_key=stable_id(prompt_version, json.dumps(comments_payload, ensure_ascii=False, sort_keys=True)),
            )
        except LlmError as exc:
            errors.append(exc.kind)
            continue
        calls += max(0, client.stats.calls - before)
        for item in payload.get("candidates") or []:
            surface = str(item.get("surface") or "").strip()
            norm = normalize_surface(surface)
            if not surface or not (2 <= len(norm) <= 12) or norm in seen:
                continue
            evidence_ids = [str(x) for x in (item.get("evidence_ids") or [])][:10]
            valid_ids: list[str] = []
            origin_quote = ""
            for evidence_id in evidence_ids:
                source_comment = by_id.get(evidence_id)
                if source_comment is None:
                    continue
                quote = find_quote(source_comment.text, surface)
                if not quote:
                    continue
                valid_ids.append(evidence_id)
                origin_quote = origin_quote or quote
            if not valid_ids:
                continue
            known = False
            canonical = str(item.get("canonical") or surface).strip() or surface
            if knowledge is not None:
                entry = knowledge.lookup(surface) or knowledge.lookup(canonical)
                if entry:
                    known = True
                    canonical = entry.canonical
            seen.add(norm)
            candidates.append(
                {
                    "surface": surface,
                    "canonical": canonical,
                    "type": str(item.get("type") or "词")[:8],
                    "meaning": str(item.get("meaning") or "")[:200],
                    "origin_quote": origin_quote[:200],
                    "confidence": round(clamp(float(item.get("confidence") or 0.5), 0, 1), 3),
                    "evidence_ids": valid_ids,
                    "known": known,
                    "source": "llm_extract",
                }
            )
    return {
        "candidates": candidates,
        "reason": "ok" if candidates else (errors[0] if errors else "no_candidates"),
        "calls": calls,
        "batches": len(batches),
        "sampled": len(sampled),
        "prompt_version": prompt_version,
        "tokens": client.stats.as_dict(),
        "errors": errors,
        "generated_at": iso(now),
    }
