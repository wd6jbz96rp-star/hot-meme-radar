"""OpenAI 兼容 Chat Completions 客户端（标准库 urllib，不引入第三方依赖）。

环境变量（前缀 `HOTMEME_LLM_`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOTMEME_LLM_BASE_URL` | `https://api.deepseek.com/v1` | 兼容端点根路径 |
| `HOTMEME_LLM_API_KEY` | 空 | 为空时 `chat_json` 直接抛 `not_configured`，**不发网络请求** |
| `HOTMEME_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `HOTMEME_LLM_TIMEOUT` | `30` | 单次请求超时（秒） |
| `HOTMEME_LLM_MAX_CALLS` | `60` | 单实例累计调用上限（含重试） |

设计要点：
- temperature=0、`response_format={"type":"json_object"}`，只接受 JSON 对象；
- JSON 解析失败 / schema 校验失败 / 可重试传输错误 → **重试 1 次**（重试也计入调用数）；
- 错误分类为 `LlmError.kind`：`not_configured / auth / rate_limit / timeout / network /
  http / bad_json / schema / budget`，调用方据此决定是否降级；
- API key 只进请求头，`repr()`、`stats()`、异常消息里都不出现明文。
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_CALLS = 60

_JSON_RETRY_HINT = (
    "上一次输出不是合法 JSON 对象。请只输出一个 JSON 对象，不要 Markdown 代码块、"
    "不要解释文字，字段必须符合 schema。"
)


class LlmError(RuntimeError):
    """LLM 调用错误。`kind` 是稳定的错误分类，调用方不应解析 message。"""

    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable

    def __str__(self) -> str:  # pragma: no cover - 由 RuntimeError 提供
        return f"[{self.kind}] {self.args[0]}"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LlmConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    max_calls: int = DEFAULT_MAX_CALLS
    temperature: float = 0.0
    max_tokens: int = 800

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LlmConfig":
        """从 env（默认 os.environ）构造；测试可传字典。"""
        if env is None:
            return cls(
                base_url=os.getenv("HOTMEME_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
                api_key=os.getenv("HOTMEME_LLM_API_KEY", ""),
                model=os.getenv("HOTMEME_LLM_MODEL", DEFAULT_MODEL),
                timeout=_env_float("HOTMEME_LLM_TIMEOUT", DEFAULT_TIMEOUT),
                max_calls=_env_int("HOTMEME_LLM_MAX_CALLS", DEFAULT_MAX_CALLS),
            )
        return cls(
            base_url=str(env.get("HOTMEME_LLM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/"),
            api_key=str(env.get("HOTMEME_LLM_API_KEY", "")),
            model=str(env.get("HOTMEME_LLM_MODEL", DEFAULT_MODEL)),
            timeout=float(env.get("HOTMEME_LLM_TIMEOUT", DEFAULT_TIMEOUT)),
            max_calls=int(env.get("HOTMEME_LLM_MAX_CALLS", DEFAULT_MAX_CALLS)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def redacted(self) -> dict[str, Any]:
        """可安全写日志/审计的配置快照（不泄露 key）。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
            "max_calls": self.max_calls,
            "api_key": "***" if self.api_key else "",
        }


@dataclass
class LlmStats:
    calls: int = 0
    retries: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "errors": self.errors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


# transport(url, payload, headers, timeout) -> (status, body_bytes)
Transport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, bytes]]


def urllib_transport(url: str, payload: dict[str, Any], headers: dict[str, str],
                     timeout: float) -> tuple[int, bytes]:
    """默认传输层：POST JSON，返回 (status, body)。错误由 ChatClient 分类。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx 带 body，便于分类
        return exc.code, exc.read() or b""


class ChatClient:
    """最小可用的 OpenAI 兼容 JSON 客户端。测试注入 `transport`，不碰网络。"""

    def __init__(self, config: LlmConfig | None = None, *,
                 transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 retry_backoff: float = 0.2) -> None:
        self.config = config or LlmConfig.from_env()
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self.retry_backoff = retry_backoff
        self.stats = LlmStats()

    # ---- 审计友好 ----
    def __repr__(self) -> str:
        cfg = self.config.redacted()
        return (f"ChatClient(base_url={cfg['base_url']!r}, model={cfg['model']!r}, "
                f"api_key={cfg['api_key']!r})")

    @property
    def model(self) -> str:
        return self.config.model

    def stats_dict(self) -> dict[str, int]:
        return self.stats.as_dict()

    # ---- 主入口 ----
    def chat_json(self, messages: list[dict[str, str]],
                  schema: dict[str, Any] | None = None,
                  retries: int = 1) -> dict[str, Any]:
        """调用一次 chat/completions，返回解析并校验后的 JSON 对象。

        重试策略：`retries=1` 表示最多 2 次调用（首次 + 1 次重试）。重试只针对
        `bad_json / schema / timeout / network / rate_limit / 5xx`；auth 与 4xx
        直接失败，避免把预算烧在配置错误上。
        """
        if not self.config.configured:
            raise LlmError("not_configured", "未配置 HOTMEME_LLM_API_KEY")
        last_error: LlmError | None = None
        for attempt in range(retries + 1):
            if self.stats.calls >= self.config.max_calls:
                raise LlmError("budget", f"已达到 HOTMEME_LLM_MAX_CALLS={self.config.max_calls}")
            self.stats.calls += 1
            try:
                obj = self._once(messages if attempt == 0 else self._with_retry_hint(messages),
                                 schema)
                return obj
            except LlmError as exc:
                last_error = exc
                self.stats.errors += 1
                if attempt >= retries or not exc.retryable:
                    raise
                self.stats.retries += 1
                if self.retry_backoff:
                    self._sleep(self.retry_backoff)
        raise last_error or LlmError("http", "未知错误")

    @staticmethod
    def _with_retry_hint(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        return [*messages, {"role": "user", "content": _JSON_RETRY_HINT}]

    def _once(self, messages: list[dict[str, str]],
              schema: dict[str, Any] | None) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        url = f"{self.config.base_url}/chat/completions"
        try:
            status, body = self._transport(url, payload, headers, self.config.timeout)
        except urllib.error.URLError as exc:  # 连接失败/超时（DNS、TLS、socket）
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise LlmError("timeout", "LLM 请求超时", retryable=True) from exc
            raise LlmError("network", f"LLM 网络错误: {type(reason).__name__}",
                           retryable=True) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LlmError("timeout", "LLM 请求超时", retryable=True) from exc
        except OSError as exc:
            raise LlmError("network", f"LLM 传输失败: {type(exc).__name__}",
                           retryable=True) from exc

        if status in (401, 403):
            raise LlmError("auth", f"LLM 鉴权失败 (HTTP {status})", status=status)
        if status == 429:
            raise LlmError("rate_limit", f"LLM 限流 (HTTP {status})", status=status,
                           retryable=True)
        if status >= 500:
            raise LlmError("http", f"LLM 服务端错误 (HTTP {status})", status=status,
                           retryable=True)
        if status >= 400:
            raise LlmError("http", f"LLM 请求错误 (HTTP {status})", status=status)

        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LlmError("bad_json", "响应不是 JSON 编码", status=status,
                           retryable=True) from exc

        usage = data.get("usage") or {}
        self.stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.stats.completion_tokens += int(usage.get("completion_tokens") or 0)

        choices = data.get("choices") or []
        if not choices:
            raise LlmError("http", "响应缺少 choices", status=status, retryable=True)
        content = (choices[0].get("message") or {}).get("content") or ""
        try:
            obj = parse_json_object(content)
        except ValueError as exc:
            raise LlmError("bad_json", f"模型输出不是合法 JSON: {exc}", retryable=True) from exc
        if schema is not None:
            validate_schema(obj, schema)
        return obj


# ---------------------------------------------------------------------------
# JSON 解析与轻量 schema 校验（避免引入 jsonschema）
# ---------------------------------------------------------------------------

def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` / ``` ... ``` 包裹。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    lines = lines[1:]  # 去掉开头的 ``` 或 ```json
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines).strip()


def _first_json_object(text: str) -> str | None:
    """从任意文本里找第一个括号平衡的 JSON 对象（跳过字符串内的括号）。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
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
                    return text[start:index + 1]
        start = text.find("{", start + 1)
    return None


def parse_json_object(text: str) -> dict[str, Any]:
    """容忍 Markdown 包裹与前后缀说明，解析出 JSON 对象。

    非法输入抛 ValueError（调用方转换为 `bad_json` 并重试）。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("空响应")
    for candidate in (strip_code_fence(text), text.strip()):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if parsed is not None:
            raise ValueError("顶层不是 JSON 对象")
    found = _first_json_object(text)
    if found is None:
        raise ValueError("找不到 JSON 对象")
    try:
        parsed = json.loads(found)
    except ValueError as exc:
        raise ValueError(f"JSON 语法错误: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("顶层不是 JSON 对象")
    return parsed


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(obj: Any, schema: dict[str, Any], path: str = "$") -> None:
    """支持 object/array/string/number/integer/boolean 的子集校验。

    只覆盖审核模块用到的关键字：type/required/properties/items/enum/
    minimum/maximum/minLength/maxLength。校验失败抛 `LlmError(kind="schema")`。
    """
    expected = schema.get("type")
    if expected and not _type_ok(obj, expected):
        raise LlmError("schema", f"{path} 应为 {expected}", retryable=True)
    if "enum" in schema and obj not in schema["enum"]:
        raise LlmError("schema", f"{path} 不在枚举 {schema['enum']} 内", retryable=True)
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            raise LlmError("schema", f"{path} < minimum", retryable=True)
        if "maximum" in schema and obj > schema["maximum"]:
            raise LlmError("schema", f"{path} > maximum", retryable=True)
    if isinstance(obj, str):
        if "minLength" in schema and len(obj) < schema["minLength"]:
            raise LlmError("schema", f"{path} 过短", retryable=True)
        if "maxLength" in schema and len(obj) > schema["maxLength"]:
            raise LlmError("schema", f"{path} 过长", retryable=True)
    if isinstance(obj, dict):
        for key in schema.get("required", ()):
            if key not in obj:
                raise LlmError("schema", f"{path} 缺少字段 {key}", retryable=True)
        for key, sub in (schema.get("properties") or {}).items():
            if key in obj:
                validate_schema(obj[key], sub, f"{path}.{key}")
    if isinstance(obj, list) and "items" in schema:
        item_schema = schema["items"]
        for index, item in enumerate(obj):
            validate_schema(item, item_schema, f"{path}[{index}]")
