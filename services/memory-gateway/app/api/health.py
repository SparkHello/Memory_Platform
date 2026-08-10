import asyncio
from pathlib import Path
import sqlite3
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
import httpx

from app.config import Settings, get_settings
from app.disk_capacity import disk_readiness_code
from app.llm.runtime import (
    ModelRuntimeConfigurationError,
    resolve_model_runtime,
)

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


_MAX_CONTROL_RESPONSE_BYTES = 2 * 1024 * 1024


@router.get("/readyz")
async def readiness(
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Operational readiness; liveness remains dependency-free ``/health``."""
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
    if runtime.is_central:
        if not settings.gateway_signing_secret:
            return _not_ready("model_gateway_usage_attribution_unavailable")
        model_code = await _central_model_readiness_code(settings, runtime)
        if model_code:
            return _not_ready(model_code)
    return JSONResponse(
        content={
            "status": "ready",
            "model_runtime": runtime.mode,
            "embedding_enabled": runtime.embedding.enabled,
        }
    )


def _not_ready(code: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "code": code},
    )


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
        return "model_gateway_unavailable"
    if ready.status_code != 200:
        return "model_gateway_not_ready"
    try:
        ready_payload = ready.json()
    except ValueError:
        return "model_gateway_invalid_response"
    if not isinstance(ready_payload, dict) or ready_payload.get("status") != "ready":
        return "model_gateway_not_ready"
    if control.status_code in {401, 403}:
        return "model_gateway_backend_auth_failed"
    if control.status_code != 200:
        return "model_gateway_control_unavailable"
    if len(control.content) > _MAX_CONTROL_RESPONSE_BYTES:
        return "model_gateway_invalid_response"
    try:
        payload = control.json()
    except ValueError:
        return "model_gateway_invalid_response"
    if not isinstance(payload, dict):
        return "model_gateway_invalid_response"
    return _central_contract_code(payload, runtime)


def _central_contract_code(payload: dict[str, Any], runtime: Any) -> str:
    expected_kinds = {
        runtime.route_for("chat"): "chat",
        runtime.route_for("memory.extract"): "chat",
        runtime.route_for("memory.compact"): "chat",
        runtime.route_for("memory.core"): "chat",
        runtime.route_for("memory.review"): "chat",
        runtime.route_for("knowledge.fast"): "chat",
        runtime.route_for("knowledge.pro"): "chat",
        runtime.route_for("memory.embedding"): "embedding",
    }
    if len(expected_kinds) != 8:
        return "model_gateway_route_contract_invalid"
    raw_routes = payload.get("routes")
    raw_deployments = payload.get("deployments")
    raw_connections = payload.get("connections")
    if not all(isinstance(items, list) for items in (raw_routes, raw_deployments, raw_connections)):
        return "model_gateway_invalid_response"
    routes = {
        str(item.get("id")): item
        for item in raw_routes
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if set(routes) != set(expected_kinds):
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
            if expected_kind == "embedding" and (
                not runtime.embedding.enabled
                or deployment.get("embedding_space") != runtime.embedding.space_id
                or deployment.get("dimensions") != runtime.embedding.dimensions
            ):
                return "model_gateway_embedding_contract_mismatch"
            usable = usable or bool(
                deployment.get("enabled") is True
                and connection
                and connection.get("enabled") is True
                and connection.get("configured") is True
            )
        if not usable:
            return "model_gateway_route_unavailable"
    return ""
