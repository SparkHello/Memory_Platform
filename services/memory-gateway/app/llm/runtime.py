from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

from model_gateway_contracts import (
    KNOWLEDGE_FAST_ROUTE,
    KNOWLEDGE_PRO_ROUTE,
    MEMORY_CHAT_ROUTE,
    MEMORY_COMPACT_ROUTE,
    MEMORY_CORE_ROUTE,
    MEMORY_EMBEDDING_ROUTE,
    MEMORY_EXTRACT_ROUTE,
    MEMORY_REVIEW_ROUTE,
)

from app.llm.embedding_contract import (
    EmbeddingMode,
    EmbeddingState,
    get_embedding_contract_snapshot,
)


class ModelRuntimeConfigurationError(ValueError):
    """Model routing configuration is incomplete or internally inconsistent."""


MODEL_GATEWAY_REQUIRED_MESSAGE = (
    "Memory Gateway 仅支持通过 Model Gateway 调用模型。"
    "请配置 MODEL_GATEWAY_BASE_URL 与 MODEL_GATEWAY_API_KEY，"
    "并完成 modelgw / scripts/setup.sh 的渠道路由配置。"
    "旧的 UPSTREAM_* / LLM_* direct-provider 路径已移除。"
)

_OPERATION_ROUTE_FIELDS = {
    "chat": "model_gateway_chat_model",
    MEMORY_CHAT_ROUTE: "model_gateway_chat_model",
    "memory-extractor": "model_gateway_memory_extract_model",
    "memory-ingester": "model_gateway_memory_extract_model",
    MEMORY_EXTRACT_ROUTE: "model_gateway_memory_extract_model",
    "memory-context-compactor": "model_gateway_memory_compact_model",
    MEMORY_COMPACT_ROUTE: "model_gateway_memory_compact_model",
    "core-memory-consolidator": "model_gateway_memory_core_model",
    MEMORY_CORE_ROUTE: "model_gateway_memory_core_model",
    "memory-review-editor": "model_gateway_memory_review_model",
    MEMORY_REVIEW_ROUTE: "model_gateway_memory_review_model",
    KNOWLEDGE_FAST_ROUTE: "model_gateway_knowledge_fast_model",
    KNOWLEDGE_PRO_ROUTE: "model_gateway_knowledge_pro_model",
    "embedding": "model_gateway_embedding_model",
    MEMORY_EMBEDDING_ROUTE: "model_gateway_embedding_model",
}


@dataclass(frozen=True, slots=True)
class EmbeddingRuntime:
    enabled: bool
    base_url: str
    api_key: str = field(repr=False)
    model: str
    dimensions: int
    space_id: str
    model_gateway_mode: bool = True
    mode: EmbeddingMode = "auto"
    state: EmbeddingState = "unavailable"
    code: str = "model_gateway_control_unavailable"
    status_model: str = ""


@dataclass(frozen=True, slots=True)
class ModelRuntime:
    """Resolved Model Gateway credentials and stable route aliases.

    Direct-provider fallbacks are intentionally unsupported. Incomplete central
    credentials fail closed with a migration-oriented error.
    """

    mode: Literal["central"]
    base_url: str
    api_key: str = field(repr=False)
    routes: Mapping[str, str]
    embedding: EmbeddingRuntime

    @property
    def is_central(self) -> bool:
        return True

    def route_for(self, operation: str) -> str:
        normalized = operation.strip().lower()
        field_name = _OPERATION_ROUTE_FIELDS.get(normalized)
        if field_name is None:
            raise ModelRuntimeConfigurationError(
                f"中央模型网关不支持 operation：{operation}"
            )
        route = self.routes.get(field_name, "")
        if not route:
            raise ModelRuntimeConfigurationError(
                f"中央模型网关 operation {operation} 没有配置模型别名"
            )
        return route


def resolve_model_runtime(settings: Any) -> ModelRuntime:
    """Resolve the Model Gateway runtime; never fall back to direct providers."""

    central_base_url = str(
        getattr(settings, "model_gateway_base_url", "") or ""
    ).strip().rstrip("/")
    central_api_key = str(
        getattr(settings, "model_gateway_api_key", "") or ""
    ).strip()
    if bool(central_base_url) != bool(central_api_key):
        raise ModelRuntimeConfigurationError(
            "MODEL_GATEWAY_BASE_URL 和 MODEL_GATEWAY_API_KEY 必须同时配置"
        )
    if not central_base_url:
        raise ModelRuntimeConfigurationError(MODEL_GATEWAY_REQUIRED_MESSAGE)

    routes = {
        field_name: str(getattr(settings, field_name, "") or "").strip()
        for field_name in set(_OPERATION_ROUTE_FIELDS.values())
    }
    missing_routes = sorted(name for name, value in routes.items() if not value)
    if missing_routes:
        raise ModelRuntimeConfigurationError(
            "中央模型网关缺少稳定 route 配置：" + ", ".join(missing_routes)
        )
    embedding_contract = get_embedding_contract_snapshot(settings)
    embedding = EmbeddingRuntime(
        enabled=embedding_contract.configured,
        base_url=central_base_url,
        api_key=central_api_key,
        model=routes["model_gateway_embedding_model"],
        dimensions=embedding_contract.dimensions,
        space_id=embedding_contract.space_id,
        model_gateway_mode=True,
        mode=embedding_contract.mode,
        state=embedding_contract.state,
        code=embedding_contract.code,
        status_model=embedding_contract.upstream_model,
    )
    return ModelRuntime(
        mode="central",
        base_url=central_base_url,
        api_key=central_api_key,
        routes=MappingProxyType(routes),
        embedding=embedding,
    )
