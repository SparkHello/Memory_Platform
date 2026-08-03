from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import threading
import time
from typing import Any, Literal

from model_gateway.auth import AuthenticatedClient
from model_gateway.models import (
    ConnectionConfig,
    DeploymentConfig,
    GatewayConfig,
    RouteConfig,
)


class RoutingError(ValueError):
    status_code = 400


class RouteNotFound(RoutingError):
    status_code = 404


class RouteForbidden(RoutingError):
    status_code = 403


class RouteUnavailable(RoutingError):
    status_code = 503


class RouteAffinityUnavailable(RoutingError):
    status_code = 409


@dataclass(frozen=True, slots=True)
class RouteTarget:
    route_id: str
    deployment_id: str
    deployment: DeploymentConfig
    connection_id: str
    connection: ConnectionConfig


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    requested_model: str
    route_id: str
    route: RouteConfig | None
    targets: tuple[RouteTarget, ...]
    required_deployment: str = ""


class CooldownRegistry:
    def __init__(self, *, clock: Any = time.monotonic):
        self._clock = clock
        self._deadlines: dict[str, float] = {}
        self._lock = threading.Lock()

    def remaining(self, connection_id: str) -> float:
        now = self._clock()
        with self._lock:
            deadline = self._deadlines.get(connection_id, 0.0)
            if deadline <= now:
                self._deadlines.pop(connection_id, None)
                return 0.0
            return deadline - now

    def defer(self, connection_id: str, seconds: float) -> None:
        deadline = self._clock() + max(0.0, seconds)
        with self._lock:
            self._deadlines[connection_id] = max(
                deadline,
                self._deadlines.get(connection_id, 0.0),
            )


class Router:
    def __init__(self, cooldowns: CooldownRegistry | None = None):
        self.cooldowns = cooldowns or CooldownRegistry()

    def resolve(
        self,
        *,
        requested_model: str,
        kind: Literal["chat", "embedding"],
        client: AuthenticatedClient,
        config: GatewayConfig,
        preferred_deployment: str = "",
        required_deployment: str = "",
    ) -> ResolvedRoute:
        model_id = requested_model.strip()
        if not model_id:
            raise RouteNotFound("请求缺少 model")

        route = config.routes.get(model_id)
        route_id = model_id if route is not None else ""
        if route is not None:
            if not route.enabled or route.kind != kind:
                raise RouteNotFound(f"没有可用的 {kind} route：{model_id}")
            if not client.config.allows_route(model_id):
                raise RouteForbidden(f"client {client.id} 无权使用 route {model_id}")
            deployment_ids = list(route.targets)
        else:
            direct_id = (
                model_id.removeprefix("deployment:")
                if model_id.startswith("deployment:")
                else model_id
            )
            if direct_id not in config.deployments:
                raise RouteNotFound(f"未知模型或 route：{model_id}")
            if not client.config.allow_direct_deployments:
                raise RouteForbidden(f"client {client.id} 不允许直接选择 deployment")
            deployment_ids = [direct_id]

        required = required_deployment.strip()
        if required:
            if required not in deployment_ids:
                raise RouteAffinityUnavailable(
                    "要求的 deployment 不属于当前 route 或已超出 client 权限"
                )
            deployment_ids = [required]
        elif preferred_deployment in deployment_ids:
            deployment_ids.remove(preferred_deployment)
            deployment_ids.insert(0, preferred_deployment)
        eligible: list[RouteTarget] = []
        cooling: list[float] = []
        policy_blocked = False
        for deployment_id in deployment_ids:
            deployment = config.deployments[deployment_id]
            connection = config.connections[deployment.connection]
            if (
                not deployment.enabled
                or not connection.enabled
                or deployment.kind != kind
                or connection.usage_scope == "disabled"
            ):
                continue
            if (
                connection.usage_scope == "interactive_only"
                and client.config.kind != "interactive"
            ):
                policy_blocked = True
                continue
            remaining = self.cooldowns.remaining(deployment.connection)
            if remaining > 0:
                cooling.append(remaining)
                continue
            eligible.append(
                RouteTarget(
                    route_id=route_id,
                    deployment_id=deployment_id,
                    deployment=deployment,
                    connection_id=deployment.connection,
                    connection=connection,
                )
            )

        if eligible:
            return ResolvedRoute(
                requested_model=model_id,
                route_id=route_id,
                route=route,
                targets=tuple(eligible),
                required_deployment=required,
            )
        if policy_blocked:
            raise RouteForbidden("该 route 的连接使用条款不允许当前 backend client 调用")
        if cooling:
            if required:
                raise RouteAffinityUnavailable("要求的 deployment 当前不可用或正在冷却")
            raise RouteUnavailable(
                f"route 的连接正在限流冷却，请约 {min(cooling):.0f} 秒后重试"
            )
        if required:
            raise RouteAffinityUnavailable("要求的 deployment 当前不可用")
        raise RouteUnavailable("route 没有已启用且可用的 deployment")


def retry_after_seconds(value: str, *, wall_time: float | None = None) -> float:
    normalized = value.strip()
    if not normalized:
        return 0.0
    try:
        return max(0.0, float(normalized))
    except ValueError:
        pass
    try:
        instant = parsedate_to_datetime(normalized)
        return max(0.0, instant.timestamp() - (wall_time or time.time()))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def should_fail_over(status_code: int, content: bytes) -> bool:
    if 300 <= status_code < 400:
        return True
    if status_code in {401, 402, 404, 408, 429} or status_code >= 500:
        return True
    if status_code != 400:
        return False
    detail = content[:1000].decode("utf-8", errors="ignore").lower()
    return any(
        marker in detail
        for marker in (
            "model not found",
            "model_not_found",
            "invalid model",
            "unsupported model",
            "insufficient balance",
            "quota exceeded",
        )
    )
