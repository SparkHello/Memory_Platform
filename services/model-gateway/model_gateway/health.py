from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any, Literal, Mapping

import httpx

from model_gateway.discovery import (
    fetch_model_listing,
    parse_model_listing,
)
from model_gateway_contracts import (
    ConnectionConfig,
    DeploymentConfig,
    GatewayConfig,
)
from model_gateway.proxy import prepare_payload
from model_gateway.routing import RouteTarget
from model_gateway.upstream_executor import (
    UpstreamExecutor,
    UsageLedgerPreflightError,
)
from model_gateway.usage import UsageMetadata, UsageStore


HealthLevel = Literal["ok", "warning", "error", "skipped"]


class HealthCheckError(ValueError):
    """Raised when a requested health-check target does not exist."""


# One shared wording table for the health-check status vocabulary; the CLI and
# the interactive console both render from it.
HEALTH_STATUS_LABELS: dict[str, str] = {
    "available": "可用",
    "connected": "已连接",
    "connected_unlisted": "已连接，模型列表中未找到已填模型",
    "connected_unverified": "已连接，但无法识别模型列表",
    "check_unsupported": "渠道不提供免费检查",
    "not_configured": "缺少 API Key",
    "policy_blocked": "套餐不允许记忆服务后台使用",
    "auth_failed": "API Key 无效或无权限",
    "network_error": "网络连接失败",
    "provider_error": "渠道返回错误",
    "rate_limited": "渠道当前限流",
    "model_not_found": "真实请求未找到该模型",
    "dimension_mismatch": "向量维度与配置不一致",
    "invalid_response": "响应无法识别或超出安全上限",
    "live_ok": "真实请求成功",
    "disabled": "已禁用",
    "degraded": "部分模型可用",
    "unavailable": "不可用",
}


@dataclass(frozen=True, slots=True)
class DeploymentHealth:
    deployment_id: str
    kind: Literal["chat", "embedding"]
    upstream_model: str
    status: str
    level: HealthLevel
    detail: str
    http_status: int | None = None
    usage_ledger_status: str = ""

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
        if self.usage_ledger_status:
            result["usage_ledger_status"] = self.usage_ledger_status
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
    discovered_models: tuple[str, ...] = ()

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
        if self.discovered_models:
            result["discovered_models"] = list(self.discovered_models)
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
    usage_store: UsageStore | None = None,
    usage_client_id: str = "modelgw-health-check",
    storage_monitor: object | None = None,
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
    executor = UpstreamExecutor(transport=transport) if live else None
    try:
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
                executor=executor,
                usage_store=usage_store,
                usage_client_id=usage_client_id,
                storage_monitor=storage_monitor,
            )
            for item_id, connection in selected
        ]
        results = await asyncio.gather(*checks)
    finally:
        if executor is not None:
            await executor.aclose()
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
    executor: UpstreamExecutor | None,
    usage_store: UsageStore | None,
    usage_client_id: str,
    storage_monitor: object | None,
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
        assert executor is not None
        return await _check_live(
            config=config,
            connection_id=connection_id,
            connection=connection,
            deployments=deployments,
            secret=secret,
            timeout_seconds=timeout_seconds,
            executor=executor,
            usage_store=usage_store,
            usage_client_id=usage_client_id,
            storage_monitor=storage_monitor,
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
    # Only honor explicit usage_scope; plan type no longer blocks backend.
    return connection.usage_scope == "interactive_only"


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

    fetch = await fetch_model_listing(
        connection,
        secret,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    if fetch.status == "network_error":
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="network_error",
            level="error",
            detail="无法连接 provider 的 models endpoint",
        )
    if fetch.status == "too_large":
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="invalid_response",
            level="error",
            detail="provider 的 models 响应超过安全上限",
        )
    if fetch.status == "unsafe":
        detail = fetch.error_detail or "provider URL 未通过安全校验"
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="invalid_response",
            level="error",
            detail=detail[:500],
        )
    if fetch.status == "http":
        status, level, detail = _http_failure(fetch.http_status or 0, discovery=True)
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status=status,
            level=level,
            detail=detail,
            http_status=fetch.http_status,
        )

    listing = parse_model_listing(fetch.content)
    if listing.error:
        return _uniform_result(
            connection_id,
            connection,
            deployments,
            status="connected_unverified",
            level="warning",
            detail="连接与鉴权正常，但 models 响应无法识别；未判定模型状态",
            http_status=fetch.http_status,
        )
    model_ids = set(listing.model_ids)

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
                    http_status=fetch.http_status,
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
                    http_status=fetch.http_status,
                )
            )
    return ConnectionHealth(
        connection_id=connection_id,
        channel_operator=connection.channel_operator,
        status="connected",
        level="ok",
        detail="models endpoint 连接与鉴权正常",
        deployments=tuple(deployment_results),
        http_status=fetch.http_status,
        discovered_model_count=len(model_ids),
        discovered_models=tuple(sorted(model_ids)[:500]),
    )


async def _check_live(
    *,
    config: GatewayConfig,
    connection_id: str,
    connection: ConnectionConfig,
    deployments: list[tuple[str, DeploymentConfig]],
    secret: str,
    timeout_seconds: float,
    executor: UpstreamExecutor,
    usage_store: UsageStore | None,
    usage_client_id: str,
    storage_monitor: object | None,
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

    checked = await asyncio.gather(
        *[
            _check_live_deployment(
                config=config,
                executor=executor,
                connection=connection,
                deployment_id=deployment_id,
                deployment=deployment,
                secret=secret,
                timeout_seconds=timeout_seconds,
                usage_store=usage_store,
                usage_client_id=usage_client_id,
                storage_monitor=storage_monitor,
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
    config: GatewayConfig,
    executor: UpstreamExecutor,
    connection: ConnectionConfig,
    deployment_id: str,
    deployment: DeploymentConfig,
    secret: str,
    timeout_seconds: float,
    usage_store: UsageStore | None,
    usage_client_id: str,
    storage_monitor: object | None,
) -> DeploymentHealth:
    # The live probe must send exactly what the data plane would send for this
    # deployment: adapter quirks, request_transform and the authoritative
    # embedding dimensions all come from the shared payload builder.
    target = RouteTarget(
        route_id="health.live",
        deployment_id=deployment_id,
        deployment=deployment,
        connection_id=deployment.connection,
        connection=connection,
    )
    payload = prepare_payload(_minimal_payload(deployment), target)
    try:
        if usage_store is None:
            result = await executor.post_json(
                target=target,
                payload=payload,
                secret=secret,
                timeout_seconds=timeout_seconds,
            )
        else:
            result = await executor.post_json_accounted(
                target=target,
                payload=payload,
                secret=secret,
                timeout_seconds=timeout_seconds,
                usage_store=usage_store,
                server=config.server,
                pricing_catalog=config.pricing,
                client_id=usage_client_id,
                route_id="health.live",
                metadata=UsageMetadata(operation="health.live"),
                storage_monitor=storage_monitor,
            )
    except UsageLedgerPreflightError:
        return _deployment_result(
            deployment_id,
            deployment,
            status="invalid_response",
            level="error",
            detail="usage ledger 预检失败；未发起真实请求",
        )

    def finish(
        *,
        status: str,
        level: HealthLevel,
        detail: str,
        http_status: int | None = None,
    ) -> DeploymentHealth:
        ledger_status = result.usage_ledger_status
        if ledger_status == "incomplete":
            detail += "；usage ledger 写入失败，检查结果仍保留"
            if level == "ok":
                level = "warning"
        return _deployment_result(
            deployment_id,
            deployment,
            status=status,
            level=level,
            detail=detail,
            http_status=http_status,
            usage_ledger_status=ledger_status,
        )

    if result.trace is None:
        return finish(
            status="invalid_response",
            level="error",
            detail=result.error_detail or "真实请求 URL 或 Header 无效",
        )
    if result.error_type:
        status = (
            "invalid_response"
            if result.trace.failure_class == "response_too_large"
            else "network_error"
        )
        detail = (
            "真实请求响应超过安全上限"
            if status == "invalid_response"
            else "最小真实请求无法连接 provider"
        )
        return finish(
            status=status,
            level="error",
            detail=detail,
            http_status=result.status_code,
        )
    assert result.status_code is not None
    if not result.is_success:
        status, level, detail = _http_failure(result.status_code, discovery=False)
        return finish(
            status=status,
            level=level,
            detail=detail,
            http_status=result.status_code,
        )

    content = result.content
    if deployment.kind == "chat":
        if not _is_chat_completion_response(content):
            return finish(
                status="invalid_response",
                level="error",
                detail="真实请求成功，但响应不是可识别的 chat completion",
                http_status=result.status_code,
            )
    else:
        dimensions = _extract_embedding_dimensions(content)
        if dimensions is None:
            return finish(
                status="invalid_response",
                level="error",
                detail="真实请求成功，但响应中没有可验证的 embedding 向量",
                http_status=result.status_code,
            )
        if any(dimension != deployment.dimensions for dimension in dimensions):
            return finish(
                status="dimension_mismatch",
                level="error",
                detail=(
                    f"真实请求含非 {deployment.dimensions} 维向量，与配置的 "
                    f"{deployment.dimensions} 维不一致"
                ),
                http_status=result.status_code,
            )
    return finish(
        status="live_ok",
        level="ok",
        detail="最小真实请求成功",
        http_status=result.status_code,
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
            "dimensions": deployment.dimensions,
        }
    return payload


def _extract_embedding_dimensions(content: bytes) -> list[int] | None:
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    data = payload["data"]
    if not data:
        return None
    dimensions: list[int] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            return None
        dimensions.append(len(item["embedding"]))
    return dimensions


def _is_chat_completion_response(content: bytes) -> bool:
    try:
        payload = json.loads(content)
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
    usage_ledger_status: str = "",
) -> DeploymentHealth:
    return DeploymentHealth(
        deployment_id=deployment_id,
        kind=deployment.kind,
        upstream_model=deployment.upstream_model,
        status=status,
        level=level,
        detail=detail,
        http_status=http_status,
        usage_ledger_status=usage_ledger_status,
    )


def _aggregate_live(
    deployments: list[DeploymentHealth],
) -> tuple[str, HealthLevel, str]:
    active = [item for item in deployments if item.status != "disabled"]
    if not active:
        return "disabled", "skipped", "没有已启用的 deployment"
    successes = sum(item.status == "live_ok" for item in active)
    if successes == len(active):
        if any(item.level == "warning" for item in active):
            return (
                "live_ok",
                "warning",
                "所有最小真实请求均成功，但部分 usage ledger 写入不完整",
            )
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
