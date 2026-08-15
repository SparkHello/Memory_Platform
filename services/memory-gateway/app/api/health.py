import asyncio
import hashlib
from pathlib import Path
import sqlite3
import time
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
import httpx

from app.config import Settings, get_settings
from app.disk_capacity import disk_readiness_code
from app.llm.embedding_contract import (
    embedding_contract_generation,
    resolve_embedding_contract,
    set_embedding_contract_failure,
)
from app.llm.runtime import (
    ModelRuntimeConfigurationError,
    resolve_model_runtime,
)

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


_MAX_CONTROL_RESPONSE_BYTES = 2 * 1024 * 1024

READYZ_CACHE_TTL_SECONDS = 3.0
_READYZ_CACHE_MAX_ENTRIES = 8

# fingerprint -> (expires_at, status_code, content). The fingerprint keeps
# tests with different dependency-overridden Settings from sharing results.
_readyz_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
# Single global single-flight lock. It is only ever held for one computation
# and released before the event loop can change, so per-test event loops are
# safe (an unheld asyncio.Lock may be reused across loops).
_readyz_lock = asyncio.Lock()


@router.get("/readyz")
async def readiness(
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Operational readiness; liveness remains dependency-free ``/health``.

    Results are cached per settings fingerprint for ``READYZ_CACHE_TTL_SECONDS``
    and concurrent misses share one computation (single-flight), so
    unauthenticated high-frequency probes cannot amplify the SQLite quick_check
    and model control-plane calls.
    """
    async with _readyz_lock:
        fingerprint = _readyz_cache_fingerprint(settings)
        now = time.monotonic()
        cached = _readyz_cache.get(fingerprint)
        if cached is not None and cached[0] > now:
            _, status_code, content = cached
        else:
            status_code, content = await _readiness_result(settings)
            # Contract resolution can advance the snapshot generation while a
            # miss is being computed. Store under the resulting fingerprint so
            # the very next probe can reuse this verdict.
            fingerprint = _readyz_cache_fingerprint(settings)
            _readyz_cache.pop(fingerprint, None)
            _readyz_cache[fingerprint] = (
                now + READYZ_CACHE_TTL_SECONDS,
                status_code,
                content,
            )
            while len(_readyz_cache) > _READYZ_CACHE_MAX_ENTRIES:
                _readyz_cache.pop(next(iter(_readyz_cache)))
    return JSONResponse(status_code=status_code, content=content)


async def _readiness_result(settings: Settings) -> tuple[int, dict[str, Any]]:
    try:
        runtime = resolve_model_runtime(settings)
    except ModelRuntimeConfigurationError:
        return _not_ready("model_runtime_configuration_error")
    database_code = await asyncio.to_thread(_database_readiness_code, settings)
    if database_code:
        return _not_ready(database_code)
    disk_code = await asyncio.to_thread(disk_readiness_code, settings)
    if disk_code:
        return _not_ready(disk_code)
    if not settings.gateway_signing_secret:
        return _not_ready("model_gateway_usage_attribution_unavailable")
    model_code = await _central_model_readiness_code(settings, runtime)
    if model_code:
        return _not_ready(model_code)
    runtime = resolve_model_runtime(settings)
    return 200, {
        "status": "ready",
        "model_runtime": runtime.mode,
        "embedding_enabled": runtime.embedding.enabled,
    }


def _not_ready(code: str) -> tuple[int, dict[str, Any]]:
    return 503, {"status": "not_ready", "code": code}


def _readyz_cache_fingerprint(settings: Settings) -> str:
    """Hash every field that can change the readiness verdict, secrets included.

    Hashing keeps raw API keys and signing secrets out of the long-lived
    in-memory cache keys.
    """
    fields = (
        settings.database_path,
        settings.knowledge_database_path,
        settings.auth_database_path,
        settings.gateway_api_key,
        str(settings.gateway_legacy_api_key_enabled),
        settings.gateway_signing_secret,
        settings.model_gateway_base_url,
        settings.model_gateway_api_key,
        settings.model_gateway_chat_model,
        settings.model_gateway_memory_extract_model,
        settings.model_gateway_memory_compact_model,
        settings.model_gateway_memory_core_model,
        settings.model_gateway_memory_review_model,
        settings.model_gateway_knowledge_fast_model,
        settings.model_gateway_knowledge_pro_model,
        settings.knowledge_agent_egress_policy,
        settings.model_gateway_embedding_model,
        settings.model_gateway_embedding_space_id,
        str(settings.embedding_dimensions),
        str(settings.request_timeout_seconds),
        str(settings.disk_soft_reserve_bytes),
        str(settings.disk_hard_reserve_bytes),
        str(embedding_contract_generation(settings)),
    )
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def _database_readiness_code(settings: Settings) -> str:
    checks = (
        ("memory_database", settings.database_path),
        ("knowledge_database", settings.knowledge_database_path),
        ("auth_database", settings.auth_database_path),
    )
    active_scoped_token = False
    for name, raw_path in checks:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            return f"{name}_unavailable"
        try:
            uri = "file:" + quote(str(path.resolve()), safe="/:\\") + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
                if name == "auth_database":
                    active_scoped_token = (
                        connection.execute(
                            "SELECT 1 FROM auth_tokens "
                            "WHERE revoked_at IS NULL LIMIT 1"
                        ).fetchone()
                        is not None
                    )
        except (OSError, sqlite3.Error):
            return f"{name}_unavailable"
        if row is None or str(row[0]).lower() != "ok":
            return f"{name}_integrity_failed"
    legacy_available = bool(
        settings.gateway_legacy_api_key_enabled
        and settings.gateway_api_key.strip()
    )
    if not active_scoped_token and not legacy_available:
        return "auth_credentials_unavailable"
    return ""


async def _central_model_readiness_code(settings: Settings, runtime: Any) -> str:
    base_url = runtime.base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(
            timeout=min(float(settings.request_timeout_seconds), 10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            ready = await client.get(f"{base_url}/readyz", headers=headers)
            control = await client.get(
                f"{base_url}/admin/configuration",
                headers={
                    **headers,
                    "Authorization": f"Bearer {runtime.api_key}",
                },
            )
    except httpx.HTTPError:
        set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_unavailable",
        )
        return "model_gateway_unavailable"
    if ready.status_code != 200:
        set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_not_ready",
        )
        return "model_gateway_not_ready"
    try:
        ready_payload = ready.json()
    except ValueError:
        set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
        return "model_gateway_invalid_response"
    if not isinstance(ready_payload, dict) or ready_payload.get("status") != "ready":
        set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_not_ready",
        )
        return "model_gateway_not_ready"
    if control.status_code in {401, 403}:
        set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_backend_auth_failed",
        )
        return "model_gateway_backend_auth_failed"
    if control.status_code != 200:
        set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_control_unavailable",
        )
        return "model_gateway_control_unavailable"
    if len(control.content) > _MAX_CONTROL_RESPONSE_BYTES:
        set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
        return "model_gateway_invalid_response"
    try:
        payload = control.json()
    except ValueError:
        set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
        return "model_gateway_invalid_response"
    if not isinstance(payload, dict):
        set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
        return "model_gateway_invalid_response"
    return _central_contract_code(payload, runtime, settings)


def _central_contract_code(
    payload: dict[str, Any],
    runtime: Any,
    settings: Settings,
) -> str:
    expected_kinds = {
        runtime.route_for("chat"): "chat",
        runtime.route_for("memory.extract"): "chat",
        runtime.route_for("memory.compact"): "chat",
        runtime.route_for("memory.core"): "chat",
        runtime.route_for("memory.review"): "chat",
    }
    knowledge_kinds = {
        runtime.route_for("knowledge.fast"): "chat",
        runtime.route_for("knowledge.pro"): "chat",
    }
    if settings.knowledge_agent_egress_policy != "none":
        expected_kinds.update(knowledge_kinds)
    embedding_route_id = runtime.route_for("memory.embedding")
    expected_count = (
        7 if settings.knowledge_agent_egress_policy != "none" else 5
    )
    if len(expected_kinds) != expected_count or embedding_route_id in expected_kinds:
        return "model_gateway_route_contract_invalid"
    raw_routes = payload.get("routes")
    raw_deployments = payload.get("deployments")
    raw_connections = payload.get("connections")
    if not all(isinstance(items, list) for items in (raw_routes, raw_deployments, raw_connections)):
        set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
        return "model_gateway_invalid_response"
    embedding_contract = resolve_embedding_contract(settings, payload)
    routes = {
        str(item.get("id")): item
        for item in raw_routes
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    visible_routes = set(routes)
    required_routes = set(expected_kinds)
    allowed_routes = required_routes | set(knowledge_kinds) | {
        embedding_route_id
    }
    if not required_routes.issubset(visible_routes) or not visible_routes.issubset(
        allowed_routes
    ):
        return "model_gateway_route_visibility_mismatch"
    deployments = {
        str(item.get("id")): item
        for item in raw_deployments
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    connections = {
        str(item.get("id")): item
        for item in raw_connections
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    for route_id, expected_kind in expected_kinds.items():
        route = routes[route_id]
        targets = route.get("targets")
        if (
            route.get("kind") != expected_kind
            or route.get("enabled") is not True
            or not isinstance(targets, list)
            or not targets
        ):
            return "model_gateway_route_contract_invalid"
        usable = False
        for target in targets:
            deployment = deployments.get(str(target))
            if not deployment or deployment.get("kind") != expected_kind:
                return "model_gateway_route_contract_invalid"
            connection = connections.get(str(deployment.get("connection") or ""))
            usable = usable or bool(
                deployment.get("enabled") is True
                and connection
                and connection.get("enabled") is True
                and connection.get("configured") is True
                and connection.get("usage_scope") == "backend_allowed"
            )
        if not usable:
            return "model_gateway_route_unavailable"
    if embedding_contract.state in {"invalid", "unavailable"}:
        return embedding_contract.code
    return ""
