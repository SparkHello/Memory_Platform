"""一次性 console 登录 code 端点测试。

覆盖：mint 的 console role 限定、exchange 单次使用、过期、统一 401、
非本机来源拒绝、docker 网桥来源 + localhost 目标放行、速率限制，以及
code/token 明文不落盘、不进日志。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import hashlib
import logging
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth import console_login
from app.auth.tokens import AuthStoreError, AuthTokenStore
from app.config import get_settings
from app.main import create_app


_EXCHANGE_FAILURE = {"detail": "登录 code 无效或已过期"}


def _auth_store() -> AuthTokenStore:
    return AuthTokenStore(get_settings().auth_database_path)


def _auth_database_path() -> Path:
    return Path(get_settings().auth_database_path)


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "http://localhost:2026",
    client: tuple[str, int] = ("127.0.0.1", 43210),
) -> TestClient:
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    get_settings.cache_clear()
    # MCP session manager 不允许重复启动，每个 client 用全新应用实例。
    return TestClient(create_app(), base_url=base_url, client=client)


@pytest.fixture
def local_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _build_client(monkeypatch) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_pending_plaintexts() -> Iterator[None]:
    console_login._clear_pending_tokens()
    yield
    console_login._clear_pending_tokens()


def _console_headers(user_id: str = "default") -> dict[str, str]:
    created = _auth_store().create_token(
        name="Test console",
        user_id=user_id,
        role="console",
    )
    return {"Authorization": f"Bearer {created.token}"}


def _mint(local_client: TestClient, headers: dict[str, str]) -> dict:
    response = local_client.post("/auth/console-login-code", headers=headers)
    assert response.status_code == 201
    return response.json()


def _expire_code(code: str) -> None:
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    past = (datetime.now(UTC) - timedelta(seconds=61)).isoformat(timespec="seconds")
    with sqlite3.connect(_auth_database_path()) as connection:
        connection.execute(
            "UPDATE console_login_codes SET expires_at = ? WHERE code_hash = ?",
            (past, code_hash),
        )
        connection.commit()


def _token_record(token_id: str):
    return next(
        record
        for record in _auth_store().list_tokens(user_id="default")
        if record.token_id == token_id
    )


def test_mint_requires_console_role(local_client: TestClient) -> None:
    store = _auth_store()
    chat = store.create_token(name="chat", user_id="default", role="chat")
    mcp = store.create_token(name="mcp", user_id="default", role="mcp")
    before = len(store.list_tokens(user_id="default"))

    assert local_client.post("/auth/console-login-code").status_code == 401
    for token in (chat.token, mcp.token):
        response = local_client.post(
            "/auth/console-login-code",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    # 被拒的请求不得留下换发的 console token 或 code 记录。
    assert len(store.list_tokens(user_id="default")) == before
    with sqlite3.connect(_auth_database_path()) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM console_login_codes"
        ).fetchone()[0]
    assert count == 0


def test_exchange_delivers_console_token_exactly_once(
    local_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = _console_headers()
    with caplog.at_level(logging.DEBUG):
        minted = _mint(local_client, headers)
        code = minted["code"]
        assert minted["expires_in_seconds"] == 300

        exchanged = local_client.post(
            "/auth/console-login-exchange",
            json={"code": code},
        )
        assert exchanged.status_code == 200
        assert exchanged.headers["cache-control"] == "no-store"
        delivered = exchanged.json()
        token = delivered["token"]
        assert delivered["token_id"] == minted["token_id"]
        assert delivered["user_id"] == "default"
        # HTTP 签发的 code 永远不携带管理密钥。
        assert delivered["model_admin_key"] is None

        # 交付的明文是真实可用的 console 凭证。
        record = _auth_store().authenticate(token)
        assert record is not None
        assert record.role == "console"
        assert record.user_id == "default"
        listed = local_client.get(
            "/auth/tokens",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert listed.status_code == 200

        # 单次使用：重放立即失效。
        replay = local_client.post(
            "/auth/console-login-exchange",
            json={"code": code},
        )
        assert replay.status_code == 401
        assert replay.json() == _EXCHANGE_FAILURE

    assert code not in caplog.text
    assert token not in caplog.text


def test_expired_and_wrong_code_fail_with_identical_401(
    local_client: TestClient,
) -> None:
    minted = _mint(local_client, _console_headers())
    code = minted["code"]

    wrong = local_client.post(
        "/auth/console-login-exchange",
        json={"code": "mgc_definitely-not-issued"},
    )
    assert wrong.status_code == 401
    assert wrong.json() == _EXCHANGE_FAILURE

    _expire_code(code)
    expired = local_client.post(
        "/auth/console-login-exchange",
        json={"code": code},
    )
    assert expired.status_code == 401
    assert expired.json() == wrong.json()

    # 过期即作废：换发的 console token 一并吊销，不留下无人持有的凭证。
    assert _token_record(minted["token_id"]).revoked_at is not None


def test_exchange_fails_closed_when_process_lost_pending_plaintext(
    local_client: TestClient,
) -> None:
    minted = _mint(local_client, _console_headers())
    # 模拟进程重启：内存中的 token 明文丢失。
    console_login._clear_pending_tokens()

    response = local_client.post(
        "/auth/console-login-exchange",
        json={"code": minted["code"]},
    )
    assert response.status_code == 401
    assert response.json() == _EXCHANGE_FAILURE
    assert _token_record(minted["token_id"]).revoked_at is not None


def test_exchange_rejects_non_local_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 公网来源：直接拒绝。
    with _build_client(
        monkeypatch,
        client=("203.0.113.10", 9999),
    ) as public_source:
        response = public_source.post(
            "/auth/console-login-exchange",
            json={"code": "mgc_whatever"},
        )
        assert response.status_code == 403

    # 私网来源但请求目标不是 localhost（LAN 访问场景）：同样拒绝。
    with _build_client(
        monkeypatch,
        base_url="http://192.168.1.20:2026",
        client=("172.17.0.1", 9999),
    ) as lan_target:
        response = lan_target.post(
            "/auth/console-login-exchange",
            json={"code": "mgc_whatever"},
        )
        assert response.status_code == 403
    get_settings.cache_clear()


def test_docker_bridge_source_with_localhost_target_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # docker 发布端口下，宿主机浏览器的来源是网桥地址而非回环。
    with _build_client(
        monkeypatch,
        client=("172.17.0.1", 52000),
    ) as docker_client:
        minted = _mint(docker_client, _console_headers())
        exchanged = docker_client.post(
            "/auth/console-login-exchange",
            json={"code": minted["code"]},
        )
        assert exchanged.status_code == 200
        assert exchanged.json()["token"].startswith("mgw_")
    get_settings.cache_clear()


def test_exchange_uses_existing_console_rate_limit(
    local_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.auth.middleware._ROLE_LIMITS",
        {"chat": (60, 4), "mcp": (120, 8), "console": (2, 8)},
    )
    minted = _mint(local_client, _console_headers())

    first = local_client.post(
        "/auth/console-login-exchange",
        json={"code": "mgc_wrong"},
    )
    second = local_client.post(
        "/auth/console-login-exchange",
        json={"code": minted["code"]},
    )
    third = local_client.post(
        "/auth/console-login-exchange",
        json={"code": minted["code"]},
    )
    assert first.status_code == 401
    assert second.status_code == 200
    assert third.status_code == 429


def test_code_and_token_plaintext_are_never_stored(
    local_client: TestClient,
) -> None:
    minted = _mint(local_client, _console_headers())
    code = minted["code"]
    token = local_client.post(
        "/auth/console-login-exchange",
        json={"code": code},
    ).json()["token"]

    raw = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{_auth_database_path()}{suffix}")
        if candidate.exists():
            raw += candidate.read_bytes()
    assert code.encode() not in raw
    assert token.encode() not in raw

    with sqlite3.connect(_auth_database_path()) as connection:
        rows = connection.execute(
            "SELECT code_hash, user_id, console_token_id, used_at "
            "FROM console_login_codes"
        ).fetchall()
    assert len(rows) == 1
    code_hash, user_id, console_token_id, used_at = rows[0]
    assert code_hash == hashlib.sha256(code.encode("utf-8")).hexdigest()
    assert (user_id, console_token_id) == ("default", minted["token_id"])
    assert used_at is not None


def _active_console_token_ids() -> set[str]:
    return {
        record.token_id
        for record in _auth_store().list_tokens(user_id="default")
        if record.role == "console" and record.revoked_at is None
    }


def test_mint_for_token_reuses_existing_token_and_delivers_admin_key(
    local_client: TestClient,
) -> None:
    """安卓 App 的一键登录：交付既有首次登录密钥与管理密钥，不新增 token。"""
    created = _auth_store().create_token(
        name="first-console", user_id="default", role="console"
    )
    before = _active_console_token_ids()
    store = console_login.ConsoleLoginCodeStore(_auth_database_path())
    minted = store.mint_for_token(created.token, admin_key="synthetic-admin-key")
    assert minted.token_id == created.record.token_id
    assert _active_console_token_ids() == before

    exchanged = local_client.post(
        "/auth/console-login-exchange", json={"code": minted.code}
    )
    assert exchanged.status_code == 200
    delivered = exchanged.json()
    assert delivered["token"] == created.token
    assert delivered["token_id"] == created.record.token_id
    assert delivered["model_admin_key"] == "synthetic-admin-key"

    replay = local_client.post(
        "/auth/console-login-exchange", json={"code": minted.code}
    )
    assert replay.status_code == 401
    assert replay.json() == _EXCHANGE_FAILURE
    # 既有 token 依然可用。
    assert _auth_store().authenticate(created.token) is not None


def test_mint_for_token_expiry_never_revokes_existing_token(
    local_client: TestClient,
) -> None:
    created = _auth_store().create_token(
        name="first-console", user_id="default", role="console"
    )
    store = console_login.ConsoleLoginCodeStore(_auth_database_path())
    minted = store.mint_for_token(created.token)
    _expire_code(minted.code)

    expired = local_client.post(
        "/auth/console-login-exchange", json={"code": minted.code}
    )
    assert expired.status_code == 401
    assert _token_record(created.record.token_id).revoked_at is None

    # 下一次签发时的过期清理同样不能碰这枚既有 token。
    again = store.mint_for_token(created.token)
    _expire_code(again.code)
    store.mint_for_token(created.token)
    assert _token_record(created.record.token_id).revoked_at is None

    # 进程重启丢失明文：既有 token 也不能被吊销。
    lost = store.mint_for_token(created.token)
    console_login._clear_pending_tokens()
    response = local_client.post(
        "/auth/console-login-exchange", json={"code": lost.code}
    )
    assert response.status_code == 401
    assert _token_record(created.record.token_id).revoked_at is None


def test_mint_for_token_rejects_non_console_or_revoked(
    local_client: TestClient,
) -> None:
    store = console_login.ConsoleLoginCodeStore(_auth_database_path())
    chat = _auth_store().create_token(name="chat", user_id="default", role="chat")
    with pytest.raises(AuthStoreError):
        store.mint_for_token(chat.token)
    console = _auth_store().create_token(
        name="console", user_id="default", role="console"
    )
    _auth_store().revoke_token(console.record.token_id)
    with pytest.raises(AuthStoreError):
        store.mint_for_token(console.token)
    with pytest.raises(AuthStoreError):
        store.mint_for_token("not-a-token")


def test_embedded_deployment_profile_is_reported_by_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _build_client(monkeypatch) as plain:
        assert plain.get("/health").json() == {"status": "ok"}
    monkeypatch.setenv("MEMGW_DEPLOYMENT_PROFILE", "embedded")
    get_settings.cache_clear()
    with _build_client(monkeypatch) as embedded:
        assert embedded.get("/health").json() == {"status": "ok", "deployment": "embedded"}
    get_settings.cache_clear()
