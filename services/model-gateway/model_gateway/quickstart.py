from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Literal, get_args

import httpx
from pydantic import Field, StrictBool, ValidationError, field_validator

from model_gateway.auth import (
    provider_secret_header_value,
)
from model_gateway.config_store import (
    GatewayPaths,
    load_config,
    read_secrets,
)
from model_gateway.control_plane import ControlPlaneService
from model_gateway.discovery import fetch_model_listing_sync, parse_model_listing
from model_gateway.ids import default_secret_ref, slug_id, unique_id
from model_gateway.memory_client import (
    CHAT_ROUTES,
    EMBEDDING_ROUTE,
    provision_memory_gateway_client,
)
from model_gateway_contracts import (
    ADAPTER_NAMES,
    BILLING_PLAN_TYPES,
    AdapterName,
    AuthConfig,
    BillingPlan,
    BillingPlanType,
    Capabilities,
    ConnectionConfig,
    DeploymentConfig,
    derive_embedding_space,
    RequestTransform,
    RouteConfig,
    StrictModel,
)


@dataclass(frozen=True, slots=True)
class ChannelPreset:
    id: str
    label: str
    channel_operator: str
    base_url: str
    adapter: str


CHANNEL_PRESETS: dict[str, ChannelPreset] = {
    "deepseek": ChannelPreset(
        id="deepseek",
        label="DeepSeek 官方",
        channel_operator="deepseek",
        base_url="https://api.deepseek.com",
        adapter="deepseek",
    ),
    "kimi-cn": ChannelPreset(
        id="kimi-cn",
        label="Kimi / Moonshot 中国区",
        channel_operator="moonshot",
        base_url="https://api.moonshot.cn/v1",
        adapter="kimi",
    ),
    "mimo": ChannelPreset(
        id="mimo",
        label="小米 MiMo 官方",
        channel_operator="xiaomi",
        base_url="https://api.xiaomimimo.com/v1",
        adapter="mimo",
    ),
    "dashscope-cn": ChannelPreset(
        id="dashscope-cn",
        label="阿里云百炼 / DashScope 北京区",
        channel_operator="dashscope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        adapter="dashscope_openai",
    ),
}

ChatCapabilityName = Literal[
    "tools",
    "parallel_tools",
    "reasoning",
    "multimodal_input",
    "json_object",
    "json_schema",
]
_CHAT_CAPABILITIES: tuple[str, ...] = get_args(ChatCapabilityName)


class QuickstartError(ValueError):
    pass


def get_channel_preset(preset_id: str) -> ChannelPreset:
    try:
        return CHANNEL_PRESETS[preset_id]
    except KeyError as exc:
        raise QuickstartError(f"未知渠道预设：{preset_id}") from exc


def discover_model_ids(
    *,
    base_url: str,
    api_key: str,
    transport: httpx.BaseTransport | None = None,
    allowed_private_networks: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Read an OpenAI-compatible model list without inference or redirects."""

    if not base_url.strip():
        raise QuickstartError("模型发现需要 base_url")
    if not api_key.strip():
        raise QuickstartError("模型发现需要 API Key")
    try:
        provider_secret_header_value(api_key)
    except ValueError as exc:
        raise QuickstartError(str(exc)) from exc
    fetch = fetch_model_listing_sync(
        base_url=base_url,
        api_key=api_key,
        transport=transport,
        allowed_private_networks=allowed_private_networks,
    )
    if fetch.status == "too_large":
        raise QuickstartError("渠道 /models 响应超过 2 MiB 安全上限")
    if fetch.status in {"network_error", "unsafe"}:
        raise QuickstartError(f"读取 /models 失败：{fetch.error_type}")
    status_code = fetch.http_status or 0
    if 300 <= status_code < 400:
        raise QuickstartError("渠道 /models 返回重定向；为避免凭证泄露已拒绝跟随")
    if status_code in {401, 403}:
        raise QuickstartError(f"渠道鉴权失败（HTTP {status_code}）")
    if status_code != 200:
        raise QuickstartError(f"渠道 /models 返回 HTTP {status_code}")
    listing = parse_model_listing(fetch.content)
    if listing.error == "invalid_json":
        raise QuickstartError("渠道 /models 没有返回有效 JSON")
    if listing.error == "invalid_entries":
        raise QuickstartError("渠道 /models 条目过多或模型 ID 格式无效")
    if not listing.model_ids:
        raise QuickstartError("渠道 /models 可访问，但没有解析到模型 ID")
    return tuple(sorted(listing.model_ids))


class QuickstartEmbeddingRecipe(StrictModel):
    """``embedding`` object of a quickstart recipe; both fields are required."""

    model: str = Field(min_length=1)
    dimensions: int = Field(strict=True, ge=1)
    space: str = ""
    author: str = ""


class QuickstartRecipe(StrictModel):
    """The reviewable, non-secret quickstart recipe contract.

    ``docs/ai-quickstart.schema.json`` mirrors this model for external
    tooling; when the two drift, this model is authoritative.  Secrets are
    rejected structurally: unknown fields (e.g. ``api_key``) are forbidden.
    """

    schema_version: Literal[1]
    preset: str = ""
    channel: str = ""
    base_url: str = ""
    chat_model: str
    adapter: AdapterName = "generic"
    plan: BillingPlanType = "payg"
    chat_author: str = ""
    chat_capabilities: list[ChatCapabilityName] = Field(default_factory=list)
    reasoning_default: Literal["inherit", "enabled", "disabled"] = "inherit"
    embedding: QuickstartEmbeddingRecipe | None = None
    replace_existing_routes: StrictBool = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_plain_int(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("schema_version 必须是整数")
        return value

    @field_validator("chat_capabilities")
    @classmethod
    def unique_capabilities(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("chat_capabilities 不得包含重复值")
        return values


def _recipe_validation_error(exc: ValidationError) -> QuickstartError:
    """Map recipe validation failures to safe wording (never echoing values)."""

    extras: list[str] = []
    locations: list[str] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in error.get("loc", ()))
        if error.get("type") == "extra_forbidden":
            if location and location not in extras:
                extras.append(location[:160])
        elif location and location not in locations:
            locations.append(location[:160])
    if extras:
        return QuickstartError(
            "quickstart 配置含未知字段（配置文件不得保存 API Key 或 secret）："
            + ", ".join(extras[:8])
        )
    if locations:
        return QuickstartError(
            "quickstart 配置字段未通过校验：" + ", ".join(locations[:8])
        )
    return QuickstartError("quickstart 配置未通过校验")


def load_quickstart_file(
    path: Path,
    *,
    api_key: str,
    connect_memory: bool = True,
) -> QuickstartSpec:
    """Load a reviewable, non-secret quickstart recipe.

    The provider key is deliberately a separate argument so a recipe created
    by an AI assistant remains safe to inspect, share, and commit as an
    example. Unknown fields are rejected instead of silently accepting a
    misspelling or a secret-like field such as ``api_key``.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QuickstartError(f"无法读取 quickstart 配置：{path}") from exc
    except json.JSONDecodeError as exc:
        raise QuickstartError(
            f"quickstart 配置不是有效 JSON：第 {exc.lineno} 行第 {exc.colno} 列"
        ) from exc
    if not isinstance(raw, dict):
        raise QuickstartError("quickstart 配置根节点必须是 JSON 对象")

    try:
        recipe = QuickstartRecipe.model_validate(raw)
    except ValidationError as exc:
        raise _recipe_validation_error(exc) from exc
    if recipe.preset:
        # Keeps the actionable wording instead of a bare field path.
        get_channel_preset(recipe.preset)
    fields = recipe.model_fields_set
    preset = CHANNEL_PRESETS.get(recipe.preset)
    spec = QuickstartSpec(
        channel_operator=(
            recipe.channel
            if "channel" in fields
            else (preset.channel_operator if preset else "")
        ),
        base_url=(
            recipe.base_url
            if "base_url" in fields
            else (preset.base_url if preset else "")
        ),
        chat_model=recipe.chat_model,
        api_key=api_key,
        adapter=(
            recipe.adapter
            if "adapter" in fields
            else (preset.adapter if preset else "generic")
        ),
        plan=recipe.plan,
        chat_author=recipe.chat_author,
        chat_capabilities=tuple(recipe.chat_capabilities),
        reasoning_default=recipe.reasoning_default,
        embedding_model=recipe.embedding.model if recipe.embedding else "",
        embedding_dimensions=recipe.embedding.dimensions if recipe.embedding else None,
        embedding_space=recipe.embedding.space if recipe.embedding else "",
        embedding_author=recipe.embedding.author if recipe.embedding else "",
        connect_memory=connect_memory,
        replace_existing_routes=recipe.replace_existing_routes,
    )
    spec.validate()
    return spec


@dataclass(slots=True)
class QuickstartSpec:
    """A complete, TTY-free description of a first-run model setup."""

    channel_operator: str
    base_url: str
    chat_model: str
    api_key: str
    adapter: str = "generic"
    plan: str = "payg"
    chat_author: str = ""
    chat_capabilities: tuple[str, ...] = ()
    reasoning_default: str = "inherit"
    embedding_model: str = ""
    embedding_dimensions: int | None = None
    embedding_space: str = ""
    embedding_author: str = ""
    connect_memory: bool = True
    replace_existing_routes: bool = False

    def validate(self) -> None:
        if self.adapter not in ADAPTER_NAMES:
            raise QuickstartError(f"未知 adapter：{self.adapter}")
        if self.plan not in BILLING_PLAN_TYPES:
            raise QuickstartError(f"未知套餐类型：{self.plan}")
        if not self.channel_operator.strip():
            raise QuickstartError("渠道简称不能为空")
        if not self.base_url.strip():
            raise QuickstartError("base_url 不能为空")
        if not self.chat_model.strip():
            raise QuickstartError("聊天模型 ID 不能为空")
        if not self.api_key.strip():
            raise QuickstartError("API Key 不能为空")
        try:
            provider_secret_header_value(self.api_key)
        except ValueError as exc:
            raise QuickstartError(str(exc)) from exc
        unknown = tuple(item for item in self.chat_capabilities if item not in _CHAT_CAPABILITIES)
        if unknown:
            raise QuickstartError("未知能力：" + ", ".join(unknown))
        if self.reasoning_default not in {"inherit", "enabled", "disabled"}:
            raise QuickstartError("reasoning_default 只能是 inherit/enabled/disabled")
        if self.embedding_model.strip():
            if not self.embedding_dimensions or self.embedding_dimensions < 1:
                raise QuickstartError("配置向量模型时必须给出正整数维度")


@dataclass(slots=True)
class QuickstartResult:
    connection_id: str
    chat_deployment_id: str
    chat_routes: tuple[str, ...]
    memory_client_key: str
    embedding_deployment_id: str = ""
    embedding_space: str = ""
    embedding_dimensions: int | None = None
    created_memory_client: bool = False
    warnings: list[str] = field(default_factory=list)


def apply_quickstart(paths: GatewayPaths, spec: QuickstartSpec) -> QuickstartResult:
    """Apply a first-run setup to the modelgw config.

    Pure with respect to interaction: this validates the spec, mutates the
    config graph (connection, deployments, routes, and the memory-gateway
    backend client) exactly once through the model validator, and writes the
    connection API key plus the local memory-gateway client key. It never
    prompts, never starts a service, and never touches the memgw project.
    """

    spec.validate()
    config = load_config(paths.config)
    existing_secrets = read_secrets(paths.secrets)
    existing_routes = sorted(route_id for route_id in CHAT_ROUTES if route_id in config.routes)
    if existing_routes and not spec.replace_existing_routes:
        raise QuickstartError(
            "已有文字用途路由，quickstart 未覆盖："
            + ", ".join(existing_routes)
            + "；确认替换时显式启用 replace_existing_routes"
        )

    operator = spec.channel_operator.strip().lower()
    connection_id = unique_id(slug_id(f"{operator}-account"), config.connections)
    connection_secret_ref = default_secret_ref("CONNECTION", connection_id)
    connection = ConnectionConfig(
        channel_operator=operator,
        adapter=spec.adapter,
        base_url=spec.base_url.strip(),
        auth=AuthConfig(type="bearer", secret_ref=connection_secret_ref),
        billing_plan=BillingPlan(type=spec.plan, name="default"),
        usage_scope="backend_allowed",
    )

    chat_capabilities = set(spec.chat_capabilities)
    if "parallel_tools" in chat_capabilities:
        chat_capabilities.add("tools")
    chat_deployment_id = unique_id(
        slug_id(f"{connection_id}-{spec.chat_model}"),
        config.deployments,
    )
    chat_deployment = DeploymentConfig(
        connection=connection_id,
        upstream_model=spec.chat_model.strip(),
        model_author=(spec.chat_author.strip() or "unknown"),
        kind="chat",
        reasoning_default=spec.reasoning_default,
        capabilities=Capabilities(
            streaming=True,
            tools="tools" in chat_capabilities,
            parallel_tools="parallel_tools" in chat_capabilities,
            reasoning="reasoning" in chat_capabilities,
            multimodal_input="multimodal_input" in chat_capabilities,
            json_object="json_object" in chat_capabilities,
            json_schema="json_schema" in chat_capabilities,
        ),
        request_transform=RequestTransform(),
    )

    deployments = {chat_deployment_id: chat_deployment}
    routes = {
        route_id: RouteConfig(
            kind="chat",
            targets=[chat_deployment_id],
            max_attempts=1,
        )
        for route_id in CHAT_ROUTES
    }

    embedding_deployment_id = ""
    resolved_embedding_space = ""
    if spec.embedding_model.strip():
        embedding_deployment_id = unique_id(
            slug_id(f"{connection_id}-{spec.embedding_model}"),
            {**config.deployments, chat_deployment_id: chat_deployment},
        )
        resolved_embedding_space = spec.embedding_space.strip() or derive_embedding_space(
            connection,
            spec.embedding_model.strip(),
            int(spec.embedding_dimensions),
        )
        embedding_deployment = DeploymentConfig(
            connection=connection_id,
            upstream_model=spec.embedding_model.strip(),
            model_author=(spec.embedding_author.strip() or "unknown"),
            kind="embedding",
            capabilities=Capabilities(streaming=False),
            request_transform=RequestTransform(),
            dimensions=spec.embedding_dimensions,
            embedding_space=resolved_embedding_space,
        )
        deployments[embedding_deployment_id] = embedding_deployment
        routes[EMBEDDING_ROUTE] = RouteConfig(
            kind="embedding",
            targets=[embedding_deployment_id],
            max_attempts=1,
        )

    # Memory Gateway owns the desired permissions for its managed client. A
    # standalone quickstart may bootstrap the client with the exact eight
    # defaults, but it must never rewrite an existing client's policy.
    memory_client = provision_memory_gateway_client(config, existing_secrets)

    client_secret_ref = memory_client.client.secret_ref
    memory_client_key = memory_client.key

    control_plane = ControlPlaneService(paths)
    snapshot = control_plane.from_loaded(
        config=config,
        secrets=existing_secrets,
    )
    candidate = control_plane.upsert_graph(
        snapshot,
        clients=(
            {"memory-gateway": memory_client.client}
            if memory_client.created
            else None
        ),
        connections={connection_id: connection},
        deployments=deployments,
        routes=routes,
        secret_updates={
            connection_secret_ref: spec.api_key.strip(),
            client_secret_ref: memory_client_key,
        },
    )
    control_plane.commit(candidate)

    return QuickstartResult(
        connection_id=connection_id,
        chat_deployment_id=chat_deployment_id,
        chat_routes=CHAT_ROUTES,
        memory_client_key=memory_client_key,
        embedding_deployment_id=embedding_deployment_id,
        embedding_space=resolved_embedding_space,
        embedding_dimensions=spec.embedding_dimensions if embedding_deployment_id else None,
        created_memory_client=memory_client.created,
    )
