from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from model_gateway.auth import AuthenticatedClient
from model_gateway.models import (
    AuthConfig,
    BillingPlan,
    Capabilities,
    ConnectionConfig,
    DeploymentConfig,
    GatewayConfig,
    RequestTransform,
    RouteConfig,
    StrictModel,
    validate_id,
)
from model_gateway.quickstart import _default_secret_ref, _slug, _unique_id


class RouteDraft(StrictModel):
    id: str
    targets: list[str] = Field(min_length=1)
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def valid_route_id(cls, value: str) -> str:
        return validate_id(value, "route")

    @field_validator("targets")
    @classmethod
    def valid_targets(cls, values: list[str]) -> list[str]:
        normalized = [validate_id(value, "deployment") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("route targets 不能重复")
        return normalized


class RouteUpdateRequest(StrictModel):
    revision: str
    routes: list[RouteDraft] = Field(min_length=1)

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        return _valid_revision(value)

    @model_validator(mode="after")
    def unique_routes(self) -> "RouteUpdateRequest":
        route_ids = [route.id for route in self.routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("路由草稿不能包含重复 route")
        return self


class SecretUpdateRequest(StrictModel):
    value: str = Field(min_length=1, max_length=65536)

    @field_validator("value")
    @classmethod
    def safe_secret(cls, value: str) -> str:
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("密钥不能包含换行或 NUL 字符")
        return value


def _valid_revision(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("revision 格式无效")
    return normalized


class ConnectionCreateRequest(StrictModel):
    """Create one upstream connection; the secret itself is never accepted here."""

    revision: str
    channel_operator: str
    adapter: Literal["generic", "kimi", "deepseek", "mimo"] = "generic"
    base_url: str
    plan: Literal[
        "payg",
        "subscription",
        "free_tier",
        "token_plan",
        "coding_plan",
        "direct_tool_only",
        "custom",
    ] = "payg"
    dry_run: bool = False

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        return _valid_revision(value)


class DeploymentDraft(StrictModel):
    upstream_model: str
    model_author: str = ""
    kind: Literal["chat", "embedding"] = "chat"
    reasoning_default: Literal["inherit", "enabled", "disabled"] = "inherit"
    capabilities: Capabilities = Field(default_factory=Capabilities)
    dimensions: int | None = Field(default=None, ge=1, le=65536)
    embedding_space: str = ""


class RouteAssignment(StrictModel):
    """Point a route at deployments; ``$<index>`` targets reference drafts."""

    id: str
    kind: Literal["chat", "embedding"] = "chat"
    targets: list[str] = Field(min_length=1)
    max_attempts: int = Field(default=1, ge=1, le=20)
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def valid_route_id(cls, value: str) -> str:
        return validate_id(value, "route")

    @field_validator("targets")
    @classmethod
    def valid_targets(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if re.fullmatch(r"\$\d+", value):
                normalized.append(value)
            else:
                normalized.append(validate_id(value, "deployment"))
        if len(set(normalized)) != len(normalized):
            raise ValueError("route targets 不能重复")
        return normalized


class DeploymentApplyRequest(StrictModel):
    """Create deployments on an existing connection and (re)point routes."""

    revision: str
    connection: str
    deployments: list[DeploymentDraft] = Field(min_length=1)
    routes: list[RouteAssignment] = Field(default_factory=list)
    dry_run: bool = False

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        return _valid_revision(value)

    @field_validator("connection")
    @classmethod
    def valid_connection(cls, value: str) -> str:
        return validate_id(value, "connection")


def configuration_revision(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def public_configuration(
    *,
    config: GatewayConfig,
    secrets: Mapping[str, str],
    client: AuthenticatedClient,
    revision: str,
) -> dict[str, Any]:
    if client.config.kind == "admin":
        visible_routes = dict(config.routes)
    else:
        visible_routes = {
            route_id: route
            for route_id, route in config.routes.items()
            if client.config.allows_route(route_id)
        }

    deployment_ids = {
        deployment_id
        for route in visible_routes.values()
        for deployment_id in route.targets
    }
    visible_deployments = {
        deployment_id: config.deployments[deployment_id]
        for deployment_id in deployment_ids
        if deployment_id in config.deployments
    }
    connection_ids = {
        deployment.connection for deployment in visible_deployments.values()
    }

    connections = []
    for connection_id, connection in config.connections.items():
        if connection_id not in connection_ids:
            continue
        connections.append(
            {
                "id": connection_id,
                "channel_operator": connection.channel_operator,
                "base_url": connection.base_url,
                "adapter": connection.adapter,
                "usage_scope": connection.usage_scope,
                "enabled": connection.enabled,
                "configured": bool(secrets.get(connection.auth.secret_ref, "")),
            }
        )

    deployments = []
    for deployment_id, deployment in config.deployments.items():
        if deployment_id not in visible_deployments:
            continue
        deployments.append(
            {
                "id": deployment_id,
                "connection": deployment.connection,
                "upstream_model": deployment.upstream_model,
                "model_author": deployment.model_author,
                "model_family": deployment.model_family,
                "kind": deployment.kind,
                "capabilities": deployment.capabilities.model_dump(mode="json"),
                "dimensions": deployment.dimensions,
                "embedding_space": deployment.embedding_space,
                "enabled": deployment.enabled,
            }
        )

    routes = [
        {
            "id": route_id,
            "kind": route.kind,
            "targets": list(route.targets),
            "required_capabilities": list(route.required_capabilities),
            "max_attempts": route.max_attempts,
            "enabled": route.enabled,
        }
        for route_id, route in visible_routes.items()
    ]
    return {
        "revision": revision,
        "admin_required": True,
        "connections": connections,
        "deployments": deployments,
        "routes": routes,
    }


def route_candidate(
    config: GatewayConfig,
    request: RouteUpdateRequest,
) -> tuple[GatewayConfig, list[str], list[str]]:
    route_payload = {
        route_id: route.model_dump(mode="python", exclude_none=False)
        for route_id, route in config.routes.items()
    }
    changed: list[str] = []
    warnings: list[str] = []
    for draft in request.routes:
        current = config.routes.get(draft.id)
        if current is None:
            raise ValueError(f"未知 route：{draft.id}")
        if list(current.targets) != draft.targets or current.enabled != draft.enabled:
            changed.append(draft.id)
            if current.kind == "embedding" and list(current.targets) != draft.targets:
                warnings.append(
                    f"{draft.id} 是向量路由；应用前请确认所有目标使用同一 embedding space"
                )
        route_payload[draft.id] = {
            **route_payload[draft.id],
            "targets": list(draft.targets),
            "enabled": draft.enabled,
        }

    payload = config.model_dump(mode="python", exclude_none=False)
    payload["routes"] = route_payload
    candidate = GatewayConfig.model_validate(payload)
    return candidate, changed, warnings


def connection_candidate(
    config: GatewayConfig,
    request: ConnectionCreateRequest,
) -> tuple[GatewayConfig, str]:
    """Merge one new connection into the on-disk config and validate the graph.

    Mirrors ``apply_quickstart``: the server stays authoritative over the full
    config, generates the connection id and secret_ref naming, and validates
    the whole graph once. The secret value is written separately through the
    one-way secret endpoint.
    """

    operator = request.channel_operator.strip().lower()
    connection_id = _unique_id(_slug(f"{operator}-account"), config.connections)
    connection = ConnectionConfig(
        channel_operator=operator,
        adapter=request.adapter,
        base_url=request.base_url,
        auth=AuthConfig(
            type="bearer",
            secret_ref=_default_secret_ref("CONNECTION", connection_id),
        ),
        billing_plan=BillingPlan(type=request.plan),
        usage_scope="backend_allowed",
    )
    payload = config.model_dump(mode="python", exclude_none=False)
    payload["connections"] = {
        **payload["connections"],
        connection_id: connection.model_dump(mode="python"),
    }
    candidate = GatewayConfig.model_validate(payload)
    return candidate, connection_id


def deployment_candidate(
    config: GatewayConfig,
    request: DeploymentApplyRequest,
) -> tuple[GatewayConfig, list[str], list[str], list[str]]:
    """Merge new deployments and route assignments, then validate the graph.

    Returns the candidate config, the generated deployment ids (in request
    order), the created/changed route ids, and warnings. Route targets may use
    ``$<index>`` placeholders referencing this request's deployments, since the
    ids are only generated server-side.
    """

    connection = config.connections.get(request.connection)
    if connection is None:
        raise ValueError(f"未知 connection：{request.connection}")

    payload = config.model_dump(mode="python", exclude_none=False)
    deployments = dict(payload["deployments"])
    deployment_ids: list[str] = []
    for draft in request.deployments:
        deployment_id = _unique_id(
            _slug(f"{request.connection}-{draft.upstream_model}"),
            deployments,
        )
        deployment = DeploymentConfig(
            connection=request.connection,
            upstream_model=draft.upstream_model,
            model_author=(draft.model_author.strip() or connection.channel_operator),
            kind=draft.kind,
            reasoning_default=draft.reasoning_default,
            capabilities=draft.capabilities,
            request_transform=RequestTransform(),
            dimensions=draft.dimensions,
            embedding_space=draft.embedding_space,
        )
        deployments[deployment_id] = deployment.model_dump(mode="python")
        deployment_ids.append(deployment_id)
    payload["deployments"] = deployments

    def resolve_target(target: str) -> str:
        match = re.fullmatch(r"\$(\d+)", target)
        if match is None:
            return target
        index = int(match.group(1))
        if index >= len(deployment_ids):
            raise ValueError(f"route target 引用了不存在的部署占位：{target}")
        return deployment_ids[index]

    routes = dict(payload["routes"])
    changed: list[str] = []
    warnings: list[str] = []
    for assignment in request.routes:
        targets = [resolve_target(target) for target in assignment.targets]
        existing = config.routes.get(assignment.id)
        if existing is None:
            routes[assignment.id] = RouteConfig(
                kind=assignment.kind,
                targets=targets,
                max_attempts=assignment.max_attempts,
                enabled=assignment.enabled,
            ).model_dump(mode="python")
            changed.append(assignment.id)
            continue
        if existing.kind != assignment.kind:
            raise ValueError(
                f"route {assignment.id} 已存在且 kind 为 {existing.kind}，"
                f"不能按 {assignment.kind} 指派"
            )
        if list(existing.targets) != targets:
            changed.append(assignment.id)
            if existing.kind == "embedding":
                warnings.append(
                    f"{assignment.id} 是向量路由；应用前请确认所有目标使用同一 embedding space"
                )
        routes[assignment.id] = {
            **routes[assignment.id],
            "targets": targets,
            "enabled": assignment.enabled,
        }
    payload["routes"] = routes

    candidate = GatewayConfig.model_validate(payload)
    return candidate, deployment_ids, changed, warnings
