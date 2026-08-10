from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping


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
    "memory.chat": "model_gateway_chat_model",
    "memory-extractor": "model_gateway_memory_extract_model",
    "memory-ingester": "model_gateway_memory_extract_model",
    "memory.extract": "model_gateway_memory_extract_model",
    "memory-context-compactor": "model_gateway_memory_compact_model",
    "memory.compact": "model_gateway_memory_compact_model",
    "core-memory-consolidator": "model_gateway_memory_core_model",
    "memory.core": "model_gateway_memory_core_model",
    "memory-review-editor": "model_gateway_memory_review_model",
    "memory.review": "model_gateway_memory_review_model",
    "knowledge.fast": "model_gateway_knowledge_fast_model",
    "knowledge.pro": "model_gateway_knowledge_pro_model",
    "embedding": "model_gateway_embedding_model",
    "memory.embedding": "model_gateway_embedding_model",
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

    dimensions = _embedding_dimensions(settings)
    routes = {
        field_name: str(getattr(settings, field_name, "") or "").strip()
        for field_name in set(_OPERATION_ROUTE_FIELDS.values())
    }
    missing_routes = sorted(name for name, value in routes.items() if not value)
    if missing_routes:
        raise ModelRuntimeConfigurationError(
            "中央模型网关缺少稳定 route 配置：" + ", ".join(missing_routes)
        )
    embedding_space = " ".join(
        str(
            getattr(settings, "model_gateway_embedding_space_id", "") or ""
        ).strip().split()
    )
    embedding = EmbeddingRuntime(
        enabled=bool(embedding_space),
        base_url=central_base_url,
        api_key=central_api_key,
        model=routes["model_gateway_embedding_model"],
        dimensions=dimensions,
        space_id=embedding_space,
        model_gateway_mode=True,
    )
    return ModelRuntime(
        mode="central",
        base_url=central_base_url,
        api_key=central_api_key,
        routes=MappingProxyType(routes),
        embedding=embedding,
    )


def _embedding_dimensions(settings: Any) -> int:
    raw_value = getattr(settings, "embedding_dimensions", 0)
    if isinstance(raw_value, bool):
        raise ModelRuntimeConfigurationError("EMBEDDING_DIMENSIONS 格式无效")
    try:
        dimensions = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ModelRuntimeConfigurationError("EMBEDDING_DIMENSIONS 格式无效") from exc
    if not 1 <= dimensions <= 65536:
        raise ModelRuntimeConfigurationError("EMBEDDING_DIMENSIONS 必须在 1 到 65536 之间")
    return dimensions
