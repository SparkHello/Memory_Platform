from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from model_gateway.auth import provider_secret_header_value

from model_gateway.config_store import (
    ConfigManager,
    ControlPlaneCommit,
    GatewayPaths,
    commit_control_plane,
    source_revision,
    validate_control_plane_snapshot,
)
from model_gateway.http_safety import normalize_base_url
from model_gateway.ids import default_secret_ref, slug_id, unique_id
from model_gateway_contracts import (
    AdapterName,
    AuthConfig,
    BillingPlan,
    BillingPlanType,
    Capabilities,
    ClientConfig,
    ConnectionConfig,
    DeploymentConfig,
    derive_embedding_space,
    GatewayConfig,
    PricingConfig,
    RequestTransform,
    RouteConfig,
    StrictModel,
    validate_id,
)


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


def _valid_revision(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("revision 格式无效")
    return normalized


class ConnectionCreateRequest(StrictModel):
    """Create one upstream connection; its secret is written separately."""

    revision: str
    channel_operator: str
    adapter: AdapterName = "generic"
    base_url: str
    plan: BillingPlanType = "payg"
    dry_run: bool = False

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        return _valid_revision(value)


class DeploymentDraft(StrictModel):
    upstream_model: str
    model_author: str = ""
    kind: Literal["chat", "embedding"] = "chat"
    adapter_profile: Literal["inherit", "dashscope_deepseek_v4"] = "inherit"
    reasoning_default: Literal["inherit", "enabled", "disabled"] = "inherit"
    tool_choice_with_reasoning: Literal["any", "auto_only", "none"] = "auto_only"
    capabilities: Capabilities = Field(default_factory=Capabilities)
    dimensions: int | None = Field(default=None, ge=1, le=65536)
    embedding_space: str = ""


class RouteAssignment(StrictModel):
    """Point a route at deployments; ``$<index>`` references a draft."""

    id: str
    kind: Literal["chat", "embedding"] = "chat"
    targets: list[str] = Field(min_length=1)
    max_attempts: int = Field(default=1, ge=1, le=20)
    fallback_scope: Literal["none", "same_channel", "any_channel"] = "none"
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
    """Create deployments on an existing connection and assign routes."""

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


class BundleConnectionDraft(StrictModel):
    id: str = ""
    channel_operator: str
    adapter: AdapterName = "generic"
    base_url: str
    secret: str = Field(min_length=1, max_length=65536)
    auth_type: Literal["bearer", "x-api-key"] = "bearer"
    plan: BillingPlanType = "payg"
    usage_scope: Literal["backend_allowed", "interactive_only", "disabled"] = (
        "backend_allowed"
    )
    allowed_private_networks: list[str] = Field(default_factory=list)
    connect_timeout_seconds: float = Field(default=10.0, ge=0.1, le=3600.0)
    read_timeout_seconds: float = Field(default=120.0, ge=0.1, le=3600.0)
    write_timeout_seconds: float = Field(default=60.0, ge=0.1, le=3600.0)
    pool_timeout_seconds: float = Field(default=10.0, ge=0.1, le=3600.0)
    response_limit_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=1024,
        le=256 * 1024 * 1024,
    )
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def valid_optional_id(cls, value: str) -> str:
        return validate_id(value, "connection") if value.strip() else ""

    @field_validator("secret")
    @classmethod
    def safe_secret(cls, value: str) -> str:
        return provider_secret_header_value(value)


class BundleDeploymentDraft(DeploymentDraft):
    id: str = ""
    pricing: str | None = None
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def valid_optional_id(cls, value: str) -> str:
        return validate_id(value, "deployment") if value.strip() else ""


class BundlePricingDraft(StrictModel):
    id: str
    value: PricingConfig

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validate_id(value, "pricing")


class BundleRouteOperation(StrictModel):
    id: str
    operation: Literal["keep", "prepend", "append", "replace"] = "keep"
    kind: Literal["chat", "embedding"] = "chat"
    targets: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=1, ge=1, le=20)
    fallback_scope: Literal["none", "same_channel", "any_channel"] = "none"
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return validate_id(value, "route")

    @model_validator(mode="after")
    def targets_match_operation(self) -> "BundleRouteOperation":
        if self.operation != "keep" and not self.targets:
            raise ValueError("非 keep route operation 至少需要一个 target")
        return self


class BundleApplyRequest(StrictModel):
    revision: str
    connection: BundleConnectionDraft
    embedding_base_url: str = Field(default="", max_length=2048)
    deployments: list[BundleDeploymentDraft] = Field(default_factory=list)
    pricing: list[BundlePricingDraft] = Field(default_factory=list)
    routes: list[BundleRouteOperation] = Field(default_factory=list)

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        return _valid_revision(value)

    @field_validator("embedding_base_url")
    @classmethod
    def stripped_embedding_base_url(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def unique_ids(self) -> "BundleApplyRequest":
        for label, values in (
            ("deployment", [item.id for item in self.deployments if item.id]),
            ("pricing", [item.id for item in self.pricing]),
            ("route", [item.id for item in self.routes]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"bundle 含重复 {label} ID")
        return self


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
    return GatewayConfig.model_validate(payload), changed, warnings


def connection_candidate(
    config: GatewayConfig,
    request: ConnectionCreateRequest,
) -> tuple[GatewayConfig, str]:
    operator = request.channel_operator.strip().lower()
    connection_id = unique_id(slug_id(f"{operator}-account"), config.connections)
    connection = ConnectionConfig(
        channel_operator=operator,
        adapter=request.adapter,
        base_url=request.base_url,
        auth=AuthConfig(
            type="bearer",
            secret_ref=default_secret_ref("CONNECTION", connection_id),
        ),
        billing_plan=BillingPlan(type=request.plan),
        usage_scope="backend_allowed",
    )
    payload = config.model_dump(mode="python", exclude_none=False)
    payload["connections"] = {
        **payload["connections"],
        connection_id: connection.model_dump(mode="python"),
    }
    return GatewayConfig.model_validate(payload), connection_id


def deployment_candidate(
    config: GatewayConfig,
    request: DeploymentApplyRequest,
) -> tuple[GatewayConfig, list[str], list[str], list[str]]:
    connection = config.connections.get(request.connection)
    if connection is None:
        raise ValueError(f"未知 connection：{request.connection}")

    payload = config.model_dump(mode="python", exclude_none=False)
    deployments = dict(payload["deployments"])
    deployment_ids: list[str] = []
    for draft in request.deployments:
        deployment_id = unique_id(
            slug_id(f"{request.connection}-{draft.upstream_model}"),
            deployments,
        )
        deployment = DeploymentConfig(
            connection=request.connection,
            upstream_model=draft.upstream_model,
            model_author=(draft.model_author.strip() or "unknown"),
            kind=draft.kind,
            adapter_profile=draft.adapter_profile,
            reasoning_default=draft.reasoning_default,
            tool_choice_with_reasoning=draft.tool_choice_with_reasoning,
            capabilities=draft.capabilities,
            request_transform=RequestTransform(),
            dimensions=draft.dimensions,
            embedding_space=(
                draft.embedding_space.strip()
                or (
                    derive_embedding_space(
                        connection,
                        draft.upstream_model,
                        int(draft.dimensions),
                    )
                    if draft.kind == "embedding" and draft.dimensions is not None
                    else ""
                )
            ),
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
                fallback_scope=assignment.fallback_scope,
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
            "max_attempts": assignment.max_attempts,
            "fallback_scope": assignment.fallback_scope,
            "enabled": assignment.enabled,
        }
    payload["routes"] = routes
    return GatewayConfig.model_validate(payload), deployment_ids, changed, warnings


def bundle_candidate(
    config: GatewayConfig,
    request: BundleApplyRequest,
) -> tuple[GatewayConfig, str, str, list[str], list[str], str]:
    """Build one fully validated graph without persisting its candidate key."""

    payload = config.model_dump(mode="python", exclude_none=False)
    operator = request.connection.channel_operator.strip().lower()
    connection_id = request.connection.id or unique_id(
        slug_id(f"{operator}-account"), config.connections
    )
    existing_connection = config.connections.get(connection_id)
    secret_ref = (
        existing_connection.auth.secret_ref
        if existing_connection is not None
        else default_secret_ref("CONNECTION", connection_id)
    )
    connection_payload = (
        existing_connection.model_dump(mode="python", exclude_none=False)
        if existing_connection is not None
        else {}
    )
    supplied = request.connection.model_fields_set
    overridable: tuple[tuple[str, str], ...] = (
        ("adapter", "adapter"),
        ("allowed_private_networks", "allowed_private_networks"),
        ("auth_type", "auth.type"),
        ("plan", "billing_plan.type"),
        ("usage_scope", "usage_scope"),
        ("connect_timeout_seconds", "connect_timeout_seconds"),
        ("read_timeout_seconds", "read_timeout_seconds"),
        ("write_timeout_seconds", "write_timeout_seconds"),
        ("pool_timeout_seconds", "pool_timeout_seconds"),
        ("response_limit_bytes", "response_limit_bytes"),
        ("enabled", "enabled"),
    )

    def dig(source: object, dotted: str) -> Any:
        value = source
        for part in dotted.split("."):
            value = getattr(value, part)
        return value

    merged = {
        name: (
            dig(existing_connection, persisted)
            if existing_connection is not None and name not in supplied
            else dig(request.connection, name)
        )
        for name, persisted in overridable
    }
    candidate_connection = ConnectionConfig(
        channel_operator=operator,
        adapter=merged["adapter"],
        allowed_private_networks=merged["allowed_private_networks"],
        base_url=request.connection.base_url,
        auth=AuthConfig(type=merged["auth_type"], secret_ref=secret_ref),
        models_endpoint=(
            existing_connection.models_endpoint
            if existing_connection is not None
            else "/models"
        ),
        chat_endpoint=(
            existing_connection.chat_endpoint
            if existing_connection is not None
            else "/chat/completions"
        ),
        embeddings_endpoint=(
            existing_connection.embeddings_endpoint
            if existing_connection is not None
            else "/embeddings"
        ),
        forward_headers=(
            list(existing_connection.forward_headers)
            if existing_connection is not None
            else []
        ),
        billing_plan=BillingPlan(type=merged["plan"]),
        usage_scope=merged["usage_scope"],
        connect_timeout_seconds=merged["connect_timeout_seconds"],
        read_timeout_seconds=merged["read_timeout_seconds"],
        write_timeout_seconds=merged["write_timeout_seconds"],
        pool_timeout_seconds=merged["pool_timeout_seconds"],
        response_limit_bytes=merged["response_limit_bytes"],
        rate_limit_cooldown_seconds=(
            existing_connection.rate_limit_cooldown_seconds
            if existing_connection is not None
            else 300.0
        ),
        enabled=merged["enabled"],
    )
    connection_payload.update(
        candidate_connection.model_dump(mode="python", exclude_none=False)
    )
    payload["connections"] = {
        **payload["connections"],
        connection_id: connection_payload,
    }

    embedding_connection_id, embedding_connection = _embedding_target_connection(
        request,
        operator=operator,
        chat_connection_id=connection_id,
        chat_connection=candidate_connection,
        connections_payload=payload["connections"],
    )
    if embedding_connection_id != connection_id:
        payload["connections"][embedding_connection_id] = (
            embedding_connection.model_dump(mode="python", exclude_none=False)
        )

    pricing_records = dict(payload["pricing"])
    for draft in request.pricing:
        pricing_records[draft.id] = draft.value.model_dump(
            mode="python", exclude_none=False
        )
    payload["pricing"] = pricing_records

    deployments = dict(payload["deployments"])
    deployment_ids: list[str] = []
    for draft in request.deployments:
        target_connection_id = (
            embedding_connection_id if draft.kind == "embedding" else connection_id
        )
        target_connection = (
            embedding_connection
            if draft.kind == "embedding"
            else candidate_connection
        )
        deployment_id = draft.id or unique_id(
            slug_id(f"{target_connection_id}-{draft.upstream_model}"), deployments
        )
        current = config.deployments.get(deployment_id)
        if current is not None and current.connection != target_connection_id:
            raise ValueError(
                f"deployment {deployment_id} 属于其他 connection，不能在 bundle 中接管"
            )
        derived_space = (
            derive_embedding_space(
                target_connection,
                draft.upstream_model,
                int(draft.dimensions),
            )
            if draft.kind == "embedding" and draft.dimensions is not None
            else ""
        )
        unchanged_embedding_identity = bool(
            current is not None
            and current.kind == "embedding"
            and draft.kind == "embedding"
            and current.upstream_model == draft.upstream_model
            and current.dimensions == draft.dimensions
            and config.connections[current.connection].channel_operator
            == target_connection.channel_operator
            and config.connections[current.connection].base_url
            == target_connection.base_url
        )
        deployment = DeploymentConfig(
            connection=target_connection_id,
            upstream_model=draft.upstream_model,
            model_author=(draft.model_author.strip() or "unknown"),
            model_family=current.model_family if current is not None else "",
            kind=draft.kind,
            adapter_profile=draft.adapter_profile,
            reasoning_default=draft.reasoning_default,
            tool_choice_with_reasoning=draft.tool_choice_with_reasoning,
            capabilities=draft.capabilities,
            request_transform=(
                current.request_transform if current is not None else RequestTransform()
            ),
            dimensions=draft.dimensions,
            embedding_space=(
                draft.embedding_space.strip()
                or (
                    current.embedding_space
                    if unchanged_embedding_identity and current is not None
                    else derived_space
                )
            ),
            pricing=draft.pricing,
            enabled=draft.enabled,
        )
        deployments[deployment_id] = deployment.model_dump(
            mode="python", exclude_none=False
        )
        deployment_ids.append(deployment_id)
    payload["deployments"] = deployments

    def resolve_target(target: str) -> str:
        match = re.fullmatch(r"\$(\d+)", target)
        if match is not None:
            index = int(match.group(1))
            if index >= len(deployment_ids):
                raise ValueError(f"route target 引用了不存在的部署占位：{target}")
            return deployment_ids[index]
        return validate_id(target, "deployment")

    routes = dict(payload["routes"])
    changed_routes: list[str] = []
    for operation in request.routes:
        if operation.operation == "keep":
            continue
        added = [resolve_target(target) for target in operation.targets]
        existing = config.routes.get(operation.id)
        if existing is not None and existing.kind != operation.kind:
            raise ValueError(
                f"route {operation.id} 已存在且 kind 为 {existing.kind}"
            )
        current_targets = list(existing.targets) if existing is not None else []
        if operation.operation == "prepend":
            targets = _ordered_unique([*added, *current_targets])
        elif operation.operation == "append":
            targets = _ordered_unique([*current_targets, *added])
        else:
            targets = _ordered_unique(added)
        required = (
            list(existing.required_capabilities) if existing is not None else []
        )
        routes[operation.id] = RouteConfig(
            kind=operation.kind,
            targets=targets,
            required_capabilities=required,
            max_attempts=operation.max_attempts,
            fallback_scope=operation.fallback_scope,
            enabled=operation.enabled,
        ).model_dump(mode="python", exclude_none=False)
        changed_routes.append(operation.id)
    payload["routes"] = routes
    candidate = GatewayConfig.model_validate(payload)
    return (
        candidate,
        connection_id,
        secret_ref,
        deployment_ids,
        changed_routes,
        embedding_connection_id,
    )


def _embedding_target_connection(
    request: BundleApplyRequest,
    *,
    operator: str,
    chat_connection_id: str,
    chat_connection: ConnectionConfig,
    connections_payload: dict[str, Any],
) -> tuple[str, ConnectionConfig]:
    raw = request.embedding_base_url.strip()
    has_embedding = any(item.kind == "embedding" for item in request.deployments)
    if raw and not has_embedding:
        raise ValueError("embedding_base_url 仅在同时配置向量模型时有效")
    if not raw or not has_embedding:
        return chat_connection_id, chat_connection
    try:
        embedding_url = normalize_base_url(
            raw,
            allowed_private_networks=chat_connection.allowed_private_networks,
        )
    except ValueError as exc:
        raise ValueError(f"embedding_base_url 无效：{exc}") from exc
    if embedding_url == chat_connection.base_url:
        return chat_connection_id, chat_connection
    embedding_connection_id = unique_id(
        slug_id(f"{operator}-embedding-account"),
        connections_payload,
    )
    return (
        embedding_connection_id,
        chat_connection.model_copy(update={"base_url": embedding_url}),
    )


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


_CONFIG_COLLECTIONS = frozenset(
    {"clients", "connections", "deployments", "routes", "pricing"}
)


@dataclass(frozen=True, slots=True)
class ControlPlaneSnapshot:
    """One config/secret pair and the config revision it was built from."""

    config: GatewayConfig
    secrets: dict[str, str]
    revision: str


@dataclass(frozen=True, slots=True)
class ControlPlaneCandidate:
    """A fully validated change awaiting the authoritative revision CAS."""

    expected_revision: str
    config: GatewayConfig | None
    secret_updates: dict[str, str | None]
    effective_config: GatewayConfig
    effective_secrets: dict[str, str]


class ControlPlaneService:
    """Framework-neutral owner of control-plane candidates and commits.

    HTTP request parsing, CLI aliases, prompts and discovery stay in adapters.
    This service owns the shared mutation contract: build a complete graph,
    validate it together with the candidate credential snapshot, then hand the
    exact candidate and expected revision to the crash-safe store primitive.
    """

    def __init__(
        self,
        paths: GatewayPaths,
        *,
        manager: ConfigManager | None = None,
    ) -> None:
        self.paths = paths
        self._manager = manager or ConfigManager(paths)

    def snapshot(self) -> ControlPlaneSnapshot:
        config, secrets = self._manager.snapshot()
        return self.from_loaded(config=config, secrets=secrets)

    def from_loaded(
        self,
        *,
        config: GatewayConfig,
        secrets: Mapping[str, str],
    ) -> ControlPlaneSnapshot:
        return ControlPlaneSnapshot(
            config=config,
            secrets=dict(secrets),
            revision=source_revision(config, self.paths.config),
        )

    def prepare(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        expected_revision: str | None = None,
        config: GatewayConfig | Mapping[str, Any] | None = None,
        secret_updates: Mapping[str, str | None] | None = None,
    ) -> ControlPlaneCandidate:
        candidate_config = (
            snapshot.config
            if config is None
            else GatewayConfig.model_validate(
                config.model_dump(mode="python", exclude_none=False)
                if isinstance(config, GatewayConfig)
                else config
            )
        )
        updates = dict(secret_updates or {})
        candidate_secrets = dict(snapshot.secrets)
        for name, value in updates.items():
            validate_id(name, "secret_ref")
            if value is None:
                candidate_secrets.pop(name, None)
            else:
                candidate_secrets[name] = value
        validate_control_plane_snapshot(
            current_config=snapshot.config,
            candidate_config=candidate_config,
            candidate_secrets=candidate_secrets,
        )
        return ControlPlaneCandidate(
            expected_revision=expected_revision or snapshot.revision,
            config=None if config is None else candidate_config,
            secret_updates=updates,
            effective_config=candidate_config,
            effective_secrets=candidate_secrets,
        )

    def commit(self, candidate: ControlPlaneCandidate) -> ControlPlaneCommit:
        committed = commit_control_plane(
            self.paths,
            expected_revision=candidate.expected_revision,
            config=candidate.config,
            secret_updates=candidate.secret_updates,
        )
        self._manager.force_reload()
        return committed

    def apply(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        expected_revision: str | None = None,
        config: GatewayConfig | Mapping[str, Any] | None = None,
        secret_updates: Mapping[str, str | None] | None = None,
    ) -> ControlPlaneCommit:
        return self.commit(
            self.prepare(
                snapshot,
                expected_revision=expected_revision,
                config=config,
                secret_updates=secret_updates,
            )
        )

    def replace_item(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        collection: str,
        item_id: str,
        item: Any,
        expected_revision: str | None = None,
        secret_updates: Mapping[str, str | None] | None = None,
    ) -> ControlPlaneCandidate:
        self._require_collection(collection)
        payload = snapshot.config.model_dump(mode="python", exclude_none=False)
        records = dict(payload[collection])
        records[item_id] = (
            item.model_dump(mode="python", exclude_none=False)
            if hasattr(item, "model_dump")
            else item
        )
        payload[collection] = records
        return self.prepare(
            snapshot,
            expected_revision=expected_revision,
            config=GatewayConfig.model_validate(payload),
            secret_updates=secret_updates,
        )

    def upsert_graph(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        clients: Mapping[str, ClientConfig] | None = None,
        connections: Mapping[str, ConnectionConfig] | None = None,
        deployments: Mapping[str, DeploymentConfig] | None = None,
        routes: Mapping[str, RouteConfig] | None = None,
        pricing: Mapping[str, PricingConfig] | None = None,
        expected_revision: str | None = None,
        secret_updates: Mapping[str, str | None] | None = None,
    ) -> ControlPlaneCandidate:
        """Merge typed domain objects into one atomically validated graph."""

        payload = snapshot.config.model_dump(mode="python", exclude_none=False)
        updates: tuple[tuple[str, Mapping[str, Any]], ...] = (
            ("clients", clients or {}),
            ("connections", connections or {}),
            ("deployments", deployments or {}),
            ("routes", routes or {}),
            ("pricing", pricing or {}),
        )
        for collection, records in updates:
            if not records:
                continue
            payload[collection].update(
                {
                    item_id: item.model_dump(mode="python", exclude_none=False)
                    for item_id, item in records.items()
                }
            )
        return self.prepare(
            snapshot,
            expected_revision=expected_revision,
            config=GatewayConfig.model_validate(payload),
            secret_updates=secret_updates,
        )

    def remove_item(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        collection: str,
        item_id: str,
        expected_revision: str | None = None,
        delete_secret: bool = False,
    ) -> ControlPlaneCandidate:
        self._require_collection(collection)
        records = getattr(snapshot.config, collection)
        if item_id not in records:
            raise ValueError(f"{collection} 中不存在：{item_id}")
        payload = snapshot.config.model_dump(mode="python", exclude_none=False)
        del payload[collection][item_id]
        secret_updates: dict[str, str | None] = {}
        if delete_secret:
            if collection == "connections":
                secret_updates[
                    snapshot.config.connections[item_id].auth.secret_ref
                ] = None
            elif collection == "clients":
                secret_updates[snapshot.config.clients[item_id].secret_ref] = None
            else:
                raise ValueError("只有 connection/client 拥有可一并删除的密钥")
        return self.prepare(
            snapshot,
            expected_revision=expected_revision,
            config=GatewayConfig.model_validate(payload),
            secret_updates=secret_updates,
        )

    def set_enabled(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        collection: str,
        item_id: str,
        enabled: bool,
        expected_revision: str | None = None,
    ) -> ControlPlaneCandidate:
        self._require_collection(collection)
        records = getattr(snapshot.config, collection)
        if item_id not in records:
            raise ValueError(f"{collection} 中不存在：{item_id}")
        payload = snapshot.config.model_dump(mode="python", exclude_none=False)
        payload[collection][item_id]["enabled"] = enabled
        return self.prepare(
            snapshot,
            expected_revision=expected_revision,
            config=GatewayConfig.model_validate(payload),
        )

    def update_deployment(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        deployment_id: str,
        enabled: bool | None = None,
        capabilities: Mapping[str, bool] | None = None,
        expected_revision: str | None = None,
    ) -> ControlPlaneCandidate:
        """Edit an existing deployment in place; capabilities merge over the current flags."""

        if deployment_id not in snapshot.config.deployments:
            raise ValueError(f"deployments 中不存在：{deployment_id}")
        payload = snapshot.config.model_dump(mode="python", exclude_none=False)
        record = payload["deployments"][deployment_id]
        if enabled is not None:
            record["enabled"] = enabled
        if capabilities:
            record["capabilities"] = {**dict(record.get("capabilities") or {}), **dict(capabilities)}
        return self.prepare(
            snapshot,
            expected_revision=expected_revision,
            config=GatewayConfig.model_validate(payload),
        )

    def upsert_client(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        client_id: str,
        client: ClientConfig,
        expected_revision: str | None = None,
        secret_value: str | None = None,
    ) -> ControlPlaneCandidate:
        updates = (
            {client.secret_ref: secret_value}
            if secret_value is not None
            else None
        )
        return self.replace_item(
            snapshot,
            collection="clients",
            item_id=client_id,
            item=client,
            expected_revision=expected_revision,
            secret_updates=updates,
        )

    def upsert_pricing(
        self,
        snapshot: ControlPlaneSnapshot,
        *,
        pricing_id: str,
        pricing: PricingConfig,
        deployment_ids: tuple[str, ...] | list[str] = (),
        expected_revision: str | None = None,
    ) -> ControlPlaneCandidate:
        unknown = [
            deployment_id
            for deployment_id in deployment_ids
            if deployment_id not in snapshot.config.deployments
        ]
        if unknown:
            raise ValueError("未知 deployment：" + ", ".join(unknown))
        payload = snapshot.config.model_dump(mode="python", exclude_none=False)
        payload["pricing"][pricing_id] = pricing.model_dump(
            mode="python", exclude_none=False
        )
        for deployment_id in deployment_ids:
            payload["deployments"][deployment_id]["pricing"] = pricing_id
        return self.prepare(
            snapshot,
            expected_revision=expected_revision,
            config=GatewayConfig.model_validate(payload),
        )

    def route_update(
        self,
        snapshot: ControlPlaneSnapshot,
        request: RouteUpdateRequest,
    ) -> tuple[ControlPlaneCandidate, list[str], list[str]]:
        config, changed, warnings = route_candidate(snapshot.config, request)
        return (
            self.prepare(
                snapshot,
                expected_revision=request.revision,
                config=config,
            ),
            changed,
            warnings,
        )

    def connection_create(
        self,
        snapshot: ControlPlaneSnapshot,
        request: ConnectionCreateRequest,
    ) -> tuple[ControlPlaneCandidate, str]:
        config, connection_id = connection_candidate(snapshot.config, request)
        return (
            self.prepare(
                snapshot,
                expected_revision=request.revision,
                config=config,
            ),
            connection_id,
        )

    def deployment_apply(
        self,
        snapshot: ControlPlaneSnapshot,
        request: DeploymentApplyRequest,
    ) -> tuple[ControlPlaneCandidate, list[str], list[str], list[str]]:
        config, deployment_ids, changed, warnings = deployment_candidate(
            snapshot.config, request
        )
        return (
            self.prepare(
                snapshot,
                expected_revision=request.revision,
                config=config,
            ),
            deployment_ids,
            changed,
            warnings,
        )

    def bundle_apply(
        self,
        snapshot: ControlPlaneSnapshot,
        request: BundleApplyRequest,
    ) -> tuple[
        ControlPlaneCandidate,
        str,
        str,
        list[str],
        list[str],
        str,
    ]:
        (
            config,
            connection_id,
            secret_ref,
            deployment_ids,
            changed_routes,
            embedding_connection_id,
        ) = bundle_candidate(snapshot.config, request)
        candidate = self.prepare(
            snapshot,
            expected_revision=request.revision,
            config=config,
            secret_updates={secret_ref: request.connection.secret},
        )
        return (
            candidate,
            connection_id,
            secret_ref,
            deployment_ids,
            changed_routes,
            embedding_connection_id,
        )

    @staticmethod
    def _require_collection(collection: str) -> None:
        if collection not in _CONFIG_COLLECTIONS:
            raise ValueError(f"不支持的配置集合：{collection}")
