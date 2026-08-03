from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping

from pydantic import Field, field_validator, model_validator

from model_gateway.auth import AuthenticatedClient
from model_gateway.models import GatewayConfig, StrictModel, validate_id


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
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("revision 格式无效")
        return normalized

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
