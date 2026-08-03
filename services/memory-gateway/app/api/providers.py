"""Provider status and the guarded Model Gateway configuration bridge.

The browser never receives either the My_Memory backend client key or an
upstream provider secret. Read-only snapshots use My_Memory's existing backend
credential. Mutations require a separate Model Gateway ``admin`` client key,
provided by the user for that request and forwarded only to the configured
Model Gateway control endpoint when it is loopback or HTTPS.
"""

from __future__ import annotations

import os
from ipaddress import ip_address
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
import httpx

from app.api.deps import get_settings, require_api_key
from app.config import Settings
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


@router.get("/status")
async def providers_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    if settings.model_gateway_enabled:
        return await _model_gateway_status(settings)
    return _direct_provider_status(settings)


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


async def _model_gateway_status(settings: Settings) -> dict[str, Any]:
    runtime = {
        "model_gateway_enabled": True,
        "model_gateway_base_url": settings.model_gateway_base_url,
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
            api_key=settings.model_gateway_api_key,
            payload=None,
        )
    except httpx.HTTPError as exc:
        return {
            "runtime": runtime,
            "embedding": _settings_embedding_status(settings),
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
            "embedding": _settings_embedding_status(settings),
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
            "embedding": _settings_embedding_status(settings),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": "独立 Model Gateway 配置接口返回了无效 JSON",
        }
    if not isinstance(control, dict):
        return {
            "runtime": runtime,
            "embedding": _settings_embedding_status(settings),
            "providers": [],
            "routes": [],
            "control": None,
            "config_error": "独立 Model Gateway 配置接口返回格式无效",
        }
    return _status_from_control(settings, runtime, control)


def _status_from_control(
    settings: Settings,
    runtime: dict[str, Any],
    control: dict[str, Any],
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

    embedding = _settings_embedding_status(settings)
    embedding_route = next(
        (route for route in routes if isinstance(route, dict) and route.get("kind") == "embedding"),
        None,
    )
    if embedding_route and embedding_route.get("targets"):
        deployment = deployment_by_id.get(str(embedding_route["targets"][0]))
        if deployment:
            embedding = {
                "model": str(deployment.get("upstream_model") or embedding_route.get("id") or ""),
                "base_url": settings.model_gateway_base_url,
                "dimensions": int(deployment.get("dimensions") or settings.embedding_dimensions),
                "configured": bool(
                    connection_by_id.get(str(deployment.get("connection") or ""), {}).get(
                        "configured"
                    )
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


def _direct_provider_status(settings: Settings) -> dict[str, Any]:
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
            "model_gateway_base_url": "",
            "chat_source": "legacy_direct",
            "knowledge_source": "provider_catalog",
            "providers_path": settings.providers_path,
            "routes_path": settings.routes_path,
        },
        "embedding": _settings_embedding_status(settings),
        "providers": providers,
        "routes": route_views,
        "control": None,
        "config_error": config_error,
    }


def _settings_embedding_status(settings: Settings) -> dict[str, Any]:
    return {
        "model": settings.embedding_model,
        "base_url": settings.embedding_base_url,
        "dimensions": settings.embedding_dimensions,
        "configured": bool(settings.embedding_api_key.strip()),
    }


async def _proxy_admin_request(
    *,
    settings: Settings,
    admin_key: str | None,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> JSONResponse:
    if not settings.model_gateway_enabled:
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
    if not _admin_transport_is_safe(settings.model_gateway_base_url):
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "为避免管理密钥明文出站，远程 Model Gateway 配置写入必须使用 HTTPS；"
                    "HTTP 只允许 localhost 或回环地址"
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
) -> httpx.Response:
    base_url = settings.model_gateway_base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=min(float(settings.request_timeout_seconds), 30.0),
        follow_redirects=False,
    ) as client:
        return await client.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            json=payload,
        )


def _admin_transport_is_safe(base_url: str) -> bool:
    parsed = urlsplit(base_url)
    if parsed.scheme.lower() == "https":
        return True
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


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
