from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.api import deps
from app.api import health as health_api
from app.api.knowledge import _knowledge_runtime_status
from app.auth.tokens import AuthTokenStore
from app.config import Settings, get_settings
from app.llm.embedding_contract import (
    clear_embedding_contract_cache,
    resolve_embedding_contract,
)
from app.llm.runtime import MODEL_GATEWAY_REQUIRED_MESSAGE


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


def _control_payload(
    settings: Settings,
    *,
    embedding_space_id: str | None = None,
    embedding_dimensions: int | None = None,
    include_knowledge_routes: bool = True,
) -> dict[str, object]:
    chat_routes = [
        settings.model_gateway_chat_model,
        settings.model_gateway_memory_extract_model,
        settings.model_gateway_memory_compact_model,
        settings.model_gateway_memory_core_model,
        settings.model_gateway_memory_review_model,
    ]
    if include_knowledge_routes:
        chat_routes.extend(
            [
                settings.model_gateway_knowledge_fast_model,
                settings.model_gateway_knowledge_pro_model,
            ]
        )
    return {
        "connections": [
            {
                "id": "synthetic-channel",
                "enabled": True,
                "configured": True,
                "usage_scope": "backend_allowed",
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
                "dimensions": (
                    embedding_dimensions
                    if embedding_dimensions is not None
                    else settings.embedding_dimensions
                ),
                "embedding_space": (
                    embedding_space_id
                    if embedding_space_id is not None
                    else settings.model_gateway_embedding_space_id
                    or "synthetic-auto-space"
                ),
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


def test_readyz_does_not_require_agent_routes_for_local_knowledge(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    assert settings.knowledge_agent_egress_policy == "none"
    client.app.dependency_overrides[get_settings] = lambda: settings

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(
            200,
            json=_control_payload(
                settings,
                include_knowledge_routes=False,
            ),
        )

    _install_model_transport(monkeypatch, handler)

    assert client.get("/readyz").status_code == 200


def test_readyz_requires_agent_routes_when_knowledge_egress_is_enabled(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.knowledge_agent_egress_policy = "normal"
    client.app.dependency_overrides[get_settings] = lambda: settings

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(
            200,
            json=_control_payload(
                settings,
                include_knowledge_routes=False,
            ),
        )

    _install_model_transport(monkeypatch, handler)

    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "code": "model_gateway_route_visibility_mismatch",
    }


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


def test_readyz_rejects_missing_pinned_embedding_route(
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
    assert response.json()["code"] == "model_gateway_embedding_contract_mismatch"


@pytest.mark.parametrize("route_state", ["absent", "disabled"])
def test_readyz_auto_mode_accepts_embedding_route_off(
    route_state,
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.model_gateway_embedding_space_id = ""
    client.app.dependency_overrides[get_settings] = lambda: settings
    payload = _control_payload(settings)
    if route_state == "absent":
        payload["routes"] = payload["routes"][:-1]  # type: ignore[index]
    else:
        payload["routes"][-1]["enabled"] = False  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json=payload)

    _install_model_transport(monkeypatch, handler)
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "model_runtime": "central",
        "embedding_enabled": False,
    }


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


def test_readyz_rejects_mixed_embedding_target_contracts(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.model_gateway_embedding_space_id = ""
    client.app.dependency_overrides[get_settings] = lambda: settings
    payload = _control_payload(settings)
    payload["deployments"].append(  # type: ignore[union-attr]
        {
            "id": "synthetic-embedding-fallback",
            "connection": "synthetic-channel",
            "kind": "embedding",
            "enabled": True,
            "dimensions": 768,
            "embedding_space": "other-space",
        }
    )
    payload["routes"][-1]["targets"].append(  # type: ignore[index,union-attr]
        "synthetic-embedding-fallback"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json=payload)

    _install_model_transport(monkeypatch, handler)
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "code": "model_gateway_embedding_contract_mismatch",
    }


def test_readyz_rejects_enabled_embedding_route_without_usable_target(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.model_gateway_embedding_space_id = ""
    client.app.dependency_overrides[get_settings] = lambda: settings
    payload = _control_payload(settings)
    payload["deployments"][-1]["enabled"] = False  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json=payload)

    _install_model_transport(monkeypatch, handler)
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "code": "model_gateway_route_unavailable",
    }


def test_readyz_rejects_chat_routes_on_interactive_only_connection(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.model_gateway_embedding_space_id = ""
    client.app.dependency_overrides[get_settings] = lambda: settings
    payload = _control_payload(settings)
    payload["connections"][0]["usage_scope"] = "interactive_only"  # type: ignore[index]
    payload["routes"] = payload["routes"][:-1]  # type: ignore[index]
    payload["deployments"] = payload["deployments"][:-1]  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json=payload)

    _install_model_transport(monkeypatch, handler)
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "code": "model_gateway_route_unavailable",
    }


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


def test_chat_completions_returns_503_envelope_without_central_runtime(
    client,
    auth_headers,
    memory_store,
    knowledge_store,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    settings.model_gateway_base_url = ""
    settings.model_gateway_api_key = ""
    client.app.dependency_overrides[get_settings] = lambda: settings
    # The real chat gateway dependency must run so its fail-closed constructor
    # raises inside the request stack instead of reaching the fake client.
    client.app.dependency_overrides.pop(deps.get_chat_gateway_client, None)

    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "memory-auto",
            "messages": [{"role": "user", "content": "你好"}],
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "model_runtime_configuration_error",
            "message": MODEL_GATEWAY_REQUIRED_MESSAGE,
        }
    }
    assert "Traceback" not in response.text


def test_central_knowledge_status_uses_resolved_routes_and_embedding(
    memory_store,
    knowledge_store,
) -> None:
    settings = _central_settings(memory_store, knowledge_store)
    resolve_embedding_contract(settings, _control_payload(settings))

    status = _knowledge_runtime_status(settings)

    assert status["model_runtime"] == "central"
    assert status["model_gateway_enabled"] is True
    assert status["agent_flash_model"] == "knowledge.fast"
    assert status["agent_pro_model"] == "knowledge.pro"
    assert status["embedding_enabled"] is True
    assert status["embedding_model"] == "memory.embedding"


@pytest.fixture(autouse=True)
def _clear_readyz_cache() -> Iterator[None]:
    health_api._readyz_cache.clear()
    clear_embedding_contract_cache()
    yield
    health_api._readyz_cache.clear()
    clear_embedding_contract_cache()


def _ready_central_settings(memory_store, knowledge_store) -> Settings:
    return _central_settings(memory_store, knowledge_store)


def _install_ready_model_gateway(monkeypatch, settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json=_control_payload(settings))

    _install_model_transport(monkeypatch, handler)


def _count_database_checks(monkeypatch) -> list[int]:
    calls = [0]
    original = health_api._database_readiness_code

    def counting(settings: Settings) -> str:
        calls[0] += 1
        return original(settings)

    monkeypatch.setattr(health_api, "_database_readiness_code", counting)
    return calls


def test_readyz_caches_ready_result_within_ttl(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _ready_central_settings(memory_store, knowledge_store)
    client.app.dependency_overrides[get_settings] = lambda: settings
    _install_ready_model_gateway(monkeypatch, settings)
    calls = _count_database_checks(monkeypatch)

    first = client.get("/readyz")
    second = client.get("/readyz")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json() == {
        "status": "ready",
        "model_runtime": "central",
        "embedding_enabled": True,
    }
    assert calls[0] == 1


def test_readyz_caches_not_ready_result_within_ttl(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _ready_central_settings(memory_store, knowledge_store)
    settings.gateway_api_key = ""
    settings.gateway_legacy_api_key_enabled = False
    client.app.dependency_overrides[get_settings] = lambda: settings
    calls = _count_database_checks(monkeypatch)

    first = client.get("/readyz")
    second = client.get("/readyz")

    assert first.status_code == 503
    assert second.status_code == 503
    assert second.json() == first.json() == {
        "status": "not_ready",
        "code": "auth_credentials_unavailable",
    }
    assert calls[0] == 1


def test_readyz_recomputes_after_cache_expiry(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _ready_central_settings(memory_store, knowledge_store)
    client.app.dependency_overrides[get_settings] = lambda: settings
    _install_ready_model_gateway(monkeypatch, settings)
    calls = _count_database_checks(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(
        health_api, "time", SimpleNamespace(monotonic=lambda: now[0])
    )

    assert client.get("/readyz").status_code == 200
    now[0] += health_api.READYZ_CACHE_TTL_SECONDS - 1.0
    assert client.get("/readyz").status_code == 200
    assert calls[0] == 1
    now[0] += 2.0
    assert client.get("/readyz").status_code == 200
    assert calls[0] == 2


def test_readyz_cache_is_scoped_to_settings_fingerprint(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings_a = _ready_central_settings(memory_store, knowledge_store)
    settings_b = _ready_central_settings(memory_store, knowledge_store)
    settings_b.embedding_dimensions = 512

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        current = client.app.dependency_overrides[get_settings]()
        return httpx.Response(200, json=_control_payload(current))

    _install_model_transport(monkeypatch, handler)
    calls = _count_database_checks(monkeypatch)

    client.app.dependency_overrides[get_settings] = lambda: settings_a
    assert client.get("/readyz").status_code == 200
    client.app.dependency_overrides[get_settings] = lambda: settings_b
    assert client.get("/readyz").status_code == 200
    client.app.dependency_overrides[get_settings] = lambda: settings_a
    assert client.get("/readyz").status_code == 200

    assert calls[0] == 2


def test_readyz_cache_evicts_oldest_beyond_capacity(
    client,
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    base = _ready_central_settings(memory_store, knowledge_store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        current = client.app.dependency_overrides[get_settings]()
        return httpx.Response(200, json=_control_payload(current))

    _install_model_transport(monkeypatch, handler)
    calls = _count_database_checks(monkeypatch)

    client.app.dependency_overrides[get_settings] = lambda: base
    assert client.get("/readyz").status_code == 200
    for extra in range(health_api._READYZ_CACHE_MAX_ENTRIES):
        settings = _ready_central_settings(memory_store, knowledge_store)
        settings.embedding_dimensions = 64 + extra
        client.app.dependency_overrides[get_settings] = lambda s=settings: s
        assert client.get("/readyz").status_code == 200
    assert calls[0] == health_api._READYZ_CACHE_MAX_ENTRIES + 1

    client.app.dependency_overrides[get_settings] = lambda: base
    assert client.get("/readyz").status_code == 200
    assert calls[0] == health_api._READYZ_CACHE_MAX_ENTRIES + 2


async def test_readyz_concurrent_requests_share_single_computation(
    memory_store,
    knowledge_store,
    monkeypatch,
) -> None:
    settings = _ready_central_settings(memory_store, knowledge_store)
    _install_ready_model_gateway(monkeypatch, settings)
    calls = [0]
    started = threading.Event()
    release = threading.Event()
    original = health_api._database_readiness_code

    def blocking(settings: Settings) -> str:
        calls[0] += 1
        started.set()
        assert release.wait(timeout=10.0)
        return original(settings)

    monkeypatch.setattr(health_api, "_database_readiness_code", blocking)

    first = asyncio.create_task(health_api.readiness(settings))
    assert await asyncio.to_thread(started.wait, 10.0)
    others = [asyncio.create_task(health_api.readiness(settings)) for _ in range(4)]
    release.set()
    responses = await asyncio.gather(first, *others)

    assert calls[0] == 1
    assert [response.status_code for response in responses] == [200] * 5
