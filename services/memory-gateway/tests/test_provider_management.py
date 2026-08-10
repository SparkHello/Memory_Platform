import json

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


def _split_gateway_settings(*, allow_private_http: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        GATEWAY_API_KEY="test-gateway-key",
        MODEL_GATEWAY_BASE_URL="http://model-gateway:2030/v1",
        MODEL_GATEWAY_API_KEY="backend-key",
        MODEL_GATEWAY_ALLOW_PRIVATE_HTTP=allow_private_http,
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


def test_provider_status_defaults_to_model_gateway_without_control_plane(
    client, auth_headers, monkeypatch
) -> None:
    response = client.get("/providers/status", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"]["chat_source"] == "model_gateway"
    assert payload["runtime"]["model_gateway_enabled"] is True
    # Without a reachable control plane the setup path still asks for models.
    assert payload["setup"]["model_gateway_connected"] is True or payload["control"] is None


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
    assert payload["setup"]["chat_ready"] is False
    assert payload["setup"]["missing_chat_routes"] == [
        "memory.extract",
        "memory.compact",
        "memory.core",
        "memory.review",
        "knowledge.fast",
        "knowledge.pro",
    ]
    assert "backend-key" not in response.text
    assert calls[0]["api_key"] == "backend-key"
    assert calls[0]["path"] == "/admin/configuration"


def test_setup_is_ready_only_when_every_chat_route_is_usable(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    snapshot = _control_snapshot()
    snapshot["routes"] = [
        {
            "id": route_id,
            "kind": "chat",
            "targets": ["chat-primary"],
            "required_capabilities": [],
            "max_attempts": 3,
            "enabled": True,
        }
        for route_id in providers_api.REQUIRED_CHAT_ROUTES
    ]

    async def fake_request(**kwargs):
        return httpx.Response(200, json=snapshot)

    client.app.dependency_overrides[get_settings] = _gateway_settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)

    response = client.get("/providers/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup"] == {
        "state": "ready",
        "service_ready": True,
        "model_gateway_connected": True,
        "chat_ready": True,
        "required_chat_routes": list(providers_api.REQUIRED_CHAT_ROUTES),
        "usable_chat_routes": list(providers_api.REQUIRED_CHAT_ROUTES),
        "missing_chat_routes": [],
        "next_action": "connect_client",
    }


def test_setup_uses_resolved_custom_route_ids(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        GATEWAY_API_KEY="test-gateway-key",
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="backend-key",
        MODEL_GATEWAY_CHAT_MODEL="custom.chat",
        MODEL_GATEWAY_MEMORY_EXTRACT_MODEL="custom.extract",
        MODEL_GATEWAY_MEMORY_COMPACT_MODEL="custom.compact",
        MODEL_GATEWAY_MEMORY_CORE_MODEL="custom.core",
        MODEL_GATEWAY_MEMORY_REVIEW_MODEL="custom.review",
        MODEL_GATEWAY_KNOWLEDGE_FAST_MODEL="custom.knowledge.fast",
        MODEL_GATEWAY_KNOWLEDGE_PRO_MODEL="custom.knowledge.pro",
    )
    expected_routes = [
        "custom.chat",
        "custom.extract",
        "custom.compact",
        "custom.core",
        "custom.review",
        "custom.knowledge.fast",
        "custom.knowledge.pro",
    ]
    snapshot = _control_snapshot()
    snapshot["routes"] = [
        {
            "id": route_id,
            "kind": "chat",
            "targets": ["chat-primary"],
            "required_capabilities": [],
            "max_attempts": 3,
            "enabled": True,
        }
        for route_id in expected_routes
    ]

    async def fake_request(**kwargs):
        return httpx.Response(200, json=snapshot)

    client.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)

    response = client.get("/providers/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["setup"]["state"] == "ready"
    assert response.json()["setup"]["required_chat_routes"] == expected_routes


def test_admin_key_can_be_checked_without_returning_configuration(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    async def fake_request(**kwargs):
        assert kwargs["path"] == "/admin/configuration"
        assert kwargs["api_key"] == "admin-key"
        return httpx.Response(200, json=_control_snapshot())

    client.app.dependency_overrides[get_settings] = _gateway_settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)

    response = client.post(
        "/providers/admin/check",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
    )

    assert response.status_code == 200
    assert response.json() == {"valid": True}
    assert "connections" not in response.text


def test_admin_can_request_full_redacted_configuration(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    async def fake_request(**kwargs):
        assert kwargs["path"] == "/admin/configuration"
        assert kwargs["api_key"] == "admin-key"
        return httpx.Response(200, json=_control_snapshot())

    client.app.dependency_overrides[get_settings] = _gateway_settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)

    response = client.get(
        "/providers/admin/configuration",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
    )

    assert response.status_code == 200
    assert response.json()["connections"][0]["id"] == "official"
    assert "admin-key" not in response.text


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


def test_split_service_hostname_requires_explicit_private_http_opt_in(
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
                "persisted": False,
                "revision": "a" * 64,
                "report": {"connections": []},
            },
        )

    unsafe = _split_gateway_settings().model_copy(
        update={"model_gateway_allow_private_http": False}
    )
    client.app.dependency_overrides[get_settings] = lambda: unsafe
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)
    body = {
        "revision": "a" * 64,
        "connection": {
            "channel_operator": "example",
            "base_url": "https://api.example/v1",
            "adapter": "generic",
        },
        "value": "candidate-key",
    }

    refused = client.post(
        "/providers/channels/discover",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json=body,
    )
    assert refused.status_code == 409
    assert calls == []

    client.app.dependency_overrides[get_settings] = _split_gateway_settings
    accepted = client.post(
        "/providers/channels/discover",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json=body,
    )
    assert accepted.status_code == 200
    assert calls[0]["path"] == "/admin/channels/discover"
    assert calls[0]["api_key"] == "admin-key"


def test_canonical_bundle_and_object_management_paths_are_fixed_target_proxies(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    calls: list[dict] = []

    async def fake_request(**kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json={"revision": "b" * 64})

    client.app.dependency_overrides[get_settings] = _gateway_settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)
    headers = {**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"}

    assert client.post(
        "/providers/channel-bundles/validate",
        headers=headers,
        json={"revision": "a" * 64},
    ).status_code == 200
    assert client.post(
        "/providers/channel-bundles/apply",
        headers=headers,
        json={"revision": "a" * 64},
    ).status_code == 200
    assert client.patch(
        "/providers/connections/a%20b",
        headers=headers,
        json={"revision": "a" * 64, "enabled": False},
    ).status_code == 200
    assert client.patch(
        "/providers/deployments/chat-primary",
        headers=headers,
        json={"revision": "a" * 64, "enabled": False},
    ).status_code == 200
    assert client.request(
        "DELETE",
        "/providers/pricing/payg",
        headers=headers,
        json={"revision": "a" * 64},
    ).status_code == 200

    assert [(call["method"], call["path"]) for call in calls] == [
        ("POST", "/admin/channel-bundles/validate"),
        ("POST", "/admin/channel-bundles/apply"),
        ("PATCH", "/admin/connections/a%20b"),
        ("PATCH", "/admin/deployments/chat-primary"),
        ("DELETE", "/admin/pricing/payg"),
    ]
    assert all(call["api_key"] == "admin-key" for call in calls)


def test_control_client_ignores_ambient_proxy_for_backend_and_admin_keys(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    client_options: list[dict] = []
    requests: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            client_options.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def request(self, method, url, **kwargs):
            requests.append({"method": method, "url": url, **kwargs})
            if url.endswith("/admin/configuration"):
                return httpx.Response(200, json=_control_snapshot())
            return httpx.Response(200, json={"valid": True, "persisted": False})

    monkeypatch.setenv("HTTP_PROXY", "http://ambient-proxy.invalid:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.invalid:9999")
    monkeypatch.setenv("ALL_PROXY", "http://ambient-proxy.invalid:9999")
    monkeypatch.setattr(providers_api.httpx, "AsyncClient", FakeAsyncClient)
    client.app.dependency_overrides[get_settings] = _gateway_settings

    status = client.get("/providers/status", headers=auth_headers)
    discovered = client.post(
        "/providers/channels/discover",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json={"revision": "a" * 64, "connection": "official", "value": "candidate"},
    )

    assert status.status_code == 200
    assert discovered.status_code == 200
    assert len(client_options) == 2
    assert all(options["trust_env"] is False for options in client_options)
    assert all(options["follow_redirects"] is False for options in client_options)
    assert [request["headers"]["Authorization"] for request in requests] == [
        "Bearer backend-key",
        "Bearer admin-key",
    ]
    assert all("ambient-proxy.invalid" not in request["url"] for request in requests)


def test_canonical_candidate_proxy_uses_mock_transport_without_real_network(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/admin/channels/discover":
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "persisted": False,
                    "revision": "a" * 64,
                    "candidate": {
                        "connection_id": "",
                        "channel_operator": "official",
                        "base_url": "https://official.example/v1",
                        "adapter": "generic",
                        "auth_type": "bearer",
                        "allowed_private_networks": [],
                        "models_endpoint": "/models",
                    },
                    "models": [
                        {
                            "id": "author/chat-v1",
                            "model_author": "unknown",
                            "aliases": [],
                        }
                    ],
                    "report": {"mode": "discovery", "connections": []},
                },
            )
        return httpx.Response(200, json={"valid": True, "applied": False})

    real_async_client = httpx.AsyncClient

    def mock_client(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(providers_api.httpx, "AsyncClient", mock_client)
    client.app.dependency_overrides[get_settings] = _gateway_settings
    body = {
        "revision": "a" * 64,
        "candidate_key": "candidate-provider-key",
        "channel_operator": "official",
        "base_url": "https://official.example/v1",
        "adapter": "generic",
    }

    response = client.post(
        "/providers/channels/discover",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["persisted"] is False
    assert len(captured) == 1
    assert captured[0].url == "http://127.0.0.1:2030/admin/channels/discover"
    assert captured[0].headers["authorization"] == "Bearer admin-key"
    assert json.loads(captured[0].content) == body
    assert "candidate-provider-key" not in response.text


def test_channel_create_and_deploy_proxy_require_and_forward_admin_key(
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
                "applied": True,
                "connection_id": "newvendor-account",
                "revision": "b" * 64,
            },
        )

    client.app.dependency_overrides[get_settings] = _gateway_settings
    monkeypatch.setattr(providers_api, "_model_gateway_control_request", fake_request)
    body = {
        "revision": "a" * 64,
        "channel_operator": "newvendor",
        "base_url": "https://newvendor.example/v1",
    }

    missing = client.post("/providers/connections", headers=auth_headers, json=body)
    assert missing.status_code == 401
    assert calls == []

    accepted = client.post(
        "/providers/connections",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json=body,
    )
    assert accepted.status_code == 200
    assert calls[0]["path"] == "/admin/connections"
    assert calls[0]["api_key"] == "admin-key"

    deployed = client.post(
        "/providers/deployments",
        headers={**auth_headers, "X-Model-Gateway-Admin-Key": "admin-key"},
        json={
            "revision": "b" * 64,
            "connection": "newvendor-account",
            "deployments": [{"upstream_model": "newvendor/chat-1"}],
            "routes": [{"id": "memory.chat", "kind": "chat", "targets": ["$0"]}],
        },
    )
    assert deployed.status_code == 200
    assert calls[1]["path"] == "/admin/deployments"
    assert calls[1]["api_key"] == "admin-key"
