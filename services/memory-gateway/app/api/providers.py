"""Provider status and the guarded Model Gateway configuration bridge.

The browser never receives either the My_Memory backend client key or an
upstream provider secret. Read-only snapshots use My_Memory's existing backend
credential. Mutations require a separate Model Gateway ``admin`` client key,
provided by the user for that request and forwarded only to the configured
Model Gateway control endpoint when it is loopback or HTTPS.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
from ipaddress import ip_address, ip_network
import threading
import time
from typing import Annotated, Any, Literal, NamedTuple
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
import httpx
from model_gateway_contracts import (
    DEFAULT_MEMORY_CHAT_ROUTES,
    KNOWLEDGE_FAST_ROUTE,
    KNOWLEDGE_PRO_ROUTE,
    MEMORY_CHAT_ROUTE,
    MEMORY_COMPACT_ROUTE,
    MEMORY_CORE_ROUTE,
    MEMORY_EMBEDDING_ROUTE,
    MEMORY_EXTRACT_ROUTE,
    MEMORY_REVIEW_ROUTE,
)

from app.api.deps import get_settings, require_api_key
from app.config import Settings
from app.llm.embedding_contract import (
    embedding_contract_mode,
    invalidate_embedding_contract,
    refresh_embedding_contract,
    resolve_embedding_contract,
    set_embedding_contract_failure,
)
from app.llm.runtime import (
    ModelRuntime,
    ModelRuntimeConfigurationError,
    resolve_model_runtime,
)
router = APIRouter(
    prefix="/providers",
    tags=["providers"],
    dependencies=[Depends(require_api_key)],
)

# Process-local live probe cache: avoids burning tokens on every status poll.
_LIVE_PROBE_LOCK = threading.Lock()
_LIVE_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LIVE_PROBE_INFLIGHT: dict[str, "asyncio.Task[dict[str, Any]]"] = {}
_LIVE_PROBE_TTL_SECONDS = 60.0
_LIVE_PROBE_TIMEOUT_SECONDS = 12.0
_MAX_CONTROL_RESPONSE_BYTES = 2 * 1024 * 1024


ROUTE_DESCRIPTIONS: dict[str, str] = {
    "chat": "透明聊天代理",
    MEMORY_CHAT_ROUTE: "透明聊天代理",
    MEMORY_EXTRACT_ROUTE: "从对话提取长期记忆",
    MEMORY_COMPACT_ROUTE: "压缩较早的会话上下文",
    MEMORY_CORE_ROUTE: "整理核心记忆",
    MEMORY_REVIEW_ROUTE: "记忆体检与修改建议",
    KNOWLEDGE_FAST_ROUTE: "知识检索快速阶段",
    KNOWLEDGE_PRO_ROUTE: "知识检索升级阶段",
    MEMORY_EMBEDDING_ROUTE: "记忆与知识语义搜索",
    "pricing.research": "价格信息提取",
}

REQUIRED_CHAT_ROUTES = DEFAULT_MEMORY_CHAT_ROUTES

_PRIVATE_MODEL_GATEWAY_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("fc00::/7"),
)


@router.get("/status")
async def providers_status(
    settings: Annotated[Settings, Depends(get_settings)],
    live_probe: Annotated[
        bool,
        Query(
            description=(
                "若为 true，在配置已就绪时额外探测 memory.chat 上游连通性"
                "（结果缓存约 60 秒，可能产生极少量 token 费用）"
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    try:
        model_runtime = resolve_model_runtime(settings)
    except ModelRuntimeConfigurationError as exc:
        payload = _invalid_runtime_status(str(exc), settings)
        model_runtime = None
    else:
        payload = await _model_gateway_status(settings, model_runtime)
    setup = _setup_summary(payload)
    if live_probe and model_runtime is not None and setup.get("chat_ready"):
        setup["live_probe"] = await _live_upstream_probe(
            settings=settings,
            model_runtime=model_runtime,
        )
        setup["upstream_ready"] = bool(setup["live_probe"].get("ok"))
    else:
        setup["live_probe"] = None
        setup["upstream_ready"] = None
    return {**payload, "setup": setup}


@router.post("/live-probe")
async def providers_live_probe(
    settings: Annotated[Settings, Depends(get_settings)],
    force: Annotated[
        bool,
        Query(
            description="默认复用约 60 秒的缓存结果；仅显式 force=true 时强制重新探测",
        ),
    ] = False,
) -> dict[str, Any]:
    """Explicit upstream connectivity check for Console / ops."""
    try:
        model_runtime = resolve_model_runtime(settings)
    except ModelRuntimeConfigurationError as exc:
        return {
            "ok": False,
            "code": "model_runtime_configuration_error",
            "message": str(exc),
            "cached": False,
        }
    return await _live_upstream_probe(
        settings=settings,
        model_runtime=model_runtime,
        force=force,
    )


@router.post("/admin/check")
async def check_provider_admin_key(
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    response = await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="GET",
        path="/admin/configuration",
        payload=None,
    )
    if response.status_code < 300:
        return JSONResponse({"valid": True})
    return response


class _AdminProxyRoute(NamedTuple):
    method: str
    path: str
    upstream: str
    name: str
    path_params: tuple[str, ...] = ()
    body: bool = True
    description: str = ""


# Guarded admin proxy table. Every row is a pure forwarder: the endpoint only
# hands (method, fixed upstream path, optional JSON body) to
# ``_proxy_admin_request``, which enforces the three-tier boundary unchanged —
# no write with a plain GATEWAY_API_KEY or the backend client key, only the
# per-request admin key, forwarded solely to the configured Model Gateway when
# it is HTTPS or loopback/private-opt-in HTTP. Upstream paths are fixed here;
# the browser can never pick the proxy target URL.
_ADMIN_PROXY_ROUTES: tuple[_AdminProxyRoute, ...] = (
    _AdminProxyRoute(
        method="GET",
        path="/admin/configuration",
        upstream="/admin/configuration",
        name="provider_admin_configuration",
        body=False,
        description=(
            "Return the full redacted graph only after an explicit admin unlock."
        ),
    ),
    _AdminProxyRoute(
        method="POST",
        path="/channels/discover",
        upstream="/admin/channels/discover",
        name="discover_provider_channel",
    ),
    _AdminProxyRoute(
        method="POST",
        path="/channels/probe-capabilities",
        upstream="/admin/channels/probe-capabilities",
        name="probe_provider_channel_capabilities",
        description=(
            "Proxy live capability probes; Model Gateway never persists the secret."
        ),
    ),
    _AdminProxyRoute(
        method="POST",
        path="/channel-bundles/validate",
        upstream="/admin/channel-bundles/validate",
        name="validate_provider_channel_bundle",
    ),
    _AdminProxyRoute(
        method="POST",
        path="/channel-bundles/apply",
        upstream="/admin/channel-bundles/apply",
        name="apply_provider_channel_bundle",
    ),
    _AdminProxyRoute(
        method="PATCH",
        path="/connections/{connection_id}",
        upstream="/admin/connections/{connection_id}",
        name="update_provider_connection",
        path_params=("connection_id",),
    ),
    _AdminProxyRoute(
        method="PATCH",
        path="/deployments/{deployment_id}",
        upstream="/admin/deployments/{deployment_id}",
        name="update_provider_deployment",
        path_params=("deployment_id",),
    ),
    _AdminProxyRoute(
        method="DELETE",
        path="/{collection}/{item_id}",
        upstream="/admin/{collection}/{item_id}",
        name="delete_provider_object",
        path_params=("collection", "item_id"),
    ),
    _AdminProxyRoute(
        method="POST",
        path="/routes/validate",
        upstream="/admin/routes/validate",
        name="validate_provider_routes",
    ),
    _AdminProxyRoute(
        method="PUT",
        path="/routes",
        upstream="/admin/routes",
        name="apply_provider_routes",
    ),
    _AdminProxyRoute(
        method="POST",
        path="/connections",
        upstream="/admin/connections",
        name="create_provider_connection",
    ),
    _AdminProxyRoute(
        method="POST",
        path="/deployments",
        upstream="/admin/deployments",
        name="apply_provider_deployments",
    ),
    _AdminProxyRoute(
        method="PUT",
        path="/connections/{connection_id}/secret",
        upstream="/admin/connections/{connection_id}/secret",
        name="update_provider_secret",
        path_params=("connection_id",),
    ),
    _AdminProxyRoute(
        method="POST",
        path="/connections/{connection_id}/check",
        upstream="/admin/connections/{connection_id}/check",
        name="check_provider_connection",
        path_params=("connection_id",),
        body=False,
    ),
)

# Path params typed more narrowly than plain ``str`` (keeps the 422 contract).
_ADMIN_PROXY_PATH_PARAM_TYPES: dict[str, Any] = {
    "collection": Literal["connections", "deployments", "pricing"],
}


def _build_admin_proxy_endpoint(spec: _AdminProxyRoute) -> Callable[..., Any]:
    """Build one endpoint whose signature matches the former hand-written one.

    FastAPI introspects ``__signature__`` for path params, the JSON body and
    the ``X-Model-Gateway-Admin-Key`` header, so the generated endpoint keeps
    the exact validation, auth-dependency and OpenAPI shape of the old
    per-endpoint functions (``name`` preserves operationId and summary).
    """

    parameters = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=_ADMIN_PROXY_PATH_PARAM_TYPES.get(name, str),
        )
        for name in spec.path_params
    ]
    if spec.body:
        parameters.append(
            inspect.Parameter(
                "payload",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=dict[str, Any],
            )
        )
    parameters.extend(
        (
            inspect.Parameter(
                "settings",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Annotated[Settings, Depends(get_settings)],
            ),
            inspect.Parameter(
                "admin_key",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Annotated[
                    str | None,
                    Header(alias="X-Model-Gateway-Admin-Key"),
                ],
                default=None,
            ),
        )
    )

    async def admin_proxy_endpoint(**kwargs: Any) -> JSONResponse:
        quoted = {
            name: quote(str(kwargs[name]), safe="") for name in spec.path_params
        }
        return await _proxy_admin_request(
            settings=kwargs["settings"],
            admin_key=kwargs["admin_key"],
            method=spec.method,
            path=spec.upstream.format(**quoted),
            payload=kwargs["payload"] if spec.body else None,
        )

    admin_proxy_endpoint.__name__ = spec.name
    admin_proxy_endpoint.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters,
        return_annotation=JSONResponse,
    )
    return admin_proxy_endpoint


for _admin_proxy_spec in _ADMIN_PROXY_ROUTES:
    router.add_api_route(
        _admin_proxy_spec.path,
        _build_admin_proxy_endpoint(_admin_proxy_spec),
        methods=[_admin_proxy_spec.method],
        name=_admin_proxy_spec.name,
        description=_admin_proxy_spec.description or None,
    )
del _admin_proxy_spec


async def _model_gateway_status(
    settings: Settings,
    model_runtime: ModelRuntime,
) -> dict[str, Any]:
    runtime = {
        "model_gateway_enabled": True,
        "model_runtime": "central",
        "model_gateway_base_url": model_runtime.base_url,
        "required_chat_routes": _required_model_routes(model_runtime),
        "chat_source": "model_gateway",
        "knowledge_source": "model_gateway",
        "providers_path": "",
        "routes_path": "",
    }
    try:
        response = await _model_gateway_control_request(
            settings=settings,
            method="GET",
            path="/admin/configuration",
            api_key=model_runtime.api_key,
            payload=None,
            base_url=model_runtime.base_url,
        )
    except httpx.HTTPError as exc:
        set_embedding_contract_failure(
            settings,
            state="unavailable",
            code="model_gateway_unavailable",
        )
        model_runtime = resolve_model_runtime(settings)
        return {
            "runtime": runtime,
            "embedding": _runtime_embedding_status(model_runtime),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": (
                "无法连接独立 Model Gateway 的配置接口："
                f"{type(exc).__name__}。请确认 modelgw 已更新并正在运行。"
            ),
        }
    if not response.is_success:
        set_embedding_contract_failure(
            settings,
            state="unavailable",
            code=(
                "model_gateway_backend_auth_failed"
                if response.status_code in {401, 403}
                else "model_gateway_control_unavailable"
            ),
        )
        model_runtime = resolve_model_runtime(settings)
        detail, _ = _remote_error(response)
        return {
            "runtime": runtime,
            "embedding": _runtime_embedding_status(model_runtime),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": f"独立 Model Gateway 拒绝读取配置：{detail}",
        }
    if len(response.content) > _MAX_CONTROL_RESPONSE_BYTES:
        set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
        model_runtime = resolve_model_runtime(settings)
        return {
            "runtime": runtime,
            "embedding": _runtime_embedding_status(model_runtime),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": "独立 Model Gateway 配置接口响应过大",
        }
    try:
        control = response.json()
    except ValueError:
        set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
        model_runtime = resolve_model_runtime(settings)
        return {
            "runtime": runtime,
            "embedding": _runtime_embedding_status(model_runtime),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": "独立 Model Gateway 配置接口返回了无效 JSON",
        }
    if not isinstance(control, dict):
        set_embedding_contract_failure(
            settings,
            state="invalid",
            code="model_gateway_invalid_response",
        )
        model_runtime = resolve_model_runtime(settings)
        return {
            "runtime": runtime,
            "embedding": _runtime_embedding_status(model_runtime),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": "独立 Model Gateway 配置接口返回格式无效",
        }
    resolve_embedding_contract(settings, control)
    model_runtime = resolve_model_runtime(settings)
    return _status_from_control(runtime, control, model_runtime)


def _status_from_control(
    runtime: dict[str, Any],
    control: dict[str, Any],
    model_runtime: ModelRuntime,
) -> dict[str, Any]:
    raw_connections = control.get("connections")
    raw_deployments = control.get("deployments")
    raw_routes = control.get("routes")
    connections = raw_connections if isinstance(raw_connections, list) else []
    deployments = raw_deployments if isinstance(raw_deployments, list) else []
    routes = raw_routes if isinstance(raw_routes, list) else []
    connection_by_id = {
        str(item.get("id") or ""): item
        for item in connections
        if isinstance(item, dict) and item.get("id")
    }
    deployment_by_id = {
        str(item.get("id") or ""): item
        for item in deployments
        if isinstance(item, dict) and item.get("id")
    }

    provider_views = []
    for connection_id, connection in connection_by_id.items():
        models = []
        for deployment in deployments:
            if not isinstance(deployment, dict) or deployment.get("connection") != connection_id:
                continue
            models.append(
                {
                    "id": str(deployment.get("upstream_model") or deployment.get("id") or ""),
                    "kind": str(deployment.get("kind") or "chat"),
                }
            )
        provider_views.append(
            {
                "id": connection_id,
                "name": str(connection.get("channel_operator") or connection_id),
                "api_host": str(connection.get("base_url") or ""),
                "configured": bool(connection.get("configured")),
                "models": models,
            }
        )

    route_views = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        targets = []
        for raw_target in route.get("targets") or []:
            target_id = str(raw_target)
            deployment = deployment_by_id.get(target_id)
            connection = (
                connection_by_id.get(str(deployment.get("connection") or ""))
                if deployment
                else None
            )
            configured = bool(
                deployment
                and deployment.get("enabled", True)
                and connection
                and connection.get("enabled", True)
                and connection.get("configured")
                and connection.get("usage_scope") == "backend_allowed"
            )
            targets.append(
                {
                    "target": target_id,
                    "provider_id": str(deployment.get("connection") or "") if deployment else "",
                    "provider_name": (
                        str(connection.get("channel_operator") or "") if connection else ""
                    ),
                    "model": str(deployment.get("upstream_model") or "") if deployment else "",
                    "valid": deployment is not None,
                    "configured": configured,
                }
            )
        route_id = str(route.get("id") or "")
        route_views.append(
            {
                "id": route_id,
                "description": ROUTE_DESCRIPTIONS.get(route_id, ""),
                "targets": targets,
                "usable": bool(route.get("enabled", True))
                and any(target["configured"] for target in targets),
            }
        )

    embedding = _runtime_embedding_status(model_runtime)

    return {
        "runtime": runtime,
        "embedding": embedding,
        "providers": provider_views,
        "routes": route_views,
        "control": control,
        "config_error": "",
    }


def _runtime_embedding_status(model_runtime: ModelRuntime) -> dict[str, Any]:
    embedding = model_runtime.embedding
    return {
        "model": embedding.status_model or embedding.model,
        "base_url": embedding.base_url,
        "dimensions": embedding.dimensions,
        "configured": embedding.enabled,
        "space_id": embedding.space_id,
        "model_gateway_mode": embedding.model_gateway_mode,
        "mode": embedding.mode,
        "state": embedding.state,
        "code": embedding.code,
    }


def _invalid_runtime_status(error: str, settings: Settings) -> dict[str, Any]:
    return {
        "runtime": {
            "model_gateway_enabled": False,
            "model_runtime": "invalid",
            "model_gateway_base_url": "",
            "required_chat_routes": list(REQUIRED_CHAT_ROUTES),
            "chat_source": "unavailable",
            "knowledge_source": "unavailable",
            "providers_path": "",
            "routes_path": "",
        },
        "embedding": {
            "model": "",
            "base_url": "",
            "dimensions": 0,
            "configured": False,
            "space_id": "",
            "model_gateway_mode": False,
            "mode": embedding_contract_mode(settings),
            "state": "unavailable",
            "code": "model_runtime_configuration_error",
        },
        "providers": [],
        "routes": [],
        "control": None,
        "config_error": error,
    }


def _setup_summary(payload: dict[str, Any]) -> dict[str, Any]:
    routes = payload.get("routes")
    route_by_id = (
        {
            str(route.get("id") or ""): route
            for route in routes
            if isinstance(route, dict)
        }
        if isinstance(routes, list)
        else {}
    )
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    configured_required = runtime.get("required_chat_routes")
    required_routes = (
        [str(item) for item in configured_required if str(item)]
        if isinstance(configured_required, list)
        else list(REQUIRED_CHAT_ROUTES)
    )
    usable = [
        route_id
        for route_id in required_routes
        if bool(route_by_id.get(route_id, {}).get("usable"))
    ]
    missing = [route_id for route_id in required_routes if route_id not in usable]
    config_error = str(payload.get("config_error") or "")
    model_gateway_connected = bool(runtime.get("model_gateway_enabled"))
    embedding = (
        payload.get("embedding")
        if isinstance(payload.get("embedding"), dict)
        else {}
    )
    embedding_error = str(embedding.get("state") or "unavailable") in {
        "invalid",
        "unavailable",
    }
    if config_error or embedding_error:
        state = "configuration_error"
        next_action = "repair_model_gateway"
    elif missing:
        state = "needs_model"
        next_action = "configure_model"
    else:
        state = "ready"
        next_action = "connect_client"
    return {
        "state": state,
        "service_ready": not config_error and not embedding_error,
        "model_gateway_connected": model_gateway_connected,
        "chat_ready": not missing and not config_error,
        "required_chat_routes": required_routes,
        "usable_chat_routes": usable,
        "missing_chat_routes": missing,
        "next_action": next_action,
        "live_probe": None,
        "upstream_ready": None,
    }


async def _live_upstream_probe(
    *,
    settings: Settings,
    model_runtime: ModelRuntime,
    force: bool = False,
) -> dict[str, Any]:
    """Cheap end-to-end check that provider egress and SSRF allowlists work."""
    try:
        chat_route = model_runtime.route_for("chat")
    except ModelRuntimeConfigurationError as exc:
        return {
            "ok": False,
            "code": "chat_route_missing",
            "message": str(exc),
            "latency_ms": 0,
            "cached": False,
            "route": "",
        }
    cache_key = (
        f"{model_runtime.base_url}|{chat_route}|"
        f"{bool(settings.model_gateway_api_key)}"
    )
    now = time.monotonic()
    if not force:
        with _LIVE_PROBE_LOCK:
            cached = _LIVE_PROBE_CACHE.get(cache_key)
            if cached is not None and cached[0] > now:
                result = dict(cached[1])
                result["cached"] = True
                return result
        # In-flight dedup: concurrent non-forced probes join the same upstream
        # request instead of each spending tokens. Handlers share one loop, so
        # a plain dict is race-free here.
        existing = _LIVE_PROBE_INFLIGHT.get(cache_key)
        if existing is not None:
            return dict(await asyncio.shield(existing))

    probe = asyncio.ensure_future(
        _execute_live_probe(
            settings=settings,
            chat_route=chat_route,
            cache_key=cache_key,
            base_url=model_runtime.base_url,
        )
    )
    _LIVE_PROBE_INFLIGHT[cache_key] = probe
    try:
        return dict(await probe)
    finally:
        if _LIVE_PROBE_INFLIGHT.get(cache_key) is probe:
            _LIVE_PROBE_INFLIGHT.pop(cache_key, None)


async def _execute_live_probe(
    *,
    settings: Settings,
    chat_route: str,
    cache_key: str,
    base_url: str,
) -> dict[str, Any]:
    started = time.monotonic()
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = f"{base}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.model_gateway_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Prefer the stable chat route name so Model Gateway selects the configured
    # production target rather than a free-form model id.
    payload = {
        "model": chat_route,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_LIVE_PROBE_TIMEOUT_SECONDS, connect=5.0),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.post(url, headers=headers, json=payload)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body_preview = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    body_preview = str(error.get("message") or error.get("type") or "")[
                        :300
                    ]
                elif response.is_success:
                    body_preview = "ok"
        except ValueError:
            body_preview = (response.text or "")[:200]
        ok = response.is_success
        code = "ok" if ok else f"http_{response.status_code}"
        if not ok and (
            "安全校验" in body_preview
            or "198.18" in body_preview
            or "私网" in body_preview
            or "destination" in body_preview.lower()
        ):
            code = "upstream_destination_blocked"
        result = {
            "ok": ok,
            "code": code,
            "message": body_preview
            or (f"上游返回 HTTP {response.status_code}" if not ok else "上游可达"),
            "latency_ms": elapsed_ms,
            "cached": False,
            "route": chat_route,
        }
    except httpx.HTTPError as exc:
        result = {
            "ok": False,
            "code": "connect_error",
            "message": f"无法连接 Model Gateway：{type(exc).__name__}",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "cached": False,
            "route": chat_route,
        }

    with _LIVE_PROBE_LOCK:
        _LIVE_PROBE_CACHE[cache_key] = (
            time.monotonic() + _LIVE_PROBE_TTL_SECONDS,
            {k: v for k, v in result.items() if k != "cached"},
        )
        # Bound cache size for long-lived processes.
        while len(_LIVE_PROBE_CACHE) > 16:
            _LIVE_PROBE_CACHE.pop(next(iter(_LIVE_PROBE_CACHE)))
    return result


def _required_model_routes(model_runtime: ModelRuntime) -> list[str]:
    return list(
        dict.fromkeys(
            model_runtime.route_for(operation)
            for operation in DEFAULT_MEMORY_CHAT_ROUTES
        )
    )


async def _proxy_admin_request(
    *,
    settings: Settings,
    admin_key: str | None,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> JSONResponse:
    try:
        model_runtime = resolve_model_runtime(settings)
    except ModelRuntimeConfigurationError as exc:
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc)},
        )
    if not model_runtime.is_central:
        return JSONResponse(
            status_code=409,
            content={"detail": "当前未启用独立 Model Gateway，不能从此页面写入配置"},
        )
    normalized_key = (admin_key or "").strip()
    if not normalized_key:
        return JSONResponse(
            status_code=401,
            content={
                "detail": {
                    "message": (
                        "请输入 Model Gateway admin 客户端密钥后再执行配置操作"
                        "（credentials/admin.txt；与登录网页的 Console token 不是同一把钥匙）"
                    ),
                    "code": "admin_key_required",
                }
            },
        )
    if not _admin_transport_is_safe(
        model_runtime.base_url,
        allow_private_http=settings.model_gateway_allow_private_http,
    ):
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "为避免管理密钥明文出站，远程 Model Gateway 配置写入必须使用 HTTPS；"
                    "HTTP 只允许 localhost/回环地址；隔离 Docker 私网需显式开启 "
                    "MODEL_GATEWAY_ALLOW_PRIVATE_HTTP"
                )
            },
        )
    mutates_configuration = _is_configuration_mutation(method, path)
    if mutates_configuration:
        invalidate_embedding_contract(settings)
    try:
        try:
            response = await _model_gateway_control_request(
                settings=settings,
                method=method,
                path=path,
                api_key=normalized_key,
                payload=payload,
                base_url=model_runtime.base_url,
            )
        finally:
            if mutates_configuration:
                # The write may have reached Model Gateway even when its
                # response was lost. Refresh with the separate backend key;
                # never reuse the user-supplied admin credential here.
                try:
                    await refresh_embedding_contract(settings)
                except Exception:
                    set_embedding_contract_failure(
                        settings,
                        state="unavailable",
                        code="model_gateway_control_unavailable",
                    )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "detail": (
                    "无法连接独立 Model Gateway 的配置接口："
                    f"{type(exc).__name__}"
                )
            },
        )
    if response.is_success:
        try:
            data = response.json()
        except ValueError:
            return JSONResponse(
                status_code=502,
                content={"detail": "独立 Model Gateway 返回了无效 JSON"},
            )
        return JSONResponse(status_code=response.status_code, content=data)
    message, code = _remote_error(response)
    if response.status_code == 401 and not code:
        code = "admin_auth_failed"
        if not message or message.upper().startswith("HTTP "):
            message = (
                "Model Gateway 拒绝了 admin 密钥。"
                "请确认粘贴的是 credentials/admin.txt，"
                "而不是登录网页用的 Console token（gateway.txt）"
            )
    detail: dict[str, Any] = {"message": message}
    if code:
        detail["code"] = code
    return JSONResponse(status_code=response.status_code, content={"detail": detail})


def _is_configuration_mutation(method: str, path: str) -> bool:
    normalized_method = method.upper()
    if normalized_method in {"PUT", "PATCH", "DELETE"}:
        return True
    return normalized_method == "POST" and path in {
        "/admin/channel-bundles/apply",
        "/admin/connections",
        "/admin/deployments",
    }


async def _model_gateway_control_request(
    *,
    settings: Settings,
    method: str,
    path: str,
    api_key: str,
    payload: dict[str, Any] | None,
    base_url: str = "",
) -> httpx.Response:
    normalized_base_url = (base_url or settings.model_gateway_base_url).rstrip("/")
    if normalized_base_url.endswith("/v1"):
        normalized_base_url = normalized_base_url[:-3]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=min(float(settings.request_timeout_seconds), 30.0),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        request_kwargs: dict[str, Any] = {"headers": headers}
        if payload is not None:
            request_kwargs["json"] = payload
        return await client.request(
            method,
            f"{normalized_base_url}{path}",
            **request_kwargs,
        )


def _admin_transport_is_safe(
    base_url: str,
    *,
    allow_private_http: bool = False,
) -> bool:
    parsed = urlsplit(base_url)
    if parsed.scheme.lower() == "https":
        return True
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return True
    try:
        address = ip_address(hostname)
    except ValueError:
        return allow_private_http and hostname == "model-gateway"
    if address.is_loopback:
        return True
    return allow_private_http and any(
        address in network for network in _PRIVATE_MODEL_GATEWAY_NETWORKS
    )


def _remote_error(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}", ""
    if not isinstance(payload, dict):
        return f"HTTP {response.status_code}", ""
    raw = payload.get("error")
    if isinstance(raw, dict):
        return (
            str(raw.get("message") or f"HTTP {response.status_code}"),
            str(raw.get("code") or raw.get("type") or ""),
        )
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail, ""
    if isinstance(detail, dict) and detail.get("message"):
        return str(detail["message"]), str(detail.get("code") or "")
    # Channel discovery returns valid/persisted/report without an error envelope.
    if payload.get("persisted") is False and isinstance(payload.get("report"), dict):
        connections = payload["report"].get("connections")
        if isinstance(connections, list) and connections:
            first = connections[0]
            if isinstance(first, dict):
                conn_detail = first.get("detail") or first.get("status")
                if conn_detail:
                    return (
                        str(conn_detail),
                        str(first.get("status") or "channel_discovery_failed"),
                    )
        if payload.get("valid") is False:
            return (
                "渠道只读发现未通过（密钥、地址、网络或 /models 响应有问题）",
                "channel_discovery_failed",
            )
    return f"HTTP {response.status_code}", ""
