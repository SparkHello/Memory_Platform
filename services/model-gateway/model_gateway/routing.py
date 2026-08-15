from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import math
import re
import threading
import time
from typing import Any, Literal, Mapping

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


class RouteCapabilityUnavailable(RoutingError):
    status_code = 422

    def __init__(self, capabilities: set[str] | tuple[str, ...]):
        self.capabilities = tuple(sorted(capabilities))
        super().__init__(
            "请求需要当前 route 无法提供的能力：" + ", ".join(self.capabilities)
        )


@dataclass(frozen=True, slots=True)
class RequestRequirements:
    """Capabilities implied by one request, independent of any route defaults."""

    streaming: bool = False
    tools: bool = False
    parallel_tools: bool = False
    reasoning: bool = False
    multimodal_input: bool = False
    json_object: bool = False
    json_schema: bool = False
    reasoning_state: Literal["unspecified", "enabled", "disabled"] = "unspecified"
    tool_choice: Literal["absent", "none", "auto", "required", "specific"] = (
        "absent"
    )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        kind: Literal["chat", "embedding"],
    ) -> "RequestRequirements":
        if kind != "chat":
            return cls()

        parallel_tools = payload.get("parallel_tool_calls") is True
        tools = bool(payload.get("tools")) or parallel_tools
        response_format = payload.get("response_format")
        response_type = (
            str(response_format.get("type") or "").strip().lower()
            if isinstance(response_format, dict)
            else ""
        )
        reasoning_state = _reasoning_state(payload)
        return cls(
            streaming=payload.get("stream") is True,
            tools=tools,
            parallel_tools=parallel_tools,
            reasoning=reasoning_state == "enabled",
            multimodal_input=_uses_multimodal_input(payload.get("messages")),
            json_object=response_type == "json_object",
            json_schema=response_type == "json_schema",
            reasoning_state=reasoning_state,
            tool_choice=_tool_choice_mode(payload),
        )

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "streaming",
                "tools",
                "parallel_tools",
                "reasoning",
                "multimodal_input",
                "json_object",
                "json_schema",
            )
            if getattr(self, name)
        )

    def missing_from(self, deployment: DeploymentConfig) -> tuple[str, ...]:
        missing = list(
            name
            for name in self.required_capabilities
            if not getattr(deployment.capabilities, name)
        )
        if self._tool_choice_blocked_with_reasoning(deployment):
            missing.append("tool_choice_with_reasoning")
        return tuple(missing)

    def _tool_choice_blocked_with_reasoning(
        self,
        deployment: DeploymentConfig,
    ) -> bool:
        if self.reasoning_state == "disabled":
            return False
        reasoning_enabled = self.reasoning_state == "enabled" or (
            self.reasoning_state == "unspecified"
            and deployment.reasoning_default == "enabled"
        )
        if not reasoning_enabled:
            return False
        policy = deployment.tool_choice_with_reasoning
        if policy == "any":
            return False
        if policy == "auto_only":
            return self.tool_choice in {"required", "specific"}
        return self.tool_choice not in {"absent", "none"}


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


MODEL_NOT_FOUND_CODES = frozenset(
    {
        "deployment_not_found",
        "invalid_model",
        "invalid_model_name",
        "model_not_found",
        "model_not_found_error",
    }
)


class RuntimeHealthRegistry:
    """Process-local, bounded circuit state for upstream routing.

    Connection failures (auth, billing and rate limits) suppress every
    deployment sharing the account. Model-specific failures and repeated 5xx
    responses suppress only the affected deployment. Configuration remains the
    source of truth; this registry is intentionally ephemeral across restarts.
    """

    def __init__(
        self,
        *,
        clock: Any = time.monotonic,
        server_failure_threshold: int = 3,
        server_failure_cooldown_seconds: float = 30.0,
        deployment_cooldown_seconds: float = 300.0,
    ) -> None:
        self._clock = clock
        self._deadlines: dict[str, float] = {}
        self._lock = threading.Lock()
        self._deployment_deadlines: dict[str, float] = {}
        self._server_failures: dict[str, int] = {}
        self._server_failure_threshold = max(1, int(server_failure_threshold))
        self._server_failure_cooldown_seconds = _finite_cooldown(
            server_failure_cooldown_seconds
        )
        self._deployment_cooldown_seconds = _finite_cooldown(
            deployment_cooldown_seconds
        )

    def remaining(self, connection_id: str) -> float:
        now = self._clock()
        with self._lock:
            deadline = self._deadlines.get(connection_id, 0.0)
            if deadline <= now:
                self._deadlines.pop(connection_id, None)
                return 0.0
            return deadline - now

    def defer(self, connection_id: str, seconds: float) -> None:
        deadline = self._clock() + _finite_cooldown(seconds)
        with self._lock:
            self._deadlines[connection_id] = max(
                deadline,
                self._deadlines.get(connection_id, 0.0),
            )

    def remaining_target(self, connection_id: str, deployment_id: str) -> float:
        now = self._clock()
        with self._lock:
            connection_deadline = self._deadlines.get(connection_id, 0.0)
            deployment_deadline = self._deployment_deadlines.get(deployment_id, 0.0)
            if connection_deadline <= now:
                self._deadlines.pop(connection_id, None)
                connection_deadline = 0.0
            if deployment_deadline <= now:
                self._deployment_deadlines.pop(deployment_id, None)
                deployment_deadline = 0.0
            return max(0.0, max(connection_deadline, deployment_deadline) - now)

    def clear_connection(
        self,
        connection_id: str,
        deployment_ids: tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            self._deadlines.pop(connection_id, None)
            for deployment_id in deployment_ids:
                self._deployment_deadlines.pop(deployment_id, None)
                self._server_failures.pop(deployment_id, None)

    def available(self, target: RouteTarget) -> bool:
        return self.remaining_target(target.connection_id, target.deployment_id) <= 0

    def defer_deployment(self, deployment_id: str, seconds: float) -> None:
        deadline = self._clock() + _finite_cooldown(seconds)
        with self._lock:
            self._deployment_deadlines[deployment_id] = max(
                deadline,
                self._deployment_deadlines.get(deployment_id, 0.0),
            )

    def record_http(
        self,
        target: RouteTarget,
        *,
        status_code: int,
        error_code: str = "",
        retry_after: float = 0.0,
    ) -> None:
        if 200 <= status_code < 500:
            with self._lock:
                self._server_failures.pop(target.deployment_id, None)

        if status_code == 429:
            self.defer(
                target.connection_id,
                max(
                    _finite_cooldown(target.connection.rate_limit_cooldown_seconds),
                    _finite_cooldown(retry_after),
                ),
            )
            return
        if status_code in {401, 402}:
            self.defer(
                target.connection_id,
                max(
                    30.0,
                    _finite_cooldown(
                        target.connection.rate_limit_cooldown_seconds
                    ),
                ),
            )
            return
        if error_code in MODEL_NOT_FOUND_CODES:
            self.defer_deployment(
                target.deployment_id,
                self._deployment_cooldown_seconds,
            )
            return
        if status_code >= 500:
            with self._lock:
                failures = self._server_failures.get(target.deployment_id, 0) + 1
                self._server_failures[target.deployment_id] = failures
            if failures >= self._server_failure_threshold:
                self.defer_deployment(
                    target.deployment_id,
                    self._server_failure_cooldown_seconds,
                )


def _finite_cooldown(seconds: float) -> float:
    try:
        value = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, value))


class Router:
    def __init__(
        self,
        *,
        runtime_health: RuntimeHealthRegistry | None = None,
    ):
        self.runtime_health = runtime_health or RuntimeHealthRegistry()

    def resolve(
        self,
        *,
        requested_model: str,
        kind: Literal["chat", "embedding"],
        client: AuthenticatedClient,
        config: GatewayConfig,
        preferred_deployment: str = "",
        required_deployment: str = "",
        requirements: RequestRequirements | None = None,
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
        else:
            if route is not None and deployment_ids:
                deployment_ids = scoped_route_targets(route, config.deployments)
            if preferred_deployment in deployment_ids:
                deployment_ids.remove(preferred_deployment)
                deployment_ids.insert(0, preferred_deployment)
        eligible: list[RouteTarget] = []
        cooling: list[float] = []
        policy_blocked = False
        compatible_exists = False
        missing_capabilities: set[str] = set()
        request_requirements = requirements or RequestRequirements()
        for deployment_id in deployment_ids:
            deployment = config.deployments[deployment_id]
            connection = config.connections[deployment.connection]
            if not target_serves_kind(deployment, connection, kind):
                continue
            missing = request_requirements.missing_from(deployment)
            if missing:
                missing_capabilities.update(missing)
                continue
            compatible_exists = True
            if (
                connection.usage_scope == "interactive_only"
                and client.config.kind != "interactive"
            ):
                policy_blocked = True
                continue
            remaining = self.runtime_health.remaining_target(
                deployment.connection,
                deployment_id,
            )
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
        if not compatible_exists and missing_capabilities:
            raise RouteCapabilityUnavailable(missing_capabilities)
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


MAX_RETRY_AFTER_SECONDS = 86_400.0


def scoped_route_targets(
    route: RouteConfig,
    deployments: Mapping[str, DeploymentConfig],
) -> list[str]:
    """Apply ``fallback_scope`` to a route's configured target list.

    ``none`` keeps only the primary target; ``same_channel`` keeps the targets
    sharing the primary target's connection.  Unknown ids (impossible in a
    validated graph) are dropped instead of raising.
    """

    target_ids = list(route.targets)
    if route.fallback_scope == "none":
        return target_ids[:1]
    if route.fallback_scope == "same_channel" and target_ids:
        primary = deployments.get(target_ids[0])
        if primary is None:
            return []
        return [
            target_id
            for target_id in target_ids
            if (deployment := deployments.get(target_id)) is not None
            and deployment.connection == primary.connection
        ]
    return target_ids


def target_serves_kind(
    deployment: DeploymentConfig,
    connection: ConnectionConfig,
    kind: Literal["chat", "embedding"],
) -> bool:
    """Static target eligibility shared by routing and readiness checks."""

    return (
        deployment.enabled
        and connection.enabled
        and deployment.kind == kind
        and connection.usage_scope != "disabled"
    )


def retry_after_seconds(
    value: str,
    *,
    wall_time: float | None = None,
    cap_seconds: float = MAX_RETRY_AFTER_SECONDS,
) -> float:
    cap = float(cap_seconds)
    if not math.isfinite(cap) or cap < 0:
        raise ValueError("Retry-After cap 必须是有限非负数")
    normalized = value.strip()
    if not normalized:
        return 0.0
    try:
        seconds = float(normalized)
    except ValueError:
        pass
    else:
        return min(cap, max(0.0, seconds)) if math.isfinite(seconds) else 0.0
    try:
        instant = parsedate_to_datetime(normalized)
        if instant.tzinfo is None:
            return 0.0
        seconds = instant.timestamp() - (
            time.time() if wall_time is None else wall_time
        )
        return min(cap, max(0.0, seconds)) if math.isfinite(seconds) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def should_fail_over(status_code: int, content: bytes = b"") -> bool:
    # Only retry explicit transient provider responses. Authentication,
    # billing, redirects and model-selection failures update their breakers but
    # are returned to the caller: silently replaying a possibly private prompt
    # to another target would cross a security and cost boundary.
    return status_code in {408, 429} or status_code >= 500


def structured_error_code(content: bytes) -> str:
    """Extract only an exact provider error code; never inspect free prose."""

    if not content or len(content) > 64 * 1024:
        return ""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    containers = [error, payload] if isinstance(error, dict) else [payload]
    for container in containers:
        value = container.get("code") or container.get("type")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if re.fullmatch(r"[a-z0-9_.:-]{1,120}", normalized):
                return normalized
    return ""


def _reasoning_state(
    payload: dict[str, Any],
) -> Literal["unspecified", "enabled", "disabled"]:
    enable_thinking = payload.get("enable_thinking")
    if isinstance(enable_thinking, bool):
        return "enabled" if enable_thinking else "disabled"

    thinking = payload.get("thinking")
    if isinstance(thinking, bool):
        return "enabled" if thinking else "disabled"
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").strip().lower()
        if thinking_type:
            return (
                "disabled"
                if thinking_type in {"none", "disabled", "off"}
                else "enabled"
            )
        if thinking:
            return "enabled"

    effort = payload.get("reasoning_effort")
    if effort is not None:
        normalized = str(effort).strip().lower()
        if normalized:
            return (
                "disabled"
                if normalized in {"none", "disabled", "off"}
                else "enabled"
            )

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, bool):
        return "enabled" if reasoning else "disabled"
    if isinstance(reasoning, dict) and reasoning:
        nested_effort = str(reasoning.get("effort") or "").strip().lower()
        return (
            "disabled"
            if nested_effort in {"none", "disabled", "off"}
            else "enabled"
        )
    return "unspecified"


def _tool_choice_mode(
    payload: dict[str, Any],
) -> Literal["absent", "none", "auto", "required", "specific"]:
    if "tool_choice" not in payload:
        return "absent"
    value = payload.get("tool_choice")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "none":
            return "none"
        if normalized == "auto":
            return "auto"
        if normalized == "required":
            return "required"
    return "specific"


def _uses_multimodal_input(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if any(
            name in message
            for name in ("audio", "image", "images", "input_audio", "video")
        ):
            return True
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").strip().lower()
            if part_type in {
                "audio",
                "file",
                "image",
                "image_url",
                "input_audio",
                "input_file",
                "input_image",
                "video",
                "video_url",
            }:
                return True
            if any(
                name in part
                for name in (
                    "audio",
                    "file",
                    "image",
                    "image_url",
                    "input_audio",
                    "video",
                )
            ):
                return True
    return False
