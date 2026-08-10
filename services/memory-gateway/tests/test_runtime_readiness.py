from __future__ import annotations

from pathlib import Path

import httpx

from app.api import health as health_api
from app.api.knowledge import _knowledge_runtime_status
from app.auth.tokens import AuthTokenStore
from app.config import Settings, get_settings


def _central_settings(memory_store, knowledge_store) -> Settings:
    auth_path = Path(memory_store.database_path).with_name("auth.db")
    AuthTokenStore(auth_path).init_db()
    return Settings(
        _env_file=None,
        GATEWAY_API_KEY="test-gateway-key",
        DATABASE_PATH=memory_store.database_path,
        KNOWLEDGE_DATABASE_PATH=knowledge_store.database_path,
        AUTH_DATABASE_PATH=str(auth_path),
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-backend-key",
        MODEL_GATEWAY_EMBEDDING_SPACE_ID="synthetic-space-v1",
        EMBEDDING_DIMENSIONS=1024,
    )


def _control_payload(settings: Settings) -> dict[str, object]:
    chat_routes = (
        settings.model_gateway_chat_model,
        settings.model_gateway_memory_extract_model,
        settings.model_gateway_memory_compact_model,
        settings.model_gateway_memory_core_model,
        settings.model_gateway_memory_review_model,
        settings.model_gateway_knowledge_fast_model,
        settings.model_gateway_knowledge_pro_model,
    )
    return {
        "connections": [
            {
                "id": "synthetic-channel",
                "enabled": True,
                "configured": True,
            }
        ],
        "deployments": [
            {
                "id": "synthetic-chat",
                "connection": "synthetic-channel",
                "kind": "chat",
                "enabled": True,
                "dimensions": None,
                "embedding_space": "",
            },
            {
                "id": "synthetic-embedding",
                "connection": "synthetic-channel",
                "kind": "embedding",
                "enabled": True,
                "dimensions": settings.embedding_dimensions,
                "embedding_space": settings.model_gateway_embedding_space_id,
            },
        ],
        "routes": [
            *[
                {
                    "id": route_id,
                    "kind": "chat",
                    "enabled": True,
                    "targets": ["synthetic-chat"],
                }
                for route_id in chat_routes
            ],
            {
                "id": settings.model_gateway_embedding_model,
                "kind": "embedding",
                "enabled": True,
                "targets": ["synthetic-embedding"],
            },
        ],
    }


def _install_model_transport(monkeypatch, handler) -> None:
    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(health_api.httpx, "AsyncClient", client_factory)


def test_readyz_checks_databases_model_ready_backend_scope_and_embedding(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    client.app.dependency_overrides[get_settings] = lambda: settings
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.headers.get("authorization", "")))
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        assert request.url.path == "/admin/configuration"
        assert request.headers["authorization"] == "Bearer central-backend-key"
        return httpx.Response(200, json=_control_payload(settings))

    _install_model_transport(monkeypatch, handler)
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "model_runtime": "central",
        "embedding_enabled": True,
    }
    assert requests == [
        ("/readyz", ""),
        ("/admin/configuration", "Bearer central-backend-key"),
    ]
    assert "central-backend-key" not in response.text


def test_readyz_rejects_bad_database_before_network(
    client,
    memory_store,
    knowledge_store,
    tmp_path,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    damaged = tmp_path / "damaged-knowledge.db"
    damaged.write_bytes(b"not sqlite")
    settings.knowledge_database_path = str(damaged)
    client.app.dependency_overrides[get_settings] = lambda: settings

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "code": "knowledge_database_unavailable",
    }


def test_readyz_rejects_missing_central_usage_attribution_secret(
    client,
    memory_store,
    knowledge_store,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.gateway_signing_secret = ""
    client.app.dependency_overrides[get_settings] = lambda: settings

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "code": "model_gateway_usage_attribution_unavailable",
    }


def test_readyz_rejects_auth_database_without_any_usable_credential(
    client,
    memory_store,
    knowledge_store,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.gateway_api_key = ""
    settings.gateway_legacy_api_key_enabled = False
    client.app.dependency_overrides[get_settings] = lambda: settings

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "code": "auth_credentials_unavailable",
    }


def test_readyz_rejects_missing_backend_route(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    client.app.dependency_overrides[get_settings] = lambda: settings
    payload = _control_payload(settings)
    payload["routes"] = payload["routes"][:-1]  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json=payload)

    _install_model_transport(monkeypatch, handler)
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["code"] == "model_gateway_route_visibility_mismatch"


def test_readyz_rejects_wrong_embedding_space_or_dimensions(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    client.app.dependency_overrides[get_settings] = lambda: settings
    payload = _control_payload(settings)
    payload["deployments"][1]["embedding_space"] = "wrong-space"  # type: ignore[index]
    payload["deployments"][1]["dimensions"] = 768  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json=payload)

    _install_model_transport(monkeypatch, handler)
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["code"] == "model_gateway_embedding_contract_mismatch"


def test_readyz_requires_model_gateway_operational_ready(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    client.app.dependency_overrides[get_settings] = lambda: settings

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(503, json={"status": "not_ready"})
        return httpx.Response(200, json=_control_payload(settings))

    _install_model_transport(monkeypatch, handler)
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["code"] == "model_gateway_not_ready"


def test_partial_central_runtime_is_not_ready_and_never_reports_direct(
    client,
    auth_headers,
    memory_store,
    knowledge_store,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.model_gateway_api_key = ""
    client.app.dependency_overrides[get_settings] = lambda: settings

    ready = client.get("/readyz")
    providers = client.get("/providers/status", headers=auth_headers)
    knowledge = _knowledge_runtime_status(settings)

    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["code"] == "model_runtime_configuration_error"
    assert providers.status_code == 200
    assert providers.json()["runtime"]["model_runtime"] == "invalid"
    assert providers.json()["setup"]["service_ready"] is False
    assert "必须同时配置" in providers.json()["config_error"]
    assert knowledge["model_runtime"] == "invalid"
    assert knowledge["agent_enabled"] is False
    assert knowledge["embedding_enabled"] is False
    assert "必须同时配置" in knowledge["model_runtime_error"]


def test_central_knowledge_status_uses_resolved_routes_and_embedding(
    memory_store,
    knowledge_store,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)

    status = _knowledge_runtime_status(settings)

    assert status["model_runtime"] == "central"
    assert status["model_gateway_enabled"] is True
    assert status["agent_flash_model"] == "knowledge.fast"
    assert status["agent_pro_model"] == "knowledge.pro"
    assert status["embedding_enabled"] is True
    assert status["embedding_model"] == "memory.embedding"
