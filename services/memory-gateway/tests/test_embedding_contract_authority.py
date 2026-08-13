from __future__ import annotations

from copy import deepcopy

import httpx
import pytest

from app.config import Settings
from app.llm.embedding_contract import (
    EMBEDDING_CONTRACT_MISMATCH_CODE,
    EMBEDDING_OFF_CODE,
    EMBEDDING_READY_CODE,
    EMBEDDING_ROUTE_UNAVAILABLE_CODE,
    clear_embedding_contract_cache,
    get_embedding_contract_snapshot,
    refresh_embedding_contract,
    resolve_embedding_contract,
)
from app.llm import embedding_contract as embedding_contract_module


def _settings(*, pinned_space: str = "", dimensions: int = 1024) -> Settings:
    return Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="test-backend-key",
        MODEL_GATEWAY_EMBEDDING_SPACE_ID=pinned_space,
        EMBEDDING_DIMENSIONS=dimensions,
    )


def _embedding_control_snapshot(
    *,
    space_id: str = "route-owned-space",
    dimensions: int = 768,
    route_enabled: bool = True,
    deployment_enabled: bool = True,
    connection_enabled: bool = True,
    connection_configured: bool = True,
    connection_usage_scope: str = "backend_allowed",
) -> dict[str, object]:
    return {
        "connections": [
            {
                "id": "embedding-channel",
                "enabled": connection_enabled,
                "configured": connection_configured,
                "usage_scope": connection_usage_scope,
            }
        ],
        "deployments": [
            {
                "id": "embedding-primary",
                "connection": "embedding-channel",
                "upstream_model": "vendor/embed-v2",
                "kind": "embedding",
                "enabled": deployment_enabled,
                "dimensions": dimensions,
                "embedding_space": space_id,
            }
        ],
        "routes": [
            {
                "id": "memory.embedding",
                "kind": "embedding",
                "enabled": route_enabled,
                "targets": ["embedding-primary"],
            }
        ],
    }


@pytest.fixture(autouse=True)
def _clear_contract_snapshots() -> None:
    clear_embedding_contract_cache()
    yield
    clear_embedding_contract_cache()


def test_auto_mode_adopts_route_owned_space_and_dimensions() -> None:
    settings = _settings(dimensions=1024)

    snapshot = resolve_embedding_contract(
        settings,
        _embedding_control_snapshot(dimensions=768),
    )

    assert snapshot.mode == "auto"
    assert snapshot.state == "ready"
    assert snapshot.code == EMBEDDING_READY_CODE
    assert snapshot.configured is True
    assert snapshot.space_id == "route-owned-space"
    assert snapshot.dimensions == 768
    assert snapshot.upstream_model == "vendor/embed-v2"


@pytest.mark.parametrize("route_state", ["absent", "disabled"])
def test_auto_mode_treats_absent_or_disabled_route_as_off(route_state: str) -> None:
    settings = _settings()
    payload = _embedding_control_snapshot()
    if route_state == "absent":
        payload["routes"] = []
    else:
        payload["routes"][0]["enabled"] = False  # type: ignore[index]

    snapshot = resolve_embedding_contract(settings, payload)

    assert snapshot.mode == "auto"
    assert snapshot.state == "off"
    assert snapshot.code == EMBEDDING_OFF_CODE
    assert snapshot.configured is False
    assert snapshot.space_id == ""
    assert snapshot.dimensions == 0


def test_pinned_mode_rejects_route_contract_mismatch() -> None:
    settings = _settings(pinned_space="pinned-space", dimensions=1024)

    snapshot = resolve_embedding_contract(
        settings,
        _embedding_control_snapshot(
            space_id="different-space",
            dimensions=768,
        ),
    )

    assert snapshot.mode == "pinned"
    assert snapshot.state == "invalid"
    assert snapshot.code == EMBEDDING_CONTRACT_MISMATCH_CODE
    assert snapshot.configured is False
    assert snapshot.space_id == ""
    assert snapshot.dimensions == 0


def test_enabled_route_rejects_mixed_target_contracts() -> None:
    settings = _settings()
    payload = _embedding_control_snapshot()
    second = deepcopy(payload["deployments"][0])  # type: ignore[index]
    second.update(
        {
            "id": "embedding-fallback",
            "embedding_space": "fallback-space",
            "dimensions": 1536,
        }
    )
    payload["deployments"].append(second)  # type: ignore[union-attr]
    payload["routes"][0]["targets"].append("embedding-fallback")  # type: ignore[index,union-attr]

    snapshot = resolve_embedding_contract(settings, payload)

    assert snapshot.state == "invalid"
    assert snapshot.code == EMBEDDING_CONTRACT_MISMATCH_CODE
    assert snapshot.configured is False


@pytest.mark.parametrize(
    "payload",
    [
        _embedding_control_snapshot(deployment_enabled=False),
        _embedding_control_snapshot(connection_enabled=False),
        _embedding_control_snapshot(connection_configured=False),
    ],
    ids=["deployment-disabled", "connection-disabled", "connection-unconfigured"],
)
def test_enabled_route_without_usable_target_is_unavailable(
    payload: dict[str, object],
) -> None:
    snapshot = resolve_embedding_contract(_settings(), payload)

    assert snapshot.state == "unavailable"
    assert snapshot.code == EMBEDDING_ROUTE_UNAVAILABLE_CODE
    assert snapshot.configured is False
    assert snapshot.space_id == ""
    assert snapshot.dimensions == 0


@pytest.mark.parametrize(
    ("pinned_space", "dimensions"),
    [("", 1024), ("route-owned-space", 768)],
    ids=["auto", "pinned"],
)
@pytest.mark.parametrize("usage_scope", ["interactive_only", "disabled"])
def test_backend_embedding_rejects_non_backend_connection_scope(
    pinned_space: str,
    dimensions: int,
    usage_scope: str,
) -> None:
    settings = _settings(pinned_space=pinned_space, dimensions=dimensions)

    snapshot = resolve_embedding_contract(
        settings,
        _embedding_control_snapshot(connection_usage_scope=usage_scope),
    )

    assert snapshot.mode == ("pinned" if pinned_space else "auto")
    assert snapshot.state == "unavailable"
    assert snapshot.code == EMBEDDING_ROUTE_UNAVAILABLE_CODE
    assert snapshot.configured is False


def test_snapshot_cache_isolated_by_backend_key_without_secret_repr() -> None:
    first = _settings()
    second = _settings()
    first.model_gateway_api_key = "backend-key-alpha"
    second.model_gateway_api_key = "backend-key-beta"

    first_snapshot = resolve_embedding_contract(
        first,
        _embedding_control_snapshot(),
    )
    second_snapshot = get_embedding_contract_snapshot(second)

    assert first_snapshot.state == "ready"
    assert second_snapshot.state == "unavailable"
    serialized_cache = repr(embedding_contract_module._snapshots)
    assert "backend-key-alpha" not in repr(first_snapshot)
    assert "backend-key-alpha" not in serialized_cache
    assert "backend-key-beta" not in repr(second_snapshot)
    assert "backend-key-beta" not in serialized_cache


@pytest.mark.asyncio
async def test_refresh_reads_control_snapshot_with_backend_key_and_no_real_network(
    monkeypatch,
) -> None:
    settings = _settings()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_embedding_control_snapshot())

    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(
            *args,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    monkeypatch.setattr(embedding_contract_module.httpx, "AsyncClient", client_factory)

    snapshot = await refresh_embedding_contract(settings)

    assert snapshot.state == "ready"
    assert snapshot.dimensions == 768
    assert len(captured) == 1
    assert captured[0].url == "http://127.0.0.1:2030/admin/configuration"
    assert captured[0].headers["authorization"] == "Bearer test-backend-key"
    assert "test-backend-key" not in repr(snapshot)
