import json

from app.auth.tokens import AuthTokenStore
from app.config import get_settings


def _store() -> AuthTokenStore:
    return AuthTokenStore(get_settings().auth_database_path)


def test_console_rest_creates_only_device_tokens_and_never_repeats_secret(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/auth/tokens",
        headers=auth_headers,
        json={"name": "Living room Chatbox", "role": "chat"},
    )

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert created.headers["pragma"] == "no-cache"
    payload = created.json()
    raw_token = payload["token"]
    assert raw_token.startswith(f'mgw_{payload["record"]["token_id"]}_')
    assert payload["record"]["name"] == "Living room Chatbox"
    assert payload["record"]["role"] == "chat"
    assert payload["record"]["user_id"] == "default"
    assert "token" not in payload["record"]
    assert "token_hash" not in payload["record"]

    listed = client.get("/auth/tokens", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    listed_text = json.dumps(listed.json(), ensure_ascii=False)
    assert raw_token not in listed_text
    assert "token_hash" not in listed_text
    assert "test-gateway-key" not in listed_text
    assert listed.json()["legacy_key_enabled"] is True
    assert listed.json()["authenticated_with_legacy_key"] is True
    assert listed.json()["allowed_create_roles"] == ["chat", "mcp"]

    console = client.post(
        "/auth/tokens",
        headers=auth_headers,
        json={"name": "Forbidden admin", "role": "console"},
    )
    assert console.status_code == 422
    assert len(_store().list_tokens(user_id="default")) == 1

    injected_user = client.post(
        "/auth/tokens",
        headers=auth_headers,
        json={"name": "Wrong user", "role": "mcp", "user_id": "other"},
    )
    assert injected_user.status_code == 422
    assert len(_store().list_tokens(user_id="default")) == 1
    assert _store().list_tokens(user_id="other") == []


def test_token_management_requires_console_scope_and_is_user_isolated(client) -> None:
    store = _store()
    alice_console = store.create_token(
        name="Alice browser",
        user_id="alice",
        role="console",
    )
    alice_chat = store.create_token(
        name="Alice phone",
        user_id="alice",
        role="chat",
    )
    bob_mcp = store.create_token(
        name="Bob MCP",
        user_id="bob",
        role="mcp",
    )
    alice_headers = {"Authorization": f"Bearer {alice_console.token}"}

    listed = client.get("/auth/tokens", headers=alice_headers)
    assert listed.status_code == 200
    assert listed.json()["authenticated_with_legacy_key"] is False
    assert listed.json()["current_user_id"] == "alice"
    assert {item["token_id"] for item in listed.json()["data"]} == {
        alice_console.record.token_id,
        alice_chat.record.token_id,
    }
    assert all(item["user_id"] == "alice" for item in listed.json()["data"])

    chat_headers = {"Authorization": f"Bearer {alice_chat.token}"}
    assert client.get("/auth/tokens", headers=chat_headers).status_code == 403
    assert client.post(
        "/auth/tokens",
        headers=chat_headers,
        json={"name": "Escalate", "role": "mcp"},
    ).status_code == 403
    assert client.delete(
        f"/auth/tokens/{alice_console.record.token_id}",
        headers=chat_headers,
    ).status_code == 403

    cross_user = client.delete(
        f"/auth/tokens/{bob_mcp.record.token_id}",
        headers=alice_headers,
    )
    assert cross_user.status_code == 404
    assert store.authenticate(bob_mcp.token) is not None

    header_override = client.get(
        "/auth/tokens",
        headers={**alice_headers, "X-User-Id": "bob"},
    )
    assert header_override.status_code == 403


def test_individual_revocation_is_idempotent_and_secret_never_reappears(client) -> None:
    store = _store()
    console = store.create_token(
        name="Console",
        user_id="default",
        role="console",
    )
    chat = store.create_token(
        name="Phone chat",
        user_id="default",
        role="chat",
    )
    console_headers = {"Authorization": f"Bearer {console.token}"}
    chat_headers = {"Authorization": f"Bearer {chat.token}"}

    assert client.get("/v1/models", headers=chat_headers).status_code == 200
    revoked = client.delete(
        f"/auth/tokens/{chat.record.token_id}",
        headers=console_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert revoked.json()["already_revoked"] is False
    assert revoked.json()["record"]["revoked_at"] is not None
    assert chat.token not in revoked.text
    assert client.get("/v1/models", headers=chat_headers).status_code == 401

    repeated = client.delete(
        f"/auth/tokens/{chat.record.token_id}",
        headers=console_headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["already_revoked"] is True
    assert chat.token not in repeated.text

    listed = client.get("/auth/tokens", headers=console_headers)
    revoked_record = next(
        item
        for item in listed.json()["data"]
        if item["token_id"] == chat.record.token_id
    )
    assert revoked_record["revoked_at"] is not None
    assert chat.token not in listed.text


def test_auth_token_rest_is_protected_before_router_dispatch(client) -> None:
    assert client.get("/auth/tokens").status_code == 401
    assert client.post(
        "/auth/tokens",
        json={"name": "unauthorized", "role": "chat"},
    ).status_code == 401
    assert client.delete("/auth/tokens/0123456789abcdef").status_code == 401


def test_last_console_token_revocation_is_a_stable_conflict(client) -> None:
    store = _store()
    console = store.create_token(
        name="Only console",
        user_id="alice",
        role="console",
    )
    headers = {"Authorization": f"Bearer {console.token}"}

    listed = client.get("/auth/tokens", headers=headers)
    assert listed.status_code == 200
    record = next(
        item
        for item in listed.json()["data"]
        if item["token_id"] == console.record.token_id
    )
    assert record["is_current"] is True
    assert record["can_revoke"] is False
    assert record["revoke_block_reason"] == "last_active_console_token"

    blocked = client.delete(
        f"/auth/tokens/{console.record.token_id}",
        headers=headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "last_active_console_token"
    assert store.authenticate(console.token) is not None


def test_current_console_can_be_revoked_only_when_another_console_is_active(
    client,
) -> None:
    store = _store()
    current = store.create_token(
        name="Current browser",
        user_id="alice",
        role="console",
    )
    backup = store.create_token(
        name="Backup browser",
        user_id="alice",
        role="console",
    )
    current_headers = {"Authorization": f"Bearer {current.token}"}
    backup_headers = {"Authorization": f"Bearer {backup.token}"}

    revoked = client.delete(
        f"/auth/tokens/{current.record.token_id}",
        headers=current_headers,
    )
    assert revoked.status_code == 200
    assert store.authenticate(current.token) is None

    blocked = client.delete(
        f"/auth/tokens/{backup.record.token_id}",
        headers=backup_headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "last_active_console_token"
