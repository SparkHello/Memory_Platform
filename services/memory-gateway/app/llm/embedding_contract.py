from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import logging
import threading
from typing import Any, Literal

import httpx
from model_gateway_contracts import MEMORY_EMBEDDING_ROUTE


EmbeddingMode = Literal["auto", "pinned"]
EmbeddingState = Literal["ready", "off", "invalid", "unavailable"]

EMBEDDING_READY_CODE = "ok"
EMBEDDING_OFF_CODE = "embedding_route_off"
EMBEDDING_CONTRACT_MISMATCH_CODE = "model_gateway_embedding_contract_mismatch"
EMBEDDING_ROUTE_UNAVAILABLE_CODE = "model_gateway_route_unavailable"

_MAX_CONTROL_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_SNAPSHOTS = 16
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingContractSnapshot:
    """Process-local authoritative view of the ``memory.embedding`` route."""

    mode: EmbeddingMode
    state: EmbeddingState
    code: str
    route_model: str
    upstream_model: str = ""
    dimensions: int = 0
    space_id: str = ""
    generation: int = 0

    @property
    def configured(self) -> bool:
        return self.state == "ready"


_snapshot_lock = threading.RLock()
_snapshots: dict[str, EmbeddingContractSnapshot] = {}
_generation = 0


def embedding_contract_mode(settings: Any) -> EmbeddingMode:
    return "pinned" if _configured_space(settings) else "auto"


def get_embedding_contract_snapshot(settings: Any) -> EmbeddingContractSnapshot:
    key = _settings_fingerprint(settings)
    with _snapshot_lock:
        snapshot = _snapshots.get(key)
        if snapshot is not None:
            # Preserve insertion order as an inexpensive bounded LRU.
            _snapshots.pop(key, None)
            _snapshots[key] = snapshot
            return snapshot
        return EmbeddingContractSnapshot(
            mode=embedding_contract_mode(settings),
            state="unavailable",
            code="model_gateway_control_unavailable",
            route_model=_route_model(settings),
        )


def embedding_contract_generation(settings: Any) -> int:
    return get_embedding_contract_snapshot(settings).generation


def resolve_embedding_contract(
    settings: Any,
    payload: dict[str, Any],
) -> EmbeddingContractSnapshot:
    """Validate and adopt the backend-scoped Model Gateway route snapshot."""

    candidate = _contract_from_control(settings, payload)
    return _store_snapshot(settings, candidate)


def set_embedding_contract_failure(
    settings: Any,
    *,
    state: Literal["invalid", "unavailable"],
    code: str,
) -> EmbeddingContractSnapshot:
    candidate = EmbeddingContractSnapshot(
        mode=embedding_contract_mode(settings),
        state=state,
        code=code,
        route_model=_route_model(settings),
    )
    return _store_snapshot(settings, candidate)


def invalidate_embedding_contract(settings: Any) -> EmbeddingContractSnapshot:
    return set_embedding_contract_failure(
        settings,
        state="unavailable",
        code="model_gateway_control_unavailable",
    )


def clear_embedding_contract_cache() -> None:
    """Test/process-reset helper; production refreshes never need this."""

    global _generation
    with _snapshot_lock:
        _snapshots.clear()
        _generation = 0


async def refresh_embedding_contract(settings: Any) -> EmbeddingContractSnapshot:
    """Refresh the route contract using only the backend client credential."""

    base_url = str(getattr(settings, "model_gateway_base_url", "") or "").strip()
    api_key = str(getattr(settings, "model_gateway_api_key", "") or "").strip()
    if bool(base_url) != bool(api_key) or not base_url:
        return set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_runtime_configuration_error",
        )
    normalized_base_url = base_url.rstrip("/")
    if normalized_base_url.endswith("/v1"):
        normalized_base_url = normalized_base_url[:-3]
    try:
        timeout = min(float(getattr(settings, "request_timeout_seconds", 60.0)), 10.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"{normalized_base_url}/admin/configuration",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
    except (httpx.HTTPError, TypeError, ValueError):
        return set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_unavailable",
        )
    if response.status_code in {401, 403}:
        return set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_backend_auth_failed",
        )
    if response.status_code != 200:
        return set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_control_unavailable",
        )
    if len(response.content) > _MAX_CONTROL_RESPONSE_BYTES:
        return set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
    return resolve_embedding_contract(settings, payload)


async def embedding_contract_refresh_loop(
    settings: Any,
    *,
    interval_seconds: float = 30.0,
) -> None:
    """Refresh after each interval; the lifespan performs the initial refresh."""

    while True:
        await asyncio.sleep(max(1.0, interval_seconds))
        try:
            await refresh_embedding_contract(settings)
        except Exception:
            set_embedding_contract_failure(
                settings,
                state="unavailable",
                code="model_gateway_control_unavailable",
            )
            logger.exception("周期刷新 embedding route 契约失败；将在下周期重试。")


def _contract_from_control(
    settings: Any,
    payload: dict[str, Any],
) -> EmbeddingContractSnapshot:
    mode = embedding_contract_mode(settings)
    route_model = _route_model(settings)
    raw_routes = payload.get("routes")
    raw_deployments = payload.get("deployments")
    raw_connections = payload.get("connections")
    if not all(
        isinstance(value, list)
        for value in (raw_routes, raw_deployments, raw_connections)
    ):
        return _failure_snapshot(
            mode, route_model, "invalid", "model_gateway_invalid_response"
        )

    routes = _objects_by_id(raw_routes)
    deployments = _objects_by_id(raw_deployments)
    connections = _objects_by_id(raw_connections)
    if routes is None or deployments is None or connections is None:
        return _failure_snapshot(
            mode, route_model, "invalid", "model_gateway_invalid_response"
        )

    route = routes.get(route_model)
    if route is None:
        if mode == "auto":
            return EmbeddingContractSnapshot(
                mode=mode,
                state="off",
                code=EMBEDDING_OFF_CODE,
                route_model=route_model,
            )
        return _failure_snapshot(
            mode, route_model, "invalid", EMBEDDING_CONTRACT_MISMATCH_CODE
        )
    enabled = route.get("enabled")
    if enabled is False:
        if mode == "auto":
            return EmbeddingContractSnapshot(
                mode=mode,
                state="off",
                code=EMBEDDING_OFF_CODE,
                route_model=route_model,
            )
        return _failure_snapshot(
            mode, route_model, "invalid", EMBEDDING_CONTRACT_MISMATCH_CODE
        )
    targets = route.get("targets")
    if (
        enabled is not True
        or route.get("kind") != "embedding"
        or not isinstance(targets, list)
        or not targets
        or any(not isinstance(target, str) or not target for target in targets)
    ):
        return _failure_snapshot(
            mode, route_model, "invalid", EMBEDDING_CONTRACT_MISMATCH_CODE
        )

    contracts: set[tuple[str, int]] = set()
    usable = False
    upstream_model = ""
    for target in targets:
        deployment = deployments.get(target)
        if deployment is None or deployment.get("kind") != "embedding":
            return _failure_snapshot(
                mode, route_model, "invalid", EMBEDDING_CONTRACT_MISMATCH_CODE
            )
        space_id = _valid_space(deployment.get("embedding_space"))
        dimensions = _valid_dimensions(deployment.get("dimensions"))
        if not space_id or dimensions == 0:
            return _failure_snapshot(
                mode, route_model, "invalid", EMBEDDING_CONTRACT_MISMATCH_CODE
            )
        contracts.add((space_id, dimensions))
        if not upstream_model:
            raw_model = deployment.get("upstream_model")
            upstream_model = raw_model if isinstance(raw_model, str) else ""
        connection = connections.get(str(deployment.get("connection") or ""))
        usable = usable or bool(
            deployment.get("enabled") is True
            and connection is not None
            and connection.get("enabled") is True
            and connection.get("configured") is True
            # Memory Gateway authenticates to Model Gateway as a backend
            # client. Interactive-only/disabled connections are deliberately
            # skipped by Model Gateway's Router and therefore cannot make an
            # embedding route operationally usable for this process.
            and connection.get("usage_scope") == "backend_allowed"
        )
    if len(contracts) != 1:
        return _failure_snapshot(
            mode, route_model, "invalid", EMBEDDING_CONTRACT_MISMATCH_CODE
        )
    space_id, dimensions = next(iter(contracts))
    if mode == "pinned" and (
        space_id != _configured_space(settings)
        or dimensions != _configured_dimensions(settings)
    ):
        return _failure_snapshot(
            mode, route_model, "invalid", EMBEDDING_CONTRACT_MISMATCH_CODE
        )
    if not usable:
        return _failure_snapshot(
            mode, route_model, "unavailable", EMBEDDING_ROUTE_UNAVAILABLE_CODE
        )
    return EmbeddingContractSnapshot(
        mode=mode,
        state="ready",
        code=EMBEDDING_READY_CODE,
        route_model=route_model,
        upstream_model=upstream_model,
        dimensions=dimensions,
        space_id=space_id,
    )


def _objects_by_id(items: list[Any]) -> dict[str, dict[str, Any]] | None:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            return None
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in result:
            return None
        result[item_id] = item
    return result


def _failure_snapshot(
    mode: EmbeddingMode,
    route_model: str,
    state: Literal["invalid", "unavailable"],
    code: str,
) -> EmbeddingContractSnapshot:
    return EmbeddingContractSnapshot(
        mode=mode,
        state=state,
        code=code,
        route_model=route_model,
    )


def _store_snapshot(
    settings: Any,
    candidate: EmbeddingContractSnapshot,
) -> EmbeddingContractSnapshot:
    global _generation
    key = _settings_fingerprint(settings)
    with _snapshot_lock:
        current = _snapshots.get(key)
        comparable = replace(candidate, generation=0)
        if current is not None and replace(current, generation=0) == comparable:
            _snapshots.pop(key, None)
            _snapshots[key] = current
            return current
        _generation += 1
        stored = replace(candidate, generation=_generation)
        _snapshots.pop(key, None)
        _snapshots[key] = stored
        while len(_snapshots) > _MAX_SNAPSHOTS:
            _snapshots.pop(next(iter(_snapshots)))
        return stored


def _settings_fingerprint(settings: Any) -> str:
    configured_space = _configured_space(settings)
    mode: EmbeddingMode = "pinned" if configured_space else "auto"
    fields = (
        str(getattr(settings, "model_gateway_base_url", "") or "").strip(),
        str(getattr(settings, "model_gateway_api_key", "") or "").strip(),
        _route_model(settings),
        mode,
        configured_space if mode == "pinned" else "",
        str(_configured_dimensions(settings)) if mode == "pinned" else "",
    )
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def _route_model(settings: Any) -> str:
    return str(
        getattr(settings, "model_gateway_embedding_model", MEMORY_EMBEDDING_ROUTE)
        or MEMORY_EMBEDDING_ROUTE
    ).strip()


def _configured_space(settings: Any) -> str:
    return str(
        getattr(settings, "model_gateway_embedding_space_id", "") or ""
    ).strip()


def _configured_dimensions(settings: Any) -> int:
    return _valid_dimensions(getattr(settings, "embedding_dimensions", 0))


def _valid_dimensions(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 1 <= value <= 65536 else 0


def _valid_space(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 300
        or any(not 33 <= ord(character) <= 126 for character in normalized)
    ):
        return ""
    return normalized
