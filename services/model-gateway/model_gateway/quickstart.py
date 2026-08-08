from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
from typing import Any

import httpx

from model_gateway.config_store import (
    GatewayPaths,
    load_config,
    read_secrets,
    set_secret,
    write_config,
)
from model_gateway.models import (
    AuthConfig,
    BillingPlan,
    Capabilities,
    ClientConfig,
    ConnectionConfig,
    DeploymentConfig,
    GatewayConfig,
    RequestTransform,
    RouteConfig,
    validate_id,
)


# The eight stable business routes the memory service expects. A first run
# points every chat purpose at a single deployment so one model can carry all
# text work; the user can split purposes later without touching code.
CHAT_ROUTES: tuple[str, ...] = (
    "memory.chat",
    "memory.extract",
    "memory.compact",
    "memory.core",
    "memory.review",
    "knowledge.fast",
    "knowledge.pro",
)
EMBEDDING_ROUTE = "memory.embedding"
QUICKSTART_FILE_SCHEMA_VERSION = 1


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
        adapter="generic",
    ),
}

_ADAPTERS = ("generic", "kimi", "deepseek", "mimo")
_PLANS = (
    "payg",
    "subscription",
    "free_tier",
    "token_plan",
    "coding_plan",
    "direct_tool_only",
    "custom",
)
_CHAT_CAPABILITIES = (
    "tools",
    "parallel_tools",
    "reasoning",
    "multimodal_input",
    "json_object",
    "json_schema",
)


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
) -> tuple[str, ...]:
    """Read an OpenAI-compatible model list without inference or redirects."""

    if not base_url.strip():
        raise QuickstartError("模型发现需要 base_url")
    if not api_key.strip():
        raise QuickstartError("模型发现需要 API Key")
    try:
        with httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(10.0),
        ) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key.strip()}"},
            )
    except httpx.HTTPError as exc:
        raise QuickstartError(f"读取 /models 失败：{type(exc).__name__}") from exc
    if 300 <= response.status_code < 400:
        raise QuickstartError("渠道 /models 返回重定向；为避免凭证泄露已拒绝跟随")
    if response.status_code in {401, 403}:
        raise QuickstartError(f"渠道鉴权失败（HTTP {response.status_code}）")
    if response.status_code != 200:
        raise QuickstartError(f"渠道 /models 返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise QuickstartError("渠道 /models 没有返回有效 JSON") from exc
    if isinstance(payload, dict):
        items = payload.get("data", payload.get("models", []))
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    if not isinstance(items, list):
        items = []
    model_ids: set[str] = set()
    for item in items:
        if isinstance(item, str) and item.strip():
            model_ids.add(item.strip())
        elif isinstance(item, dict):
            value = item.get("id") or item.get("model")
            if isinstance(value, str) and value.strip():
                model_ids.add(value.strip())
    if not model_ids:
        raise QuickstartError("渠道 /models 可访问，但没有解析到模型 ID")
    return tuple(sorted(model_ids))


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

    allowed = {
        "schema_version",
        "preset",
        "channel",
        "base_url",
        "chat_model",
        "adapter",
        "plan",
        "chat_author",
        "chat_capabilities",
        "reasoning_default",
        "embedding",
        "replace_existing_routes",
    }
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise QuickstartError(
            "quickstart 配置含未知字段（配置文件不得保存 API Key 或 secret）："
            + ", ".join(unknown)
        )
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != QUICKSTART_FILE_SCHEMA_VERSION:
        raise QuickstartError(
            f"quickstart schema_version 必须是 {QUICKSTART_FILE_SCHEMA_VERSION}"
        )

    def string_value(container: dict[str, Any], name: str, default: str = "") -> str:
        value = container.get(name, default)
        if not isinstance(value, str):
            raise QuickstartError(f"quickstart 字段 {name} 必须是字符串")
        return value

    raw_capabilities = raw.get("chat_capabilities", [])
    if not isinstance(raw_capabilities, list) or any(
        not isinstance(item, str) for item in raw_capabilities
    ):
        raise QuickstartError("quickstart 字段 chat_capabilities 必须是字符串数组")
    if len(set(raw_capabilities)) != len(raw_capabilities):
        raise QuickstartError("quickstart 字段 chat_capabilities 不得包含重复值")

    raw_embedding = raw.get("embedding")
    if raw_embedding is None:
        embedding: dict[str, Any] = {}
    elif isinstance(raw_embedding, dict):
        embedding = raw_embedding
    else:
        raise QuickstartError("quickstart 字段 embedding 必须是对象或 null")
    embedding_allowed = {"model", "dimensions", "space", "author"}
    embedding_unknown = sorted(str(key) for key in embedding if key not in embedding_allowed)
    if embedding_unknown:
        raise QuickstartError(
            "quickstart embedding 含未知字段：" + ", ".join(embedding_unknown)
        )
    if raw_embedding is not None:
        missing_embedding = sorted(
            name for name in ("model", "dimensions", "space") if name not in embedding
        )
        if missing_embedding:
            raise QuickstartError(
                "quickstart embedding 缺少字段：" + ", ".join(missing_embedding)
            )
    dimensions = embedding.get("dimensions")
    if dimensions is not None and (
        isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1
    ):
        raise QuickstartError("quickstart embedding.dimensions 必须是正整数")
    replace_existing_routes = raw.get("replace_existing_routes", False)
    if not isinstance(replace_existing_routes, bool):
        raise QuickstartError("quickstart 字段 replace_existing_routes 必须是布尔值")

    preset_id = string_value(raw, "preset")
    preset = get_channel_preset(preset_id) if preset_id else None
    spec = QuickstartSpec(
        channel_operator=string_value(
            raw,
            "channel",
            preset.channel_operator if preset else "",
        ),
        base_url=string_value(raw, "base_url", preset.base_url if preset else ""),
        chat_model=string_value(raw, "chat_model"),
        api_key=api_key,
        adapter=string_value(raw, "adapter", preset.adapter if preset else "generic"),
        plan=string_value(raw, "plan", "payg"),
        chat_author=string_value(raw, "chat_author"),
        chat_capabilities=tuple(raw_capabilities),
        reasoning_default=string_value(raw, "reasoning_default", "inherit"),
        embedding_model=string_value(embedding, "model"),
        embedding_dimensions=dimensions,
        embedding_space=string_value(embedding, "space"),
        embedding_author=string_value(embedding, "author"),
        connect_memory=connect_memory,
        replace_existing_routes=replace_existing_routes,
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
        if self.adapter not in _ADAPTERS:
            raise QuickstartError(f"未知 adapter：{self.adapter}")
        if self.plan not in _PLANS:
            raise QuickstartError(f"未知套餐类型：{self.plan}")
        if not self.channel_operator.strip():
            raise QuickstartError("渠道简称不能为空")
        if not self.base_url.strip():
            raise QuickstartError("base_url 不能为空")
        if not self.chat_model.strip():
            raise QuickstartError("聊天模型 ID 不能为空")
        if not self.api_key.strip():
            raise QuickstartError("API Key 不能为空")
        unknown = tuple(item for item in self.chat_capabilities if item not in _CHAT_CAPABILITIES)
        if unknown:
            raise QuickstartError("未知能力：" + ", ".join(unknown))
        if self.reasoning_default not in {"inherit", "enabled", "disabled"}:
            raise QuickstartError("reasoning_default 只能是 inherit/enabled/disabled")
        if self.embedding_model.strip():
            if not self.embedding_dimensions or self.embedding_dimensions < 1:
                raise QuickstartError("配置向量模型时必须给出正整数维度")
            if not self.embedding_space.strip():
                raise QuickstartError("配置向量模型时必须给出向量空间名称")


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
    connection_id = _unique_id(_slug(f"{operator}-account"), config.connections)
    connection_secret_ref = _default_secret_ref("CONNECTION", connection_id)
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
    chat_deployment_id = _unique_id(
        _slug(f"{connection_id}-{spec.chat_model}"),
        config.deployments,
    )
    chat_deployment = DeploymentConfig(
        connection=connection_id,
        upstream_model=spec.chat_model.strip(),
        model_author=(spec.chat_author.strip() or operator),
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

    payload = config.model_dump(mode="python")
    payload["connections"] = {
        **payload["connections"],
        connection_id: connection.model_dump(mode="python"),
    }
    deployments = dict(payload["deployments"])
    deployments[chat_deployment_id] = chat_deployment.model_dump(mode="python")
    routes = dict(payload["routes"])
    for route_id in CHAT_ROUTES:
        routes[route_id] = RouteConfig(
            kind="chat",
            targets=[chat_deployment_id],
            max_attempts=1,
        ).model_dump(mode="python")

    embedding_deployment_id = ""
    if spec.embedding_model.strip():
        embedding_deployment_id = _unique_id(
            _slug(f"{connection_id}-{spec.embedding_model}"),
            {**config.deployments, chat_deployment_id: chat_deployment},
        )
        embedding_deployment = DeploymentConfig(
            connection=connection_id,
            upstream_model=spec.embedding_model.strip(),
            model_author=(spec.embedding_author.strip() or operator),
            kind="embedding",
            capabilities=Capabilities(streaming=False),
            request_transform=RequestTransform(),
            dimensions=spec.embedding_dimensions,
            embedding_space=spec.embedding_space.strip(),
        )
        deployments[embedding_deployment_id] = embedding_deployment.model_dump(mode="python")
        routes[EMBEDDING_ROUTE] = RouteConfig(
            kind="embedding",
            targets=[embedding_deployment_id],
            max_attempts=1,
        ).model_dump(mode="python")

    payload["deployments"] = deployments
    payload["routes"] = routes

    # Ensure the memory-gateway backend client exists so a standalone quickstart
    # (no prior `stack install`) still yields a working connection. When it
    # already exists, reuse its secret_ref and existing key so we never diverge
    # from a key `stack install` already synced to memgw.
    clients = dict(payload["clients"])
    existing_client = config.clients.get("memory-gateway")
    created_memory_client = existing_client is None
    if existing_client is not None:
        client_secret_ref = existing_client.secret_ref
    else:
        client_secret_ref = _default_secret_ref("CLIENT", "memory-gateway")
    clients["memory-gateway"] = ClientConfig(
        kind="backend",
        secret_ref=client_secret_ref,
        allowed_routes=["memory.*", "knowledge.*"],
        allow_direct_deployments=False,
    ).model_dump(mode="python")
    payload["clients"] = clients

    memory_client_key = existing_secrets.get(client_secret_ref) or secrets.token_urlsafe(32)

    # Single validation of the whole relationship graph before the sole write.
    validated = GatewayConfig.model_validate(payload)
    write_config(paths.config, validated)
    set_secret(paths.secrets, connection_secret_ref, spec.api_key.strip())
    set_secret(paths.secrets, client_secret_ref, memory_client_key)

    return QuickstartResult(
        connection_id=connection_id,
        chat_deployment_id=chat_deployment_id,
        chat_routes=CHAT_ROUTES,
        memory_client_key=memory_client_key,
        embedding_deployment_id=embedding_deployment_id,
        embedding_space=spec.embedding_space.strip() if embedding_deployment_id else "",
        embedding_dimensions=spec.embedding_dimensions if embedding_deployment_id else None,
        created_memory_client=created_memory_client,
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._:-]+", "-", value.lower()).strip("-._:")
    if not normalized:
        normalized = "item"
    if len(normalized) > 120:
        digest = sha256(normalized.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized[:110].rstrip('-._:')}-{digest}"
    return normalized


def _unique_id(candidate: str, records: object) -> str:
    container = records if hasattr(records, "__contains__") else {}
    if candidate not in container:  # type: ignore[operator]
        return candidate
    index = 2
    while True:
        suffix = f"-{index}"
        alternate = candidate[: 120 - len(suffix)].rstrip("-._:") + suffix
        if alternate not in container:  # type: ignore[operator]
            return alternate
        index += 1


def _default_secret_ref(prefix: str, item_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", item_id).strip("_").upper()
    value = f"{prefix}_{slug}_API_KEY"
    if len(value) <= 120:
        return validate_id(value, "secret_ref")
    digest = sha256(item_id.encode("utf-8")).hexdigest()[:8].upper()
    return validate_id(f"{value[:111]}_{digest}", "secret_ref")
