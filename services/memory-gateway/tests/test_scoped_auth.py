from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.auth.middleware import EarlyAuthMiddleware, ProcessAccessGate, _is_irreversible
from app.auth.tokens import AuthTokenStore
from app.config import Settings, get_settings


MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
MCP_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _store() -> AuthTokenStore:
    return AuthTokenStore(get_settings().auth_database_path)


def test_role_scopes_are_enforced_before_router_dispatch(client) -> None:
    store = _store()
    chat = store.create_token(name="Phone chat", user_id="alice", role="chat").token
    mcp = store.create_token(name="Laptop MCP", user_id="alice", role="mcp").token
    console = store.create_token(
        name="Admin browser",
        user_id="alice",
        role="console",
    ).token

    chat_headers = {"Authorization": f"Bearer {chat}"}
    mcp_headers = {"Authorization": f"Bearer {mcp}", **MCP_HEADERS}
    console_headers = {"Authorization": f"Bearer {console}"}

    assert client.get("/v1/models", headers=chat_headers).status_code == 200
    assert client.get("/memories", headers=chat_headers).status_code == 403
    assert client.post("/mcp", headers=mcp_headers, json=MCP_LIST).status_code == 200
    assert client.get("/v1/models", headers=mcp_headers).status_code == 403
    assert client.get("/memories", headers=console_headers).status_code == 200
    assert client.post(
        "/mcp", headers={**console_headers, **MCP_HEADERS}, json=MCP_LIST
    ).status_code == 403


def test_scoped_token_is_always_bound_to_its_stored_user(client) -> None:
    token = _store().create_token(
        name="Alice console",
        user_id="alice",
        role="console",
    ).token
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/memories", headers=headers).status_code == 200
    assert client.get(
        "/memories",
        headers={**headers, "X-User-Id": "alice"},
    ).status_code == 200
    rejected = client.get(
        "/memories",
        headers={**headers, "X-User-Id": "bob"},
    )
    assert rejected.status_code == 403


def test_legacy_all_scope_can_be_disabled_without_disabling_scoped_tokens(
    client,
    monkeypatch,
) -> None:
    scoped = _store().create_token(
        name="New console",
        user_id="default",
        role="console",
    ).token
    legacy = {"Authorization": "Bearer test-gateway-key"}
    scoped_headers = {"Authorization": f"Bearer {scoped}"}

    assert client.get("/memories", headers=legacy).status_code == 200
    monkeypatch.setenv("GATEWAY_LEGACY_API_KEY_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert client.get("/memories", headers=legacy).status_code == 401
        assert client.get("/memories", headers=scoped_headers).status_code == 200
    finally:
        monkeypatch.setenv("GATEWAY_LEGACY_API_KEY_ENABLED", "true")
        get_settings.cache_clear()


def test_revocation_is_observed_by_the_next_request_and_last_used_is_recorded(
    client,
) -> None:
    store = _store()
    created = store.create_token(name="Revocable", user_id="default", role="chat")
    headers = {"Authorization": f"Bearer {created.token}"}

    assert client.get("/v1/models", headers=headers).status_code == 200
    assert store.list_tokens()[0].last_used_at is not None
    assert store.revoke_token(created.record.token_id)
    assert client.get("/v1/models", headers=headers).status_code == 401


@pytest.mark.anyio
async def test_unauthorized_chunked_body_is_not_consumed(tmp_path) -> None:
    received = 0
    downstream_called = False

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        nonlocal received
        received += 1
        return {
            "type": "http.request",
            "body": b"x" * (1024 * 1024),
            "more_body": True,
        }

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    settings = Settings(
        _env_file=None,
        DATABASE_PATH=str(tmp_path / "memory.db"),
        KNOWLEDGE_DATABASE_PATH=str(tmp_path / "knowledge.db"),
        AUTH_DATABASE_PATH=str(tmp_path / "auth.db"),
    )
    middleware = EarlyAuthMiddleware(downstream, settings_provider=lambda: settings)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"transfer-encoding", b"chunked")],
        },
        receive,
        send,
    )

    assert received == 0
    assert downstream_called is False
    assert sent[0]["status"] == 401


@pytest.mark.anyio
async def test_unicode_legacy_bearer_is_compared_as_utf8_and_never_raises(tmp_path) -> None:
    legacy_key = "中文手工访问密钥-足够长但不要求只能ASCII"
    settings = Settings(
        _env_file=None,
        GATEWAY_API_KEY=legacy_key,
        DATABASE_PATH=str(tmp_path / "memory.db"),
        KNOWLEDGE_DATABASE_PATH=str(tmp_path / "knowledge.db"),
        AUTH_DATABASE_PATH=str(tmp_path / "auth.db"),
    )

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def request(raw_authorization: bytes) -> list[dict]:
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await EarlyAuthMiddleware(
            downstream,
            settings_provider=lambda: settings,
        )(
            {
                "type": "http",
                "method": "GET",
                "path": "/memories",
                "headers": [(b"authorization", raw_authorization)],
            },
            receive,
            send,
        )
        return sent

    accepted = await request(f"Bearer {legacy_key}".encode("utf-8"))
    arbitrary = await request(b"Bearer \xff\xfe\xfd")

    assert accepted[0]["status"] == 200
    assert arbitrary[0]["status"] == 401


@pytest.mark.anyio
async def test_early_middleware_holds_chat_concurrency_slot_until_response_finishes(
    tmp_path,
) -> None:
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def downstream(scope, receive, send):
        nonlocal entered
        entered += 1
        if entered == 4:
            all_entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    settings = Settings(
        _env_file=None,
        GATEWAY_API_KEY="legacy-concurrency-key",
        DATABASE_PATH=str(tmp_path / "memory.db"),
        KNOWLEDGE_DATABASE_PATH=str(tmp_path / "knowledge.db"),
        AUTH_DATABASE_PATH=str(tmp_path / "auth.db"),
    )
    middleware = EarlyAuthMiddleware(downstream, settings_provider=lambda: settings)

    async def request() -> list[dict]:
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/models",
                "headers": [(b"authorization", b"Bearer legacy-concurrency-key")],
            },
            receive,
            send,
        )
        return sent

    tasks = [asyncio.create_task(request()) for _ in range(4)]
    await asyncio.wait_for(all_entered.wait(), timeout=1)
    fifth = await request()
    assert fifth[0]["status"] == 429
    assert dict(fifth[0]["headers"])[b"retry-after"] == b"1"
    release.set()
    responses = await asyncio.gather(*tasks)
    assert all(response[0]["status"] == 200 for response in responses)


def test_process_gate_enforces_chat_concurrency_and_role_rates() -> None:
    now = [100.0]
    gate = ProcessAccessGate(clock=lambda: now[0])

    for _ in range(4):
        assert gate.acquire(identity="phone", role="chat", irreversible=False)[0]
    admitted, retry_after, reason = gate.acquire(
        identity="phone", role="chat", irreversible=False
    )
    assert (admitted, retry_after, reason) == (False, 1, "concurrency")
    for _ in range(4):
        gate.release(identity="phone", role="chat")

    for _ in range(60 - 4):
        assert gate.acquire(identity="phone", role="chat", irreversible=False)[0]
        gate.release(identity="phone", role="chat")
    admitted, retry_after, reason = gate.acquire(
        identity="phone", role="chat", irreversible=False
    )
    assert admitted is False
    assert retry_after == 60
    assert reason == "rate"
    now[0] += 60.1
    assert gate.acquire(identity="phone", role="chat", irreversible=False)[0]


def test_process_gate_adds_console_irreversible_limit() -> None:
    gate = ProcessAccessGate(clock=lambda: 10.0)
    for _ in range(10):
        assert gate.acquire(
            identity="browser", role="console", irreversible=True
        )[0]
        gate.release(identity="browser", role="console")
    admitted, _, reason = gate.acquire(
        identity="browser", role="console", irreversible=True
    )
    assert admitted is False
    assert reason == "irreversible"
    # Ordinary console reads still use the independent 120/minute allowance.
    assert gate.acquire(identity="browser", role="console", irreversible=False)[0]


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/providers/channels/discover", False),
        ("POST", "/providers/channel-bundles/validate", False),
        ("POST", "/providers/channel-bundles/apply", True),
        ("PATCH", "/providers/connections/channel-a", True),
        ("PATCH", "/providers/deployments/model-a", True),
        ("DELETE", "/providers/pricing/price-a", True),
    ],
)
def test_provider_candidate_reads_do_not_consume_irreversible_budget(
    method: str,
    path: str,
    expected: bool,
) -> None:
    assert _is_irreversible(
        {"type": "http", "method": method, "path": path}
    ) is expected


def test_batch_purge_preview_is_read_only_but_commit_uses_irreversible_gate(client) -> None:
    token = _store().create_token(
        name="Purge console",
        user_id="default",
        role="console",
    ).token
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(11):
        assert client.post(
            "/memories/deleted/purge/preview",
            headers=headers,
            json={},
        ).status_code == 422
    for _ in range(10):
        assert client.post(
            "/memories/deleted/purge/commit",
            headers=headers,
            json={},
        ).status_code == 422
    limited = client.post(
        "/memories/deleted/purge/commit",
        headers=headers,
        json={},
    )
    assert limited.status_code == 429
    assert "不可逆管理操作" in limited.json()["detail"]


def test_signing_secret_must_be_independent_from_every_access_token() -> None:
    shared = "a-secure-value-that-is-longer-than-32-characters"
    with pytest.raises(ValidationError, match="必须与 GATEWAY_API_KEY 独立"):
        Settings(
            _env_file=None,
            GATEWAY_API_KEY=shared,
            GATEWAY_SIGNING_SECRET=shared,
        )
    with pytest.raises(ValidationError, match="不得使用 scoped access token"):
        Settings(
            _env_file=None,
            GATEWAY_SIGNING_SECRET="mgw_0123456789abcdef_abcdefghijklmnopqrstuvwxyzABCDEFGH",
        )
    unicode_shared = "独立签名密钥" * 8
    with pytest.raises(ValidationError, match="必须与 GATEWAY_API_KEY 独立"):
        Settings(
            _env_file=None,
            GATEWAY_API_KEY=unicode_shared,
            GATEWAY_SIGNING_SECRET=unicode_shared,
        )


def test_signing_secret_must_be_independent_from_model_backend_key() -> None:
    shared = "shared-model-and-signing-secret-0123456789"
    with pytest.raises(ValidationError, match="MODEL_GATEWAY_API_KEY 独立"):
        Settings(
            _env_file=None,
            GATEWAY_SIGNING_SECRET=shared,
            MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
            MODEL_GATEWAY_API_KEY=shared,
        )


def test_missing_signing_secret_disables_signed_feature_without_access_key_fallback(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GATEWAY_SIGNING_SECRET", "")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/knowledge/read",
            headers=auth_headers,
            json={"reference": "version:missing"},
        )
        assert response.status_code == 503
        assert "GATEWAY_SIGNING_SECRET" in response.json()["detail"]
    finally:
        monkeypatch.setenv(
            "GATEWAY_SIGNING_SECRET",
            "pytest-only-signing-secret-32-bytes-minimum",
        )
        get_settings.cache_clear()
