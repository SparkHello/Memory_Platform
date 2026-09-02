from __future__ import annotations

import re
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from model_gateway.auth import AuthenticatedClient, provider_secret_header_value
from model_gateway.control_plane import (
    BundleApplyRequest,
    BundleConnectionDraft,
    BundleDeploymentDraft,
    BundlePricingDraft,
    BundleRouteOperation,
    ConnectionCreateRequest,
    DeploymentApplyRequest,
    DeploymentDraft,
    RouteAssignment,
    RouteDraft,
    RouteUpdateRequest,
    bundle_candidate,
    connection_candidate,
    deployment_candidate,
    route_candidate,
)
from model_gateway_contracts import AdapterName, Capabilities, GatewayConfig, StrictModel, validate_id


def _valid_revision(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("revision 格式无效")
    return normalized


class SecretUpdateRequest(StrictModel):
    value: str = Field(min_length=1, max_length=65536)
    revision: str = ""

    @field_validator("value")
    @classmethod
    def safe_secret(cls, value: str) -> str:
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("密钥不能包含换行或 NUL 字符")
        return provider_secret_header_value(value)

    @field_validator("revision")
    @classmethod
    def valid_optional_revision(cls, value: str) -> str:
        return _valid_revision(value) if value.strip() else ""


class CandidateDiscoverRequest(StrictModel):
    revision: str
    connection: str = ""
    value: str = Field(default="", max_length=65536)
    candidate_key: str = Field(default="", max_length=65536)
    channel_operator: str = ""
    base_url: str = ""
    adapter: AdapterName = "generic"
    dialect: AdapterName | None = None
    auth_type: Literal["bearer", "x-api-key"] = "bearer"
    allowed_private_networks: list[str] = Field(default_factory=list)
    models_endpoint: str | None = "/models"

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        return _valid_revision(value)

    @field_validator("connection")
    @classmethod
    def valid_connection(cls, value: str) -> str:
        return validate_id(value, "connection") if value.strip() else ""

    @field_validator("value", "candidate_key")
    @classmethod
    def safe_secret(cls, value: str) -> str:
        return provider_secret_header_value(value) if value else ""

    @model_validator(mode="after")
    def existing_or_draft(self) -> "CandidateDiscoverRequest":
        if bool(self.value) == bool(self.candidate_key):
            raise ValueError("必须且只能提供 candidate_key")
        if not self.connection and (
            not self.channel_operator.strip() or not self.base_url.strip()
        ):
            raise ValueError("新渠道 discovery 需要 channel_operator 和 base_url")
        return self

    @property
    def secret_value(self) -> str:
        return self.candidate_key or self.value

    @property
    def adapter_value(self) -> str:
        # Compatibility spellings terminate at this HTTP-body boundary.
        return self.dialect or self.adapter


class CapabilityProbeRequest(StrictModel):
    """Live capability probe for a candidate channel, or for an existing one.

    Either describe a candidate (``candidate_key`` + ``channel_operator`` +
    ``base_url``) or name a saved connection (``connection_id``); the latter
    reuses the stored provider secret so the console can auto-detect the flags
    of a model that is already configured without asking for the key again.
    """

    revision: str
    connection_id: str | None = None
    candidate_key: str | None = Field(default=None, min_length=1, max_length=65536)
    channel_operator: str | None = None
    base_url: str | None = None
    adapter: AdapterName = "generic"
    auth_type: Literal["bearer", "x-api-key"] = "bearer"
    allowed_private_networks: list[str] = Field(default_factory=list)
    upstream_model: str = Field(min_length=1, max_length=300)
    probes: list[
        Literal["chat", "streaming", "tools", "reasoning", "json_object"]
    ] = Field(
        default_factory=lambda: [
            "chat",
            "streaming",
            "tools",
            "reasoning",
            "json_object",
        ]
    )

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        return _valid_revision(value)

    @field_validator("candidate_key")
    @classmethod
    def safe_secret(cls, value: str) -> str:
        return provider_secret_header_value(value)

    @field_validator("channel_operator", "upstream_model")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("probes")
    @classmethod
    def unique_probes(
        cls,
        values: list[
            Literal["chat", "streaming", "tools", "reasoning", "json_object"]
        ],
    ) -> list[
        Literal["chat", "streaming", "tools", "reasoning", "json_object"]
    ]:
        if not values:
            raise ValueError("至少选择一项探测")
        if len(set(values)) != len(values):
            raise ValueError("probes 不得重复")
        return values

    @model_validator(mode="after")
    def candidate_or_existing(self) -> "CapabilityProbeRequest":
        if self.connection_id:
            if self.candidate_key or self.channel_operator or self.base_url:
                raise ValueError("connection_id 与候选渠道字段不能同时提供")
            validate_id(self.connection_id, "connection")
            return self
        missing = [
            name
            for name, value in (
                ("candidate_key", self.candidate_key),
                ("channel_operator", self.channel_operator),
                ("base_url", self.base_url),
            )
            if not (value or "").strip()
        ]
        if missing:
            raise ValueError("缺少候选渠道字段：" + ", ".join(missing) + "（或改用 connection_id）")
        return self


class RevisionRequest(StrictModel):
    revision: str

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        return _valid_revision(value)


class EnabledUpdateRequest(RevisionRequest):
    enabled: bool


class DeploymentUpdateRequest(RevisionRequest):
    """PATCH /admin/deployments/{id}: toggle enabled and/or edit capability flags.

    ``capabilities`` is a partial update: only the listed flags change. This is
    how a model added without ``tools`` gets it later, instead of delete+recreate.
    """

    enabled: bool | None = None
    capabilities: dict[str, bool] | None = None

    @field_validator("capabilities")
    @classmethod
    def known_capabilities(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("capabilities 不能为空对象")
        unknown = sorted(set(value) - set(Capabilities.model_fields))
        if unknown:
            raise ValueError("未知 capability：" + ", ".join(unknown))
        return value

    @model_validator(mode="after")
    def requires_a_change(self) -> "DeploymentUpdateRequest":
        if self.enabled is None and self.capabilities is None:
            raise ValueError("必须提供 enabled 或 capabilities 之一")
        return self


# This module is the backwards-compatible HTTP/body import boundary.  The
# canonical request DTOs and graph builders live in control_plane; re-exporting
# them keeps older callers and generated schemas stable.
__all__ = [
    "BundleApplyRequest",
    "BundleConnectionDraft",
    "BundleDeploymentDraft",
    "BundlePricingDraft",
    "BundleRouteOperation",
    "CandidateDiscoverRequest",
    "CapabilityProbeRequest",
    "ConnectionCreateRequest",
    "DeploymentApplyRequest",
    "DeploymentDraft",
    "EnabledUpdateRequest",
    "RevisionRequest",
    "RouteAssignment",
    "RouteDraft",
    "RouteUpdateRequest",
    "SecretUpdateRequest",
    "bundle_candidate",
    "connection_candidate",
    "deployment_candidate",
    "public_configuration",
    "route_candidate",
]


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

    deployment_ids = (
        set(config.deployments)
        if client.config.kind == "admin"
        else {
            deployment_id
            for route in visible_routes.values()
            for deployment_id in route.targets
        }
    )
    visible_deployments = {
        deployment_id: config.deployments[deployment_id]
        for deployment_id in deployment_ids
        if deployment_id in config.deployments
    }
    connection_ids = (
        set(config.connections)
        if client.config.kind == "admin"
        else {deployment.connection for deployment in visible_deployments.values()}
    )

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
                "allowed_private_networks": list(connection.allowed_private_networks),
                "connect_timeout_seconds": connection.connect_timeout_seconds,
                "read_timeout_seconds": connection.read_timeout_seconds,
                "write_timeout_seconds": connection.write_timeout_seconds,
                "pool_timeout_seconds": connection.pool_timeout_seconds,
                "response_limit_bytes": connection.response_limit_bytes,
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
                "adapter_profile": deployment.adapter_profile,
                "reasoning_default": deployment.reasoning_default,
                "tool_choice_with_reasoning": deployment.tool_choice_with_reasoning,
                "capabilities": deployment.capabilities.model_dump(mode="json"),
                "dimensions": deployment.dimensions,
                "embedding_space": deployment.embedding_space,
                "pricing": deployment.pricing,
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
            "fallback_scope": route.fallback_scope,
            "enabled": route.enabled,
        }
        for route_id, route in visible_routes.items()
    ]
    result = {
        "revision": revision,
        "admin_required": True,
        "connections": connections,
        "deployments": deployments,
        "routes": routes,
    }
    if client.config.kind == "admin":
        result["pricing"] = [
            {"id": pricing_id, **pricing.model_dump(mode="json", exclude_none=False)}
            for pricing_id, pricing in config.pricing.items()
        ]
    return result
