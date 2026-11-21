"""Phase F Admin 服务测试：鉴权 / 503 / 401 / 200 / 审核动作写库（事务回滚）。

无 ``TEST_DATABASE_URL`` 时只跑纯单元测试（鉴权、页面、健康检查）；有则额外跑
Postgres 集成测试（TestClient + 注入调用方事务连接，结束时整体回滚，0 残留）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from admin import app as admin_app
from admin.app import create_app

DB_URL = os.environ.get("TEST_DATABASE_URL")
MIGRATE_URL = os.environ.get("TEST_MIGRATE_DATABASE_URL") or ""
requires_db = pytest.mark.skipif(
    not DB_URL, reason="需要 TEST_DATABASE_URL 才能跑 Postgres 集成测试"
)
REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-admin-token-123"
HEADERS = {"X-Admin-Token": TOKEN}
NOW = int(time.time())


# ================================================================ 测试工具
def _evidence(items: int = 2) -> dict:
    return {
        "items": [
            {
                "doc_id": f"doc-{index}",
                "bvid": f"BV{index:03d}",
                "kind": "comment",
                "quote": f"审核证据{index}",
                "ctime": NOW - 60,
                "like": 2,
                "zone": "鬼畜",
                "video_title": f"标题{index}",
            }
            for index in range(items)
        ],
        "badge_2x2": items >= 2,
        "badge_docs": items,
        "badge_videos": items,
        "evidence_video_count": items,
        "cred_base": 60.0,
        "updated_round": 1,
    }


# ================================================================ 1. 未配置 token → 503
def test_unconfigured_token_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOTMEME_ADMIN_TOKEN", raising=False)
    client = TestClient(create_app(token="", dsn=""))
    assert client.get("/admin/").status_code == 503
    assert client.post("/admin/action", json={}).status_code == 503
    assert client.get("/admin/queue").status_code == 503
    health = client.get("/admin/health")
    assert health.status_code == 200
    assert health.json()["auth_configured"] is False
    assert health.json()["ok"] is False


def test_token_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOTMEME_ADMIN_TOKEN", TOKEN)
    client = TestClient(create_app(dsn=""))
    assert client.get("/admin/", headers=HEADERS).status_code == 200
    assert client.get("/admin/").status_code == 401


# ================================================================ 2. 鉴权 401 / 200
def test_admin_page_requires_auth() -> None:
    client = TestClient(create_app(token=TOKEN, dsn=""))
    response = client.get("/admin/")
    assert response.status_code == 401
    assert "ADMIN TOKEN" in response.text
    assert "登录" in response.text
    wrong = client.get("/admin/", headers={"X-Admin-Token": "wrong"})
    assert wrong.status_code == 401
    ok = client.get("/admin/", headers=HEADERS)
    assert ok.status_code == 200
    assert "热梗雷达 · 审核 Admin" in ok.text
    # 页面不内联任何条目数据（数据都走 /admin/queue + textContent 渲染）
    assert "审核证据" not in ok.text


def test_admin_login_form_sets_samesite_cookie() -> None:
    client = TestClient(create_app(token=TOKEN, dsn=""))
    response = client.post(
        "/admin/login", data={"token": TOKEN}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"
    cookie = response.headers.get("set-cookie", "")
    assert "hotmeme_admin=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    # cookie 里不能出现原始 token
    assert TOKEN not in cookie
    # 之后不带 header 也能进（会话 cookie）
    assert client.get("/admin/").status_code == 200
    # 错误 token → 401 登录页
    bad = TestClient(create_app(token=TOKEN, dsn="")).post(
        "/admin/login", data={"token": "nope"}
    )
    assert bad.status_code == 401
    assert "登录" in bad.text


def test_admin_query_token_is_not_accepted() -> None:
    """token 不放进 URL（避免进入访问日志）：?token= 不再作为登录入口。"""
    client = TestClient(create_app(token=TOKEN, dsn=""))
    assert client.get("/admin/", params={"token": TOKEN}).status_code == 401


def test_session_cookie_is_hmac_not_raw_token() -> None:
    from admin.app import session_cookie_value

    value = session_cookie_value(TOKEN)
    assert value != TOKEN
    assert len(value) == 64
    assert session_cookie_value(TOKEN) == value


def test_api_requires_auth() -> None:
    client = TestClient(create_app(token=TOKEN, dsn=""))
    assert client.get("/admin/queue").status_code == 401
    assert client.post("/admin/action", json={}).status_code == 401
    assert (
        client.post(
            "/admin/action", json={"family_key": "x"}, headers={"X-Admin-Token": "bad"}
        ).status_code
        == 401
    )


# ================================================================ 3. 健康检查 / 页面安全
def test_health_shape_and_no_secrets() -> None:
    client = TestClient(create_app(token=TOKEN, dsn=""))
    response = client.get("/admin/health")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "hotmeme-admin"
    assert data["auth_configured"] is True
    assert "schema_version" in data
    assert "llm_enabled" in data
    # 不返回 token / 连接串
    text = json.dumps(data)
    assert TOKEN not in text
    assert "postgresql://" not in text
    assert "password" not in text.lower()


def test_static_page_has_no_cdn_and_escapes() -> None:
    page = (REPO_ROOT / "admin" / "static" / "index.html").read_text(encoding="utf-8")
    assert "审核 Admin" in page
    # 无 CDN / 外部资源
    assert "cdn." not in page.lower()
    assert 'src="http' not in page
    assert "link rel=\"stylesheet\" href=\"http" not in page
    # 数据渲染走 textContent，不用 innerHTML 拼数据
    assert "innerHTML" not in page
    assert "textContent" in page
    # 页面通过 JSON 接口取数
    assert "/admin/queue" in page
    assert "/admin/action" in page


def test_login_page_escapes_message() -> None:
    from admin.app import _login_page

    response = _login_page(message="<script>alert(1)</script>")
    assert response.status_code == 401
    assert "<script>alert(1)</script>" not in response.body.decode("utf-8")
    assert "&lt;script&gt;" in response.body.decode("utf-8")


# ================================================================ 4. 参数校验（无库即可 400）
def test_action_validation_errors() -> None:
    client = TestClient(create_app(token=TOKEN, dsn=""))
    cases = [
        ({}, "family_key_required"),
        ({"family_key": "梗"}, "invalid_action"),
        (
            {"family_key": "梗", "action": "approve"},
            "operator_required",
        ),
        (
            {"family_key": "梗", "action": "reject", "operator": "op"},
            "reason_required",
        ),
        (
            {"family_key": "梗", "action": "merge", "operator": "op", "reason": "合并"},
            "merge_into_required",
        ),
        (
            {
                "family_key": "梗",
                "action": "approve",
                "operator": "op",
                "round_id": "abc",
            },
            "invalid_round_id",
        ),
    ]
    for payload, error in cases:
        response = client.post("/admin/action", json=payload, headers=HEADERS)
        assert response.status_code == 400, payload
        assert response.json()["error"] == error


def test_action_rejects_invalid_json() -> None:
    client = TestClient(create_app(token=TOKEN, dsn=""))
    response = client.post(
        "/admin/action",
        content=b"not-json",
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


def test_queue_503_when_db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(create_app(token=TOKEN, dsn=""))
    response = client.get("/admin/queue", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"] == "db_unavailable"


# ================================================================ 5. 启动拒绝
def test_main_refuses_without_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HOTMEME_ADMIN_TOKEN", raising=False)
    assert admin_app.main([]) == 2
    captured = capsys.readouterr()
    assert "拒绝启动" in captured.err


# ================================================================ 6. Postgres 集成（事务回滚）
class _Rollback(Exception):
    """用回滚结束测试事务（0 残留）。"""


@pytest.fixture()
def db():  # noqa: ANN001
    if not DB_URL:
        pytest.skip("需要 TEST_DATABASE_URL")
    from meme_info import state

    if MIGRATE_URL:  # 迁移需要 migrator 角色；无 migrator DSN 时假设 schema 已就绪
        state.migrate(MIGRATE_URL)
    else:
        assert state.check(DB_URL)["ok"] is True
    return DB_URL


def _seed_round(conn) -> int:  # noqa: ANN001
    row = conn.execute(
        "INSERT INTO round (started_at, window_start, window_end, status) "
        "VALUES (now(), now() - interval '15 minutes', now(), 'ok') RETURNING id"
    ).fetchone()
    return int(row[0])


def _insert_pool1(  # noqa: PLR0913
    conn,  # noqa: ANN001
    key: str,
    *,
    score: float = 60.0,
    count_now: int = 3,
    round_id: int | None = None,
    canonical: str | None = None,
    aliases: list[str] | None = None,
    evidence: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO pool1 (family_key, canonical, aliases_json, status, score, "
        "count_prev, count_now, zero_streak, audit_state, audit_hash, "
        "audit_adjustment, meta_only, explainer_only, first_seen_round, "
        "last_seen_round, evidence_json) "
        "VALUES (%s, %s, %s::jsonb, 'normal', %s, 0, %s, 0, 'none', '', 0, "
        "false, false, %s, %s, %s::jsonb)",
        (
            key,
            canonical or key,
            json.dumps(aliases or [], ensure_ascii=False),
            float(score),
            int(count_now),
            round_id,
            round_id,
            json.dumps(evidence or _evidence(), ensure_ascii=False),
        ),
    )


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _counts(conn) -> dict[str, int]:  # noqa: ANN001
    return {
        table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in ("pool1", "pool2", "audit_event", "pool_transition", "round")
    }


@requires_db
def test_admin_approve_writes_db_and_rolls_back(db: str) -> None:
    from meme_info import state

    key = _unique("admin梗")
    before: dict[str, int] = {}
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            before = _counts(conn)
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=60.0, count_now=4, round_id=round_id)
            client = TestClient(create_app(token=TOKEN, dsn=db, connection=conn))
            queue = client.get("/admin/queue", headers=HEADERS)
            assert queue.status_code == 200
            items = queue.json()["queue"]
            assert [item for item in items if item["family_key"] == key]
            evidence_hash = next(
                item["evidence_hash"] for item in items if item["family_key"] == key
            )
            response = client.post(
                "/admin/action",
                headers=HEADERS,
                json={
                    "family_key": key,
                    "action": "approve",
                    "operator": "tester",
                    "reason": "确认是热梗",
                    "evidence_hash": evidence_hash,
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert result["score_after"] == 75.0
            assert result["audit_state"] == "human_pass"
            assert result["pool2_after"] is True
            row = conn.execute(
                "SELECT audit_state, audit_adjustment, score FROM pool1 "
                "WHERE family_key = %s",
                (key,),
            ).fetchone()
            assert row[0] == "human_pass" and float(row[1]) == 15.0
            assert float(row[2]) == 75.0
            assert (
                conn.execute(
                    "SELECT count(*) FROM audit_event WHERE family_key = %s", (key,)
                ).fetchone()[0]
                == 1
            )
            raise _Rollback

    with state.connection(db) as conn:
        assert _counts(conn) == before, "Admin 写库必须随测试事务回滚"
        assert (
            conn.execute(
                "SELECT count(*) FROM pool1 WHERE family_key = %s", (key,)
            ).fetchone()[0]
            == 0
        )


@requires_db
def test_admin_reject_requires_reason_and_writes(db: str) -> None:
    from meme_info import state

    key = _unique("admin否决")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=80.0, count_now=5, round_id=round_id)
            client = TestClient(create_app(token=TOKEN, dsn=db, connection=conn))
            no_reason = client.post(
                "/admin/action",
                headers=HEADERS,
                json={
                    "family_key": key,
                    "action": "reject",
                    "operator": "tester",
                },
            )
            assert no_reason.status_code == 400
            assert no_reason.json()["error"] == "reason_required"
            response = client.post(
                "/admin/action",
                headers=HEADERS,
                json={
                    "family_key": key,
                    "action": "reject",
                    "operator": "tester",
                    "reason": "不是梗",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert result["audit_state"] == "human_fail"
            assert result["score_after"] == 55.0
            event = conn.execute(
                "SELECT kind, verdict, reason, operator FROM audit_event "
                "WHERE family_key = %s",
                (key,),
            ).fetchone()
            assert tuple(event) == ("human", "fail", "不是梗", "tester")
            raise _Rollback


@requires_db
def test_admin_merge_writes_db(db: str) -> None:
    from meme_info import state

    source = _unique("admin源")
    target = _unique("admin目标")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(
                conn, source, score=50.0, count_now=2, round_id=round_id,
                aliases=["源别名"],
            )
            _insert_pool1(
                conn, target, score=60.0, count_now=2, round_id=round_id,
                aliases=["目标别名"],
            )
            client = TestClient(create_app(token=TOKEN, dsn=db, connection=conn))
            response = client.post(
                "/admin/action",
                headers=HEADERS,
                json={
                    "family_key": source,
                    "action": "merge",
                    "operator": "tester",
                    "reason": "同一梗",
                    "merge_into": target,
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert result["merged_into"] == target
            source_row = conn.execute(
                "SELECT status, audit_state FROM pool1 WHERE family_key = %s",
                (source,),
            ).fetchone()
            assert tuple(source_row) == ("out", "human_merge")
            target_row = conn.execute(
                "SELECT aliases_json FROM pool1 WHERE family_key = %s", (target,)
            ).fetchone()
            aliases = target_row[0]
            if isinstance(aliases, (str, bytes)):
                aliases = json.loads(aliases)
            assert "源别名" in aliases
            raise _Rollback


@requires_db
def test_admin_stale_evidence_returns_409(db: str) -> None:
    from meme_info import state

    key = _unique("admin过期")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=60.0, round_id=round_id)
            client = TestClient(create_app(token=TOKEN, dsn=db, connection=conn))
            response = client.post(
                "/admin/action",
                headers=HEADERS,
                json={
                    "family_key": key,
                    "action": "approve",
                    "operator": "tester",
                    "evidence_hash": "deadbeef" * 4,
                },
            )
            assert response.status_code == 409
            assert response.json()["error"] == "stale_evidence"
            assert (
                conn.execute(
                    "SELECT count(*) FROM audit_event WHERE family_key = %s", (key,)
                ).fetchone()[0]
                == 0
            )
            raise _Rollback


@requires_db
def test_admin_queue_excludes_human_reviewed(db: str) -> None:
    from meme_info import state

    key = _unique("admin队列")
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(conn, key, score=62.0, count_now=2, round_id=round_id)
            client = TestClient(create_app(token=TOKEN, dsn=db, connection=conn))
            first = client.get("/admin/queue", headers=HEADERS)
            item = next(
                item
                for item in first.json()["queue"]
                if item["family_key"] == key
            )
            approved = client.post(
                "/admin/action",
                headers=HEADERS,
                json={
                    "family_key": key,
                    "action": "approve",
                    "operator": "tester",
                    "evidence_hash": item["evidence_hash"],
                },
            )
            assert approved.status_code == 200
            second = client.get("/admin/queue", headers=HEADERS)
            assert all(
                item["family_key"] != key for item in second.json()["queue"]
            )
            raise _Rollback


@requires_db
def test_admin_html_output_escaped_in_queue_json(db: str) -> None:
    """恶意 canonical 进入 JSON 时按 JSON 编码返回；页面用 textContent 渲染。"""
    from meme_info import state

    key = _unique("xss")
    canonical = "<script>alert('x')</script>"
    with pytest.raises(_Rollback):
        with state.transaction(db) as conn:
            round_id = _seed_round(conn)
            _insert_pool1(
                conn, key, canonical=canonical, score=62.0, round_id=round_id
            )
            client = TestClient(create_app(token=TOKEN, dsn=db, connection=conn))
            response = client.get("/admin/queue", headers=HEADERS)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/json")
            data = response.json()
            match = [item for item in data["queue"] if item["family_key"] == key]
            assert match and match[0]["canonical"] == canonical
            raise _Rollback
