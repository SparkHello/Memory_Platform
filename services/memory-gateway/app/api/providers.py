"""Provider status and the guarded Model Gateway configuration bridge.

The browser never receives either the My_Memory backend client key or an
upstream provider secret. Read-only snapshots use My_Memory's existing backend
credential. Mutations require a separate Model Gateway ``admin`` client key,
provided by the user for that request and forwarded only to the configured
Model Gateway control endpoint when it is loopback or HTTPS.
"""

from __future__ import annotations

import os
from ipaddress import ip_address, ip_network
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
import httpx

from app.api.deps import get_settings, require_api_key
from app.config import Settings
from app.llm.runtime import (
    ModelRuntime,
    ModelRuntimeConfigurationError,
    resolve_model_runtime,
)
from app.providers.catalog import (
    ROUTE_NAMES,
    ProviderConfigError,
    load_providers,
    load_routes,
    split_target,
)


router = APIRouter(
    prefix="/providers",
    tags=["providers"],
    dependencies=[Depends(require_api_key)],
)


ROUTE_DESCRIPTIONS: dict[str, str] = {
    "chat": "透明聊天代理",
    "memory.chat": "透明聊天代理",
    "memory.extract": "从对话提取长期记忆",
    "memory.compact": "压缩较早的会话上下文",
    "memory.core": "整理核心记忆",
    "memory.review": "记忆体检与修改建议",
    "knowledge.fast": "知识检索快速阶段",
    "knowledge.pro": "知识检索升级阶段",
    "memory.embedding": "记忆与知识语义搜索",
    "pricing.research": "价格信息提取",
}

REQUIRED_CHAT_ROUTES: tuple[str, ...] = (
    "memory.chat",
    "memory.extract",
    "memory.compact",
    "memory.core",
    "memory.review",
    "knowledge.fast",
    "knowledge.pro",
)

_PRIVATE_MODEL_GATEWAY_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("fc00::/7"),
)


@router.get("/status")
async def providers_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        model_runtime = resolve_model_runtime(settings)
    except ModelRuntimeConfigurationError as exc:
        payload = _invalid_runtime_status(str(exc))
    else:
        payload = (
            await _model_gateway_status(settings, model_runtime)
            if model_runtime.is_central
            else _direct_provider_status(settings, model_runtime)
        )
    return {**payload, "setup": _setup_summary(payload)}


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


@router.get("/admin/configuration")
async def provider_admin_configuration(
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    """Return the full redacted graph only after an explicit admin unlock."""

    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="GET",
        path="/admin/configuration",
        payload=None,
    )


@router.post("/channels/discover")
async def discover_provider_channel(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="POST",
        path="/admin/channels/discover",
        payload=payload,
    )


@router.post("/channel-bundles/validate")
async def validate_provider_channel_bundle(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="POST",
        path="/admin/channel-bundles/validate",
        payload=payload,
    )


@router.post("/channel-bundles/apply")
async def apply_provider_channel_bundle(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="POST",
        path="/admin/channel-bundles/apply",
        payload=payload,
    )


@router.patch("/connections/{connection_id}")
async def update_provider_connection(
    connection_id: str,
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="PATCH",
        path=f"/admin/connections/{quote(connection_id, safe='')}",
        payload=payload,
    )


@router.patch("/deployments/{deployment_id}")
async def update_provider_deployment(
    deployment_id: str,
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="PATCH",
        path=f"/admin/deployments/{quote(deployment_id, safe='')}",
        payload=payload,
    )


@router.delete("/{collection}/{item_id}")
async def delete_provider_object(
    collection: Literal["connections", "deployments", "pricing"],
    item_id: str,
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="DELETE",
        path=f"/admin/{collection}/{quote(item_id, safe='')}",
        payload=payload,
    )


@router.post("/routes/validate")
async def validate_provider_routes(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="POST",
        path="/admin/routes/validate",
        payload=payload,
    )


@router.put("/routes")
async def apply_provider_routes(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="PUT",
        path="/admin/routes",
        payload=payload,
    )


@router.post("/connections")
async def create_provider_connection(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="POST",
        path="/admin/connections",
        payload=payload,
    )


@router.post("/deployments")
async def apply_provider_deployments(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="POST",
        path="/admin/deployments",
        payload=payload,
    )


@router.put("/connections/{connection_id}/secret")
async def update_provider_secret(
    connection_id: str,
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="PUT",
        path=f"/admin/connections/{quote(connection_id, safe='')}/secret",
        payload=payload,
    )


@router.post("/connections/{connection_id}/check")
async def check_provider_connection(
    connection_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    admin_key: Annotated[
        str | None,
        Header(alias="X-Model-Gateway-Admin-Key"),
    ] = None,
) -> JSONResponse:
    return await _proxy_admin_request(
        settings=settings,
        admin_key=admin_key,
        method="POST",
        path=f"/admin/connections/{quote(connection_id, safe='')}/check",
        payload=None,
    )


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
        detail, _ = _remote_error(response)
        return {
            "runtime": runtime,
            "embedding": _runtime_embedding_status(model_runtime),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": f"独立 Model Gateway 拒绝读取配置：{detail}",
        }
    try:
        control = response.json()
    except ValueError:
        return {
            "runtime": runtime,
            "embedding": _runtime_embedding_status(model_runtime),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": "独立 Model Gateway 配置接口返回了无效 JSON",
        }
    if not isinstance(control, dict):
        return {
            "runtime": runtime,
            "embedding": _runtime_embedding_status(model_runtime),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": "独立 Model Gateway 配置接口返回格式无效",
        }
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
                "protocol": "openai_compatible",
                "api_host": str(connection.get("base_url") or ""),
                "api_key_env": "",
                "legacy_api_key_envs": [],
                "configured": bool(connection.get("configured")),
                "models": models,
                "urls": {},
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
                "migrated": True,
            }
        )

    embedding = _runtime_embedding_status(model_runtime)
    embedding_route = next(
        (
            route
            for route in routes
            if isinstance(route, dict)
            and route.get("kind") == "embedding"
            and route.get("id") == model_runtime.embedding.model
        ),
        None,
    )
    if embedding_route and embedding_route.get("targets"):
        deployment = deployment_by_id.get(str(embedding_route["targets"][0]))
        if deployment:
            embedding = {
                **embedding,
                "model": str(deployment.get("upstream_model") or embedding_route.get("id") or ""),
                "base_url": model_runtime.base_url,
                "dimensions": int(
                    deployment.get("dimensions")
                    or model_runtime.embedding.dimensions
                ),
                "configured": model_runtime.embedding.enabled
                and bool(
                    connection_by_id.get(
                        str(deployment.get("connection") or ""), {}
                    ).get("configured")
                ),
            }

    return {
        "runtime": runtime,
        "embedding": embedding,
        "providers": provider_views,
        "routes": route_views,
        "control": control,
        "config_error": "",
    }


def _direct_provider_status(
    settings: Settings,
    model_runtime: ModelRuntime,
) -> dict[str, Any]:
    try:
        definitions = load_providers(settings.providers_path)
        routes = load_routes(settings.routes_path)
        config_error = ""
    except ProviderConfigError as exc:
        definitions, routes, config_error = {}, {}, str(exc)

    providers = [
        {
            **definition.public_dict(),
            "configured": bool(definition.resolve_api_key(os.environ)),
        }
        for definition in definitions.values()
    ]
    route_views = []
    for route_name in ROUTE_NAMES:
        targets = []
        for target in routes.get(route_name, []):
            try:
                provider_id, model_id = split_target(target)
            except ProviderConfigError:
                targets.append({"target": target, "valid": False, "configured": False})
                continue
            definition = definitions.get(provider_id)
            configured = bool(
                definition
                and definition.resolve_api_key(os.environ)
                and model_id in definition.models
            )
            targets.append(
                {
                    "target": target,
                    "provider_id": provider_id,
                    "provider_name": definition.name if definition else "",
                    "model": model_id,
                    "valid": bool(definition and model_id in definition.models),
                    "configured": configured,
                }
            )
        route_views.append(
            {
                "id": route_name,
                "description": ROUTE_DESCRIPTIONS.get(route_name, ""),
                "targets": targets,
                "usable": any(target["configured"] for target in targets),
                "migrated": route_name in {"knowledge.fast", "knowledge.pro"},
            }
        )
    return {
        "runtime": {
            "model_gateway_enabled": False,
            "model_runtime": "direct",
            "model_gateway_base_url": "",
            "required_chat_routes": list(REQUIRED_CHAT_ROUTES),
            "chat_source": "legacy_direct",
            "knowledge_source": "provider_catalog",
            "providers_path": settings.providers_path,
            "routes_path": settings.routes_path,
        },
        "embedding": _runtime_embedding_status(model_runtime),
        "providers": providers,
        "routes": route_views,
        "control": None,
        "config_error": config_error,
    }


def _runtime_embedding_status(model_runtime: ModelRuntime) -> dict[str, Any]:
    embedding = model_runtime.embedding
    return {
        "model": embedding.model,
        "base_url": embedding.base_url,
        "dimensions": embedding.dimensions,
        "configured": embedding.enabled,
        "space_id": embedding.space_id,
        "model_gateway_mode": embedding.model_gateway_mode,
    }


def _invalid_runtime_status(error: str) -> dict[str, Any]:
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
    if config_error:
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
        "service_ready": not config_error,
        "model_gateway_connected": model_gateway_connected,
        "chat_ready": not missing and not config_error,
        "required_chat_routes": required_routes,
        "usable_chat_routes": usable,
        "missing_chat_routes": missing,
        "next_action": next_action,
    }


def _required_model_routes(model_runtime: ModelRuntime) -> list[str]:
    operations = (
        "chat",
        "memory.extract",
        "memory.compact",
        "memory.core",
        "memory.review",
        "knowledge.fast",
        "knowledge.pro",
    )
    return list(
        dict.fromkeys(model_runtime.route_for(operation) for operation in operations)
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
            content={"detail": "请输入 Model Gateway admin 客户端密钥后再执行配置操作"},
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
    try:
        response = await _model_gateway_control_request(
            settings=settings,
            method=method,
            path=path,
            api_key=normalized_key,
            payload=payload,
            base_url=model_runtime.base_url,
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
    detail: dict[str, Any] = {"message": message}
    if code:
        detail["code"] = code
    return JSONResponse(status_code=response.status_code, content={"detail": detail})


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
    return f"HTTP {response.status_code}", ""
