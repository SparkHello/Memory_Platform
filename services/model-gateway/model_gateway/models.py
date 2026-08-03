from __future__ import annotations

from fnmatch import fnmatchcase
from decimal import Decimal
import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
RESTRICTED_PLAN_TYPES = {"token_plan", "coding_plan", "direct_tool_only"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=2030, ge=1, le=65535)
    body_limit_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)

    @field_validator("host")
    @classmethod
    def local_host_only(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("默认安全模式只允许绑定本机回环地址")
        return value.strip()


class ClientConfig(StrictModel):
    kind: Literal["backend", "interactive", "admin"] = "backend"
    secret_ref: str
    allowed_routes: list[str] = Field(default_factory=lambda: ["*"])
    allow_direct_deployments: bool = False
    enabled: bool = True

    @field_validator("secret_ref")
    @classmethod
    def valid_secret_ref(cls, value: str) -> str:
        return validate_id(value, "secret_ref")

    def allows_route(self, route_id: str) -> bool:
        return any(fnmatchcase(route_id, pattern) for pattern in self.allowed_routes)


class AuthConfig(StrictModel):
    type: Literal["bearer", "x-api-key"] = "bearer"
    secret_ref: str

    @field_validator("secret_ref")
    @classmethod
    def valid_secret_ref(cls, value: str) -> str:
        return validate_id(value, "secret_ref")


class BillingPlan(StrictModel):
    type: Literal[
        "payg",
        "subscription",
        "free_tier",
        "token_plan",
        "coding_plan",
        "direct_tool_only",
        "custom",
    ] = "payg"
    name: str = "default"


class PricingTier(StrictModel):
    """Token prices for one input-size tier, expressed per ``unit_tokens``."""

    max_input_tokens: int | None = Field(default=None, ge=1)
    input: Decimal | None = Field(default=None, ge=0)
    cached_input: Decimal | None = Field(default=None, ge=0)
    output: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def has_a_rate(self) -> "PricingTier":
        if self.input is None and self.cached_input is None and self.output is None:
            raise ValueError("pricing tier 至少需要一个 Token 单价")
        return self


class PricingConfig(StrictModel):
    """Auditable pricing record; prices are never inferred from a similar model."""

    mode: Literal["per_token", "subscription", "free_tier", "custom", "unknown"] = (
        "unknown"
    )
    currency: str = "USD"
    unit_tokens: int = Field(default=1_000_000, ge=1)
    tiers: list[PricingTier] = Field(default_factory=list)
    source_url: str = ""
    effective_from: str = ""
    checked_at: str = ""
    notes: str = ""

    @field_validator("currency")
    @classmethod
    def normalized_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("currency 必须是三位 ISO 货币代码")
        return normalized

    @field_validator("source_url")
    @classmethod
    def official_source_url(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and urlparse(normalized).scheme not in {"http", "https"}:
            raise ValueError("pricing source_url 必须是 HTTP(S) URL")
        return normalized

    @model_validator(mode="after")
    def validate_pricing(self) -> "PricingConfig":
        if self.mode == "per_token":
            if not self.tiers:
                raise ValueError("per_token pricing 必须声明至少一个 tier")
            if not self.source_url:
                raise ValueError("per_token pricing 必须记录官方 source_url")
        if self.tiers:
            finite = [tier.max_input_tokens for tier in self.tiers if tier.max_input_tokens]
            if finite != sorted(finite) or len(finite) != len(set(finite)):
                raise ValueError("pricing tiers 的 max_input_tokens 必须严格递增")
            open_ended = [tier for tier in self.tiers if tier.max_input_tokens is None]
            if len(open_ended) > 1 or (open_ended and self.tiers[-1] is not open_ended[0]):
                raise ValueError("pricing 只能有一个无上限 tier，且必须位于最后")
        return self


class ConnectionConfig(StrictModel):
    channel_operator: str
    protocol: Literal["openai_compatible"] = "openai_compatible"
    adapter: Literal["generic", "kimi", "deepseek", "mimo"] = "generic"
    base_url: str
    auth: AuthConfig
    billing_plan: BillingPlan = Field(default_factory=BillingPlan)
    usage_scope: Literal["backend_allowed", "interactive_only", "disabled"] = (
        "backend_allowed"
    )
    models_endpoint: str | None = "/models"
    chat_endpoint: str = "/chat/completions"
    embeddings_endpoint: str = "/embeddings"
    forward_headers: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=300.0, ge=1.0, le=3600.0)
    rate_limit_cooldown_seconds: float = Field(default=300.0, ge=0.0, le=86400.0)
    enabled: bool = True

    @field_validator("channel_operator")
    @classmethod
    def normalized_operator(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("channel_operator 不能为空")
        return validate_id(normalized, "channel_operator")

    @field_validator("base_url")
    @classmethod
    def safe_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if not parsed.hostname:
            raise ValueError("base_url 必须是完整 URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url 不能内嵌账号或密钥")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url 不能包含 query 或 fragment")
        local = parsed.hostname.lower() in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            raise ValueError("远程 connection 必须使用 HTTPS；HTTP 仅允许本机回环地址")
        return normalized

    @field_validator("forward_headers")
    @classmethod
    def safe_forward_headers(cls, values: list[str]) -> list[str]:
        blocked = {
            "authorization",
            "cookie",
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
        }
        normalized: list[str] = []
        for value in values:
            name = value.strip().lower()
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name):
                raise ValueError(f"无效 forward header：{value}")
            if name in blocked or name.startswith("x-model-gateway-"):
                raise ValueError(f"禁止转发本地或敏感 header：{value}")
            if name not in normalized:
                normalized.append(name)
        return normalized

    @field_validator("models_endpoint", "chat_endpoint", "embeddings_endpoint")
    @classmethod
    def relative_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized.startswith("/") or "://" in normalized:
            raise ValueError("endpoint 必须是以 / 开头的相对路径")
        return normalized

    @model_validator(mode="after")
    def restricted_plan_scope(self) -> "ConnectionConfig":
        if (
            self.billing_plan.type in RESTRICTED_PLAN_TYPES
            and self.usage_scope == "backend_allowed"
        ):
            raise ValueError("受限套餐不能配置为 backend_allowed")
        hostname = (urlparse(self.base_url).hostname or "").lower()
        if (
            "token-plan" in hostname or "coding.dashscope" in hostname
        ) and self.usage_scope == "backend_allowed":
            raise ValueError("检测到套餐专属端点，禁止后台使用")
        return self


class Capabilities(StrictModel):
    streaming: bool = True
    tools: bool = False
    parallel_tools: bool = False
    reasoning: bool = False
    multimodal_input: bool = False
    json_object: bool = False
    json_schema: bool = False


class RequestTransform(StrictModel):
    remove: list[str] = Field(default_factory=list)
    set_if_missing: dict[str, Any] = Field(default_factory=dict)
    force: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def protect_semantic_fields(self) -> "RequestTransform":
        protected = {"model", "messages", "input", "stream"}
        touched = set(self.remove) | set(self.set_if_missing) | set(self.force)
        invalid = sorted(touched & protected)
        if invalid:
            raise ValueError("request_transform 不能修改核心字段：" + ", ".join(invalid))
        return self


class DeploymentConfig(StrictModel):
    connection: str
    upstream_model: str
    model_author: str
    model_family: str = ""
    kind: Literal["chat", "embedding"] = "chat"
    reasoning_default: Literal["inherit", "enabled", "disabled"] = "inherit"
    capabilities: Capabilities = Field(default_factory=Capabilities)
    request_transform: RequestTransform = Field(default_factory=RequestTransform)
    dimensions: int | None = Field(default=None, ge=1, le=65536)
    embedding_space: str = ""
    pricing: str | None = None
    enabled: bool = True

    @field_validator("connection")
    @classmethod
    def valid_connection(cls, value: str) -> str:
        return validate_id(value, "connection")

    @field_validator("pricing")
    @classmethod
    def valid_pricing(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_id(value, "pricing")

    @field_validator("upstream_model", "model_author")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        if len(normalized) > 300 or any(
            not 33 <= ord(character) <= 126 for character in normalized
        ):
            raise ValueError("模型标识必须是无空白的可打印 ASCII，且最长 300 字符")
        return normalized

    @field_validator("embedding_space")
    @classmethod
    def safe_embedding_space(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and (
            len(normalized) > 300
            or any(not 33 <= ord(character) <= 126 for character in normalized)
        ):
            raise ValueError("embedding_space 必须是无空白的可打印 ASCII，且最长 300 字符")
        return normalized

    @model_validator(mode="after")
    def embedding_identity(self) -> "DeploymentConfig":
        if self.kind == "embedding" and (
            self.dimensions is None or not self.embedding_space.strip()
        ):
            raise ValueError("embedding deployment 必须声明 dimensions 和 embedding_space")
        if self.kind == "chat" and (
            self.dimensions is not None or self.embedding_space.strip()
        ):
            raise ValueError("chat deployment 不能声明 embedding 向量空间")
        if self.kind == "embedding":
            for group_name, values in (
                ("set_if_missing", self.request_transform.set_if_missing),
                ("force", self.request_transform.force),
            ):
                if "dimensions" not in values:
                    continue
                configured = values["dimensions"]
                if (
                    isinstance(configured, bool)
                    or not isinstance(configured, int)
                    or configured != self.dimensions
                ):
                    raise ValueError(
                        f"embedding request_transform.{group_name}.dimensions "
                        "必须等于 deployment 声明维度"
                    )
        return self


class RouteConfig(StrictModel):
    kind: Literal["chat", "embedding"] = "chat"
    targets: list[str] = Field(min_length=1)
    required_capabilities: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=3, ge=1, le=20)
    enabled: bool = True

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, values: list[str]) -> list[str]:
        normalized = [validate_id(value, "deployment") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("route targets 不能重复")
        return normalized


class GatewayConfig(StrictModel):
    schema_version: Literal[1] = 1
    server: ServerConfig = Field(default_factory=ServerConfig)
    clients: dict[str, ClientConfig] = Field(default_factory=dict)
    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)
    deployments: dict[str, DeploymentConfig] = Field(default_factory=dict)
    routes: dict[str, RouteConfig] = Field(default_factory=dict)
    pricing: dict[str, PricingConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> "GatewayConfig":
        for group_name, values in (
            ("client", self.clients),
            ("connection", self.connections),
            ("deployment", self.deployments),
            ("route", self.routes),
            ("pricing", self.pricing),
        ):
            for item_id in values:
                validate_id(item_id, group_name)

        client_secret_refs = [client.secret_ref for client in self.clients.values()]
        if len(client_secret_refs) != len(set(client_secret_refs)):
            raise ValueError("每个 client 必须使用独立的 secret_ref，避免身份权限混淆")

        for deployment_id, deployment in self.deployments.items():
            if deployment.connection not in self.connections:
                raise ValueError(
                    f"deployment {deployment_id} 引用了不存在的 connection："
                    f"{deployment.connection}"
                )
            if deployment.pricing is not None and deployment.pricing not in self.pricing:
                raise ValueError(
                    f"deployment {deployment_id} 引用了不存在的 pricing："
                    f"{deployment.pricing}"
                )

        for route_id, route in self.routes.items():
            deployments: list[DeploymentConfig] = []
            for target in route.targets:
                deployment = self.deployments.get(target)
                if deployment is None:
                    raise ValueError(f"route {route_id} 引用了不存在的 deployment：{target}")
                if deployment.kind != route.kind:
                    raise ValueError(f"route {route_id} 与 deployment {target} 的 kind 不一致")
                for capability in route.required_capabilities:
                    if not hasattr(deployment.capabilities, capability):
                        raise ValueError(f"未知 capability：{capability}")
                    if not getattr(deployment.capabilities, capability):
                        raise ValueError(
                            f"deployment {target} 不满足 route {route_id} 的 {capability}"
                        )
                deployments.append(deployment)
            if route.kind == "embedding":
                spaces = {
                    (deployment.embedding_space, deployment.dimensions)
                    for deployment in deployments
                }
                if len(spaces) != 1:
                    raise ValueError(
                        f"embedding route {route_id} 不能混用不同向量空间或维度"
                    )
        return self


def validate_id(value: str, label: str) -> str:
    normalized = value.strip()
    if not ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} ID 格式无效：{value}")
    return normalized
