import httpx

from app.api import providers as providers_api
from app.config import Settings, get_settings


def _gateway_settings() -> Settings:
    return Settings(
        _env_file=None,
        GATEWAY_API_KEY="test-gateway-key",
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="backend-key",
    )


def _insecure_remote_gateway_settings() -> Settings:
    # Settings already rejects this value; bypass validation here to verify the
    # proxy keeps the same defense at the final admin-key forwarding boundary.
    return _gateway_settings().model_copy(
        update={"model_gateway_base_url": "http://gateway.example/v1"}
    )


def _control_snapshot() -> dict:
    return {
        "revision": "a" * 64,
        "admin_required": True,
        "connections": [
            {
                "id": "official",
                "channel_operator": "official-vendor",
                "base_url": "https://official.example/v1",
                "adapter": "generic",
                "usage_scope": "backend_allowed",
                "enabled": True,
                "configured": True,
            }
        ],
        "deployments": [
            {
                "id": "chat-primary",
                "connection": "official",
                "upstream_model": "author/chat-v1",
                "model_author": "author",
                "model_family": "chat",
                "kind": "chat",
                "capabilities": {"tools": True},
                "dimensions": None,
                "embedding_space": "",
                "enabled": True,
            }
        ],
        "routes": [
            {
                "id": "memory.chat",
                "kind": "chat",
                "targets": ["chat-primary"],
                "required_capabilities": ["tools"],
                "max_attempts": 3,
                "enabled": True,
            }
        ],
    }


def test_direct_provider_status_remains_read_only(client, auth_headers) -> None:
    response = client.get("/providers/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["runtime"]["chat_source"] == "legacy_direct"
    assert response.json()["control"] is None


def test_model_gateway_status_uses_backend_key_and_never_returns_it(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    calls: list[dict] = []

    async def fake_request(**kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json=_control_snapshot())

    client.app.dependency_overrides[get_settings] = _gateway_settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)

    response = client.get("/providers/status", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"]["chat_source"] == "model_gateway"
    assert payload["providers"][0]["configured"] is True
    assert payload["routes"][0]["targets"][0]["model"] == "author/chat-v1"
    assert "backend-key" not in response.text
    assert calls[0]["api_key"] == "backend-key"
    assert calls[0]["path"] == "/admin/configuration"


def test_provider_writes_require_and_forward_only_the_admin_key(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    calls: list[dict] = []

    async def fake_request(**kwargs):
        calls.append(kwargs)
        return httpx.Response(
            200,
            json={
                "valid": True,
                "revision": "a" * 64,
                "changed_routes": ["memory.chat"],
                "warnings": [],
            },
        )

    client.app.dependency_overrides[get_settings] = _gateway_settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)
    body = {
        "revision": "a" * 64,
        "routes": [
            {"id": "memory.chat", "targets": ["chat-primary"], "enabled": True}
        ],
    }

    missing = client.post("/providers/routes/validate", headers=auth_headers, json=body)
    assert missing.status_code == 401
    assert calls == []

    accepted = client.post(
        "/providers/routes/validate",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json=body,
    )
    assert accepted.status_code == 200
    assert calls[0]["api_key"] == "admin-key"
    assert calls[0]["api_key"] != _gateway_settings().model_gateway_api_key


def test_model_gateway_control_error_is_normalized_for_the_web_client(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    async def fake_request(**kwargs):
        return httpx.Response(
            409,
            json={
                "error": {
                    "message": "配置已经被其他操作修改；请刷新页面后重新调整",
                    "type": "model_gateway_config_stale",
                }
            },
        )

    client.app.dependency_overrides[get_settings] = _gateway_settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)

    response = client.put(
        "/providers/routes",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json={"revision": "a" * 64, "routes": []},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "配置已经被其他操作修改；请刷新页面后重新调整",
        "code": "model_gateway_config_stale",
    }


def test_provider_writes_refuse_plain_http_to_a_remote_gateway(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    async def unexpected_request(**kwargs):
        raise AssertionError("admin key must not be forwarded over remote HTTP")

    client.app.dependency_overrides[get_settings] = _insecure_remote_gateway_settings
    monkeypatch.setattr(
        providers_api,
        "_model_gateway_control_request",
        unexpected_request,
    )

    response = client.post(
        "/providers/routes/validate",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json={"revision": "a" * 64, "routes": []},
    )

    assert response.status_code == 409
    assert "HTTPS" in response.json()["detail"]
