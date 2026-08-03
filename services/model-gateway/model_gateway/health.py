from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Mapping

import httpx

from model_gateway.adapters import apply_connection_adapter
from model_gateway.models import (
    RESTRICTED_PLAN_TYPES,
    ConnectionConfig,
    DeploymentConfig,
    GatewayConfig,
)


HealthLevel = Literal["ok", "warning", "error", "skipped"]


class HealthCheckError(ValueError):
    """Raised when a requested health-check target does not exist."""


@dataclass(frozen=True, slots=True)
class DeploymentHealth:
    deployment_id: str
    kind: Literal["chat", "embedding"]
    upstream_model: str
    status: str
    level: HealthLevel
    detail: str
    http_status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "deployment_id": self.deployment_id,
            "kind": self.kind,
            "upstream_model": self.upstream_model,
            "status": self.status,
            "level": self.level,
            "detail": self.detail,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result


@dataclass(frozen=True, slots=True)
class ConnectionHealth:
    connection_id: str
    channel_operator: str
    status: str
    level: HealthLevel
    detail: str
    deployments: tuple[DeploymentHealth, ...]
    http_status: int | None = None
    discovered_model_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "connection_id": self.connection_id,
            "channel_operator": self.channel_operator,
            "status": self.status,
            "level": self.level,
            "detail": self.detail,
            "deployments": [item.as_dict() for item in self.deployments],
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.discovered_model_count is not None:
            result["discovered_model_count"] = self.discovered_model_count
        return result


@dataclass(frozen=True, slots=True)
class HealthReport:
    mode: Literal["discovery", "live"]
    connections: tuple[ConnectionHealth, ...]

    @property
    def summary(self) -> dict[str, int]:
        deployments = [
            deployment
            for connection in self.connections
            for deployment in connection.deployments
        ]
        return {
            "connections": len(self.connections),
            "deployments": len(deployments),
            "ok": sum(item.level == "ok" for item in deployments),
            "warnings": sum(item.level == "warning" for item in deployments),
            "errors": sum(item.level == "error" for item in deployments),
            "skipped": sum(item.level == "skipped" for item in deployments),
        }

    @property
    def has_errors(self) -> bool:
        return any(
            connection.level == "error"
            or any(item.level == "error" for item in connection.deployments)
            for connection in self.connections
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "summary": self.summary,
            "connections": [item.as_dict() for item in self.connections],
        }


async def check_health(
    *,
    config: GatewayConfig,
    secrets: Mapping[str, str],
    connection_id: str = "",
    live: bool = False,
    client_kind: str = "backend",
    timeout_seconds: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HealthReport:
    """Check configured connections without exposing credentials or provider bodies.

    Discovery mode makes at most one ``GET /models`` request per connection. A
    deployment absent from that response is only ``connected_unlisted``: model
    discovery endpoints are not authoritative deprecation registries.

    Live mode is explicit and sends one minimal inference request per enabled
    deployment. Restricted plans and interactive-only connections are never
    probed on behalf of a backend (or admin) workload.
    """

    selected = _select_connections(config, connection_id)
    timeout = max(0.1, float(timeout_seconds))
    checks = [
        _check_connection(
            config=config,
            secrets=secrets,
            connection_id=item_id,
            connection=connection,
            live=live,
            client_kind=client_kind,
            timeout_seconds=timeout,
            transport=transport,
        )
        for item_id, connection in selected
    ]
    results = await asyncio.gather(*checks)
    return HealthReport(
        mode="live" if live else "discovery",
        connections=tuple(results),
    )


def _select_connections(
    config: GatewayConfig,
    connection_id: str,
) -> list[tuple[str, ConnectionConfig]]:
    normalized = connection_id.strip()
    if not normalized:
        return list(config.connections.items())
    connection = config.connections.get(normalized)
    if connection is None:
        raise HealthCheckError(f"未知 connection：{normalized}")
    return [(normalized, connection)]


async def _check_connection(
    *,
    config: GatewayConfig,
    secrets: Mapping[str, str],
    connection_id: str,
    connection: ConnectionConfig,
    live: bool,
    client_kind: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> ConnectionHealth:
    deployments = [
        (deployment_id, deployment)
        for deployment_id, deployment in config.deployments.items()
        if deployment.connection == connection_id
    ]

    if not connection.enabled or connection.usage_scope == "disabled":
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="disabled",
            level="skipped",
            detail="connection 已禁用",
        )

    if _blocked_for_workload(connection, client_kind):
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="policy_blocked",
            level="skipped",
            detail="该连接的使用范围不允许 backend 健康检查",
        )

    secret = secrets.get(connection.auth.secret_ref, "")
    if not secret:
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="not_configured",
            level="skipped",
            detail="未配置连接密钥",
        )

    if live:
        return await _check_live(
            connection_id=connection_id,
            connection=connection,
            deployments=deployments,
            secret=secret,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
    return await _check_discovery(
        connection_id=connection_id,
        connection=connection,
        deployments=deployments,
        secret=secret,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )


def _blocked_for_workload(connection: ConnectionConfig, client_kind: str) -> bool:
    if client_kind == "interactive":
        return False
    return (
        connection.usage_scope == "interactive_only"
        or connection.billing_plan.type in RESTRICTED_PLAN_TYPES
    )


async def _check_discovery(
    *,
    connection_id: str,
    connection: ConnectionConfig,
    deployments: list[tuple[str, DeploymentConfig]],
    secret: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> ConnectionHealth:
    if connection.models_endpoint is None:
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="check_unsupported",
            level="warning",
            detail="connection 未配置 models endpoint；未发起推理",
        )

    try:
        async with _client(connection, timeout_seconds, transport) as client:
            response = await client.get(
                f"{connection.base_url}{connection.models_endpoint}",
                headers=_auth_headers(connection, secret),
            )
    except httpx.HTTPError:
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="network_error",
            level="error",
            detail="无法连接 provider 的 models endpoint",
        )

    if not response.is_success:
        status, level, detail = _http_failure(response.status_code, discovery=True)
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status=status,
            level=level,
            detail=detail,
            http_status=response.status_code,
        )

    model_ids, parseable = _extract_model_ids(response)
    if not parseable:
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="connected_unverified",
            level="warning",
            detail="连接与鉴权正常，但 models 响应无法识别；未判定模型状态",
            http_status=response.status_code,
        )

    deployment_results: list[DeploymentHealth] = []
    for deployment_id, deployment in deployments:
        if not deployment.enabled:
            deployment_results.append(
                _deployment_result(
                    deployment_id,
                    deployment,
                    status="disabled",
                    level="skipped",
                    detail="deployment 已禁用",
                )
            )
        elif deployment.upstream_model in model_ids:
            deployment_results.append(
                _deployment_result(
                    deployment_id,
                    deployment,
                    status="available",
                    level="ok",
                    detail="连接、鉴权和模型列表均正常",
                    http_status=response.status_code,
                )
            )
        else:
            deployment_results.append(
                _deployment_result(
                    deployment_id,
                    deployment,
                    status="connected_unlisted",
                    level="warning",
                    detail=(
                        "连接与鉴权正常，但模型未出现在 /models 列表中；"
                        "这不表示模型已废弃"
                    ),
                    http_status=response.status_code,
                )
            )
    return ConnectionHealth(
        connection_id=connection_id,
        channel_operator=connection.channel_operator,
        status="connected",
        level="ok",
        detail="models endpoint 连接与鉴权正常",
        deployments=tuple(deployment_results),
        http_status=response.status_code,
        discovered_model_count=len(model_ids),
    )


async def _check_live(
    *,
    connection_id: str,
    connection: ConnectionConfig,
    deployments: list[tuple[str, DeploymentConfig]],
    secret: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> ConnectionHealth:
    enabled = [item for item in deployments if item[1].enabled]
    disabled = [item for item in deployments if not item[1].enabled]
    if not enabled:
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="disabled",
            level="skipped",
            detail="connection 没有已启用的 deployment",
        )

    async with _client(connection, timeout_seconds, transport) as client:
        checked = await asyncio.gather(
            *[
                _check_live_deployment(
                    client=client,
                    connection=connection,
                    deployment_id=deployment_id,
                    deployment=deployment,
                    secret=secret,
                )
                for deployment_id, deployment in enabled
            ]
        )
    checked.extend(
        _deployment_result(
            deployment_id,
            deployment,
            status="disabled",
            level="skipped",
            detail="deployment 已禁用",
        )
        for deployment_id, deployment in disabled
    )
    order = {deployment_id: index for index, (deployment_id, _) in enumerate(deployments)}
    checked.sort(key=lambda item: order[item.deployment_id])
    status, level, detail = _aggregate_live(checked)
    return ConnectionHealth(
        connection_id=connection_id,
        channel_operator=connection.channel_operator,
        status=status,
        level=level,
        detail=detail,
        deployments=tuple(checked),
    )


async def _check_live_deployment(
    *,
    client: httpx.AsyncClient,
    connection: ConnectionConfig,
    deployment_id: str,
    deployment: DeploymentConfig,
    secret: str,
) -> DeploymentHealth:
    endpoint = (
        connection.chat_endpoint
        if deployment.kind == "chat"
        else connection.embeddings_endpoint
    )
    payload = _minimal_payload(deployment)
    apply_connection_adapter(
        payload,
        connection=connection,
        deployment=deployment,
    )
    transform = deployment.request_transform
    for name in transform.remove:
        payload.pop(name, None)
    for name, value in transform.set_if_missing.items():
        payload.setdefault(name, deepcopy(value))
    for name, value in transform.force.items():
        payload[name] = deepcopy(value)
    try:
        response = await client.post(
            f"{connection.base_url}{endpoint}",
            headers=_auth_headers(connection, secret),
            json=payload,
        )
    except httpx.HTTPError:
        return _deployment_result(
            deployment_id,
            deployment,
            status="network_error",
            level="error",
            detail="最小真实请求无法连接 provider",
        )

    if not response.is_success:
        status, level, detail = _http_failure(response.status_code, discovery=False)
        return _deployment_result(
            deployment_id,
            deployment,
            status=status,
            level=level,
            detail=detail,
            http_status=response.status_code,
        )

    if deployment.kind == "chat":
        if not _is_chat_completion_response(response):
            return _deployment_result(
                deployment_id,
                deployment,
                status="invalid_response",
                level="error",
                detail="真实请求成功，但响应不是可识别的 chat completion",
                http_status=response.status_code,
            )
    else:
        dimension = _extract_embedding_dimension(response)
        if dimension is None:
            return _deployment_result(
                deployment_id,
                deployment,
                status="invalid_response",
                level="error",
                detail="真实请求成功，但响应中没有可验证的 embedding 向量",
                http_status=response.status_code,
            )
        if dimension != deployment.dimensions:
            return _deployment_result(
                deployment_id,
                deployment,
                status="dimension_mismatch",
                level="error",
                detail=(
                    f"真实请求返回 {dimension} 维向量，与配置的 "
                    f"{deployment.dimensions} 维不一致"
                ),
                http_status=response.status_code,
            )
    return _deployment_result(
        deployment_id,
        deployment,
        status="live_ok",
        level="ok",
        detail="最小真实请求成功",
        http_status=response.status_code,
    )


def _minimal_payload(deployment: DeploymentConfig) -> dict[str, Any]:
    if deployment.kind == "chat":
        payload: dict[str, Any] = {
            "model": deployment.upstream_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
    else:
        payload = {
            "model": deployment.upstream_model,
            "input": ["ping"],
        }
    return payload


def _client(
    connection: ConnectionConfig,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    timeout = min(timeout_seconds, connection.timeout_seconds)
    arguments: dict[str, Any] = {
        # A health probe must not carry an upstream credential to another host.
        "follow_redirects": False,
        "timeout": httpx.Timeout(timeout),
    }
    if transport is not None:
        arguments["transport"] = transport
    return httpx.AsyncClient(**arguments)


def _auth_headers(connection: ConnectionConfig, secret: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if connection.auth.type == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    else:
        headers["X-Api-Key"] = secret
    return headers


def _extract_model_ids(response: httpx.Response) -> tuple[set[str], bool]:
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError, RecursionError):
        return set(), False

    candidates: Any
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        candidates = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
        candidates = payload["models"]
    else:
        return set(), False

    model_ids: set[str] = set()
    for item in candidates:
        if isinstance(item, str) and item.strip():
            model_ids.add(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        value = item.get("id") or item.get("model") or item.get("name")
        if isinstance(value, str) and value.strip():
            model_ids.add(value.strip())
    return model_ids, True


def _extract_embedding_dimension(response: httpx.Response) -> int | None:
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    data = payload["data"]
    if not data or not isinstance(data[0], dict):
        return None
    embedding = data[0].get("embedding")
    return len(embedding) if isinstance(embedding, list) else None


def _is_chat_completion_response(response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("choices"), list)


def _http_failure(
    status_code: int,
    *,
    discovery: bool,
) -> tuple[str, HealthLevel, str]:
    if status_code in {401, 403}:
        return "auth_failed", "error", f"provider 拒绝鉴权（HTTP {status_code}）"
    if status_code == 429:
        return "rate_limited", "warning", "provider 当前限流（HTTP 429）"
    if discovery and status_code in {404, 405}:
        return (
            "check_unsupported",
            "warning",
            f"provider 不支持 models endpoint（HTTP {status_code}）；未发起推理",
        )
    if not discovery and status_code == 404:
        return "model_not_found", "error", "真实请求未找到该模型（HTTP 404）"
    return "provider_error", "error", f"provider 检查失败（HTTP {status_code}）"


def _uniform_result(
    connection_id: str,
    connection: ConnectionConfig,
    deployments: list[tuple[str, DeploymentConfig]],
    *,
    status: str,
    level: HealthLevel,
    detail: str,
    http_status: int | None = None,
) -> ConnectionHealth:
    items = tuple(
        _deployment_result(
            deployment_id,
            deployment,
            status="disabled" if not deployment.enabled else status,
            level="skipped" if not deployment.enabled else level,
            detail="deployment 已禁用" if not deployment.enabled else detail,
            http_status=http_status if deployment.enabled else None,
        )
        for deployment_id, deployment in deployments
    )
    return ConnectionHealth(
        connection_id=connection_id,
        channel_operator=connection.channel_operator,
        status=status,
        level=level,
        detail=detail,
        deployments=items,
        http_status=http_status,
    )


def _deployment_result(
    deployment_id: str,
    deployment: DeploymentConfig,
    *,
    status: str,
    level: HealthLevel,
    detail: str,
    http_status: int | None = None,
) -> DeploymentHealth:
    return DeploymentHealth(
        deployment_id=deployment_id,
        kind=deployment.kind,
        upstream_model=deployment.upstream_model,
        status=status,
        level=level,
        detail=detail,
        http_status=http_status,
    )


def _aggregate_live(
    deployments: list[DeploymentHealth],
) -> tuple[str, HealthLevel, str]:
    active = [item for item in deployments if item.status != "disabled"]
    if not active:
        return "disabled", "skipped", "没有已启用的 deployment"
    successes = sum(item.status == "live_ok" for item in active)
    if successes == len(active):
        return "live_ok", "ok", "所有已启用 deployment 的最小真实请求均成功"
    if successes:
        level: HealthLevel = (
            "error" if any(item.level == "error" for item in active) else "warning"
        )
        return "degraded", level, f"{successes}/{len(active)} 个真实请求成功"
    statuses = {item.status for item in active}
    if len(statuses) == 1:
        item = active[0]
        return item.status, item.level, "所有已启用 deployment 检查结果相同"
    level = "error" if any(item.level == "error" for item in active) else "warning"
    return "unavailable", level, "所有已启用 deployment 的真实请求均未成功"
