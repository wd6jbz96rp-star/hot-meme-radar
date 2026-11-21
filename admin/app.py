"""Phase F Admin 审核服务（独立 FastAPI，默认 127.0.0.1:8091，SSH 隧道访问）。

边界（``docs/pm/REARCHITECTURE.md`` §3.7.1 / §5.9.2）：

- **独立服务**：不 import 公开 ``app.api`` / 不改 Nginx / 不暴露公网；默认只监听
  ``127.0.0.1:8091``，通过 ``ssh -L 8091:127.0.0.1:8091`` 访问；
- **只写审核**：只经 ``meme_info.audit`` / ``meme_info.state`` 写 ``audit_event``、
  ``pool1``、``pool2``、``pool_transition``、``family_alias``；不碰 worker/报告；
- **鉴权**：``HOTMEME_ADMIN_TOKEN``（或 ``--token``）；未配置时启动直接拒绝，
  app 工厂模式下所有管理端点返回 503；未授权返回 401；
- **CSRF / 会话**：表单登录（POST body，token 不进 URL）后写 ``SameSite=Strict``
  HttpOnly 会话 cookie；或显式 ``X-Admin-Token`` 头；不使用可被跨站表单自动携带的凭证；
- **输出转义**：管理页只做 ``textContent`` 渲染（不拼 innerHTML）、无 CDN；
- **可回滚测试**：``create_app(connection=...)`` 支持注入调用方事务连接
  （测试用，生产不用），所有写操作在调用方事务内提交/回滚。

用法::

    HOTMEME_ADMIN_TOKEN=... ADMIN_DATABASE_URL=... python -m admin.app
    # 或
    python -m admin.app --host 127.0.0.1 --port 8091

浏览器打开 ``http://127.0.0.1:8091/admin/``，用管理 token 表单登录，服务端写入
SameSite=Strict HttpOnly 会话 cookie（token 不进入 URL / 访问日志）。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import html
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from meme_info import audit as audit_mod

ADMIN_TOKEN_ENV = "HOTMEME_ADMIN_TOKEN"
ADMIN_DATABASE_URL_ENV = "ADMIN_DATABASE_URL"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8091
COOKIE_NAME = "hotmeme_admin"
COOKIE_PATH = "/admin"
SESSION_SALT = b"hotmeme-admin-session-v1"

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"

ACTIONS: tuple[str, ...] = ("approve", "reject", "merge", "need_more")
REASON_REQUIRED_ACTIONS = ("reject", "merge")


# ---------------------------------------------------------------- 会话 / 鉴权
def session_cookie_value(token: str) -> str:
    """会话 cookie 值：对 token 做 HMAC，避免 cookie 里出现原始 token。"""
    return hmac.new(token.encode("utf-8"), SESSION_SALT, hashlib.sha256).hexdigest()


def token_matches(candidate: str | None, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(str(candidate), str(expected))


def _configured_token(app: FastAPI) -> str:
    return str(getattr(app.state, "admin_token", "") or "")


def _configured_dsn(app: FastAPI) -> str:
    return str(getattr(app.state, "admin_dsn", "") or "")


def _request_credentials(request: Request) -> list[str]:
    values: list[str] = []
    header = request.headers.get("x-admin-token")
    if header:
        values.append(header)
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        values.append(cookie)
    return values


def _is_authorized(app: FastAPI, request: Request) -> bool:
    token = _configured_token(app)
    if not token:
        return False
    expected_cookie = session_cookie_value(token)
    for value in _request_credentials(request):
        if token_matches(value, token) or token_matches(value, expected_cookie):
            return True
    return False


@contextlib.contextmanager
def _connection(app: FastAPI) -> Iterator[Any]:
    """每请求连接：测试注入连接优先；生产按 ADMIN_DATABASE_URL 新建只读连接。"""
    injected = getattr(app.state, "admin_connection", None)
    from meme_info import state

    if injected is not None:
        yield injected
        return
    with state.connection(_configured_dsn(app), autocommit=True) as connection:
        yield connection


def _login_page(*, message: str = "", status_code: int = 401) -> HTMLResponse:
    safe_message = html.escape(message)
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>热梗审核 · 登录</title>
<style>
body{{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;background:#0f1115;
color:#e6e6e6;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
.card{{background:#181b22;border:1px solid #2a2f3a;border-radius:12px;padding:28px;width:min(92vw,380px)}}
h1{{font-size:18px;margin:0 0 16px}} p{{color:#9aa4b2;font-size:13px;line-height:1.6}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #2a2f3a;
background:#0f1115;color:#e6e6e6;font-size:14px;margin:8px 0 14px}}
button{{width:100%;padding:10px 12px;border-radius:8px;border:0;background:#3b82f6;color:#fff;
font-size:14px;cursor:pointer}} .msg{{color:#f87171;font-size:13px;min-height:18px}}
</style></head><body>
<div class="card"><h1>热梗审核 Admin</h1>
<p>401：需要 HOTMEME_ADMIN_TOKEN。登录成功后写入 SameSite=Strict 会话 cookie（token 不出现在 URL / 日志里）。</p>
<form method="post" action="/admin/login">
  <input type="password" name="token" placeholder="ADMIN TOKEN" autocomplete="current-password" autofocus>
  <button type="submit">进入审核台</button>
</form>
<div class="msg">{safe_message}</div>
</div></body></html>"""
    return HTMLResponse(body, status_code=status_code)


def _unconfigured_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error": "admin_token_not_configured",
            "message": "未配置 HOTMEME_ADMIN_TOKEN：Admin 服务拒绝处理请求",
        },
    )


def _read_index_html() -> str:
    try:
        return INDEX_PATH.read_text(encoding="utf-8")
    except OSError:
        return (
            "<!doctype html><meta charset='utf-8'><h1>Admin 页面未安装</h1>"
            "<p>缺少 admin/static/index.html</p>"
        )


# ---------------------------------------------------------------- app 工厂
def create_app(
    *,
    token: str | None = None,
    dsn: str | None = None,
    connection: Any = None,
) -> FastAPI:
    """创建 Admin app。

    - ``token`` 缺省读 ``HOTMEME_ADMIN_TOKEN``；为空时管理端点返回 503；
    - ``dsn`` 缺省读 ``ADMIN_DATABASE_URL``（再回退 ``DATABASE_URL``，见 state）；
    - ``connection`` 仅供测试注入（调用方事务回滚用），生产不要传。
    """
    configured_token = (token if token is not None else os.getenv(ADMIN_TOKEN_ENV, "")) or ""
    configured_dsn = (
        dsn if dsn is not None else os.getenv(ADMIN_DATABASE_URL_ENV, "")
    ) or ""

    app = FastAPI(
        title="热梗雷达 · 审核 Admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.admin_token = configured_token
    app.state.admin_dsn = configured_dsn
    app.state.admin_connection = connection

    @app.get("/admin/health")
    async def admin_health() -> JSONResponse:
        """健康检查（不要求鉴权；不返回连接串/token 等敏感信息）。"""
        db_ok = False
        schema_version: int | None = None
        error = ""
        try:
            from meme_info import state

            with _connection(app) as conn:
                schema_version = state.read_schema_version(conn)
                db_ok = schema_version is not None
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛
            error = f"{type(exc).__name__}"
        return JSONResponse(
            {
                "ok": bool(db_ok and configured_token),
                "service": "hotmeme-admin",
                "auth_configured": bool(configured_token),
                "db": db_ok,
                "schema_version": schema_version,
                "llm_enabled": audit_mod.llm_enabled(),
                "error": error,
            }
        )

    def _login_response() -> Response:
        response = RedirectResponse(url="/admin/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            session_cookie_value(configured_token),
            max_age=12 * 3600,
            path=COOKIE_PATH,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/admin/", response_class=HTMLResponse)
    async def admin_index(request: Request) -> Response:
        """管理页面：需要鉴权（``X-Admin-Token`` 头或 SameSite=Strict 会话 cookie）。"""
        if not configured_token:
            return _unconfigured_response()
        if not _is_authorized(app, request):
            return _login_page()
        return HTMLResponse(_read_index_html())

    @app.post("/admin/login")
    async def admin_login(request: Request) -> Response:
        """表单登录：``application/x-www-form-urlencoded`` 的 token → 会话 cookie。

        token 只走 POST body（不放进 URL），登录成功后 303 跳回 ``/admin/``；
        失败返回 401 登录页。登录接口本身无鉴权（这是获得鉴权的入口）。
        """
        if not configured_token:
            return _unconfigured_response()
        try:
            body = (await request.body()).decode("utf-8", "replace")
            form = urllib.parse.parse_qs(body)
        except Exception:  # noqa: BLE001 - 非法表单
            form = {}
        candidate = (form.get("token") or [""])[0]
        if not token_matches(candidate, configured_token):
            return _login_page(message="token 不正确")
        return _login_response()

    @app.get("/admin/queue")
    async def admin_queue(request: Request, limit: int = 20) -> JSONResponse:
        """审核队列（family/score/pool/evidence/最近审核），只读。"""
        if not configured_token:
            return _unconfigured_response()
        if not _is_authorized(app, request):
            return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
        try:
            from meme_info import state

            with _connection(app) as conn:
                candidates = audit_mod.build_audit_queue(
                    conn, limit=max(0, min(int(limit), 100))
                )
                stats = audit_mod.audit_stats(conn)
        except Exception as exc:  # noqa: BLE001 - 返回可读错误，不 500 泄露栈
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "db_unavailable",
                    "message": type(exc).__name__,
                },
            )
        return JSONResponse(
            {
                "ok": True,
                "schema_version": audit_mod.AUDIT_SCHEMA_VERSION,
                "llm_enabled": audit_mod.llm_enabled(),
                "actions": list(ACTIONS),
                "queue": [candidate.to_dict() for candidate in candidates],
                "stats": stats,
            }
        )

    @app.post("/admin/action")
    async def admin_action(request: Request) -> JSONResponse:
        """审核动作：approve / reject / merge / need_more（人工结论写库）。"""
        if not configured_token:
            return _unconfigured_response()
        if not _is_authorized(app, request):
            return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 非法 JSON
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "invalid_json"}
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "invalid_body"}
            )
        family_key = str(payload.get("family_key") or "").strip()
        action = str(payload.get("action") or "").strip()
        operator = str(payload.get("operator") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        evidence_hash = str(payload.get("evidence_hash") or "").strip() or None
        merge_into = str(payload.get("merge_into") or "").strip() or None
        round_id: int | None = None
        if payload.get("round_id") not in (None, ""):
            try:
                round_id = int(payload["round_id"])
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400, content={"ok": False, "error": "invalid_round_id"}
                )
        if not family_key:
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "family_key_required"}
            )
        if action not in ACTIONS:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid_action", "actions": list(ACTIONS)},
            )
        if not operator:
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "operator_required"}
            )
        if action in REASON_REQUIRED_ACTIONS and not reason:
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "reason_required"}
            )
        if action == "merge" and not merge_into:
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "merge_into_required"}
            )
        kind, verdict, _state = audit_mod.ACTION_MAP[action]
        from meme_info import state

        try:
            with state.transaction(
                app.state.admin_connection
                if app.state.admin_connection is not None
                else _configured_dsn(app)
            ) as conn:
                result = audit_mod.record_audit(
                    conn,
                    family_key=family_key,
                    kind=kind,
                    verdict=verdict,
                    reason=reason,
                    operator=operator,
                    evidence_hash=evidence_hash,
                    round_id=round_id,
                    merge_into=merge_into,
                )
        except audit_mod.AuditStaleEvidenceError as exc:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "stale_evidence", "message": str(exc)},
            )
        except audit_mod.AuditNotFoundError as exc:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "not_found", "message": str(exc)},
            )
        except audit_mod.AuditValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid_request", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - 管理页给可读错误
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "write_failed",
                    "message": type(exc).__name__,
                },
            )
        return JSONResponse({"ok": True, "action": action, "result": result})

    return app


# ---------------------------------------------------------------- 启动
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m admin.app",
        description="热梗雷达审核 Admin（默认 127.0.0.1:8091，SSH 隧道访问）",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8091）")
    parser.add_argument(
        "--token",
        default=None,
        help="管理 token（缺省读 HOTMEME_ADMIN_TOKEN；不打印）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = (args.token if args.token is not None else os.getenv(ADMIN_TOKEN_ENV, "")) or ""
    if not token:
        print(
            f"拒绝启动：未配置 {ADMIN_TOKEN_ENV}（或 --token）。"
            "Admin 服务必须带鉴权运行。",
            file=sys.stderr,
        )
        return 2
    dsn = os.getenv(ADMIN_DATABASE_URL_ENV, "")
    if not dsn:
        print(
            f"提示：未设置 {ADMIN_DATABASE_URL_ENV}，请求时数据库健康检查会失败。",
            file=sys.stderr,
        )
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"警告：Admin 默认只应监听本机；当前 host={args.host}，请确认已在防火墙后。",
            file=sys.stderr,
        )
    import uvicorn

    app = create_app(token=token, dsn=dsn or None)
    uvicorn.run(app, host=str(args.host), port=int(args.port), log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
