from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


MODEL_GATEWAY_ROUTE_HEADER = "X-Model-Gateway-Route"
MODEL_GATEWAY_DEPLOYMENT_HEADER = "X-Model-Gateway-Deployment"
MODEL_GATEWAY_CONNECTION_HEADER = "X-Model-Gateway-Connection"
MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER = "X-Model-Gateway-Channel-Operator"
MODEL_GATEWAY_MODEL_AUTHOR_HEADER = "X-Model-Gateway-Model-Author"
MODEL_GATEWAY_VENDOR_HEADER = "X-Model-Gateway-Vendor"
MODEL_GATEWAY_UPSTREAM_MODEL_HEADER = "X-Model-Gateway-Upstream-Model"
MODEL_GATEWAY_EMBEDDING_SPACE_HEADER = "X-Model-Gateway-Embedding-Space"
MODEL_GATEWAY_EMBEDDING_DIMENSIONS_HEADER = "X-Model-Gateway-Embedding-Dimensions"


@dataclass(frozen=True, slots=True)
class ModelGatewayMetadata:
    route: str = ""
    deployment_id: str = ""
    connection_id: str = ""
    channel_operator: str = ""
    model_author: str = ""
    vendor: str = ""
    upstream_model: str = ""
    embedding_space_id: str = ""
    embedding_dimensions: int = 0


class ModelGatewayProtocolError(ValueError):
    """The central gateway returned incomplete or contradictory attribution."""


_HEADER_FIELDS = {
    MODEL_GATEWAY_ROUTE_HEADER.lower(): ("route", 120),
    MODEL_GATEWAY_DEPLOYMENT_HEADER.lower(): ("deployment_id", 200),
    MODEL_GATEWAY_CONNECTION_HEADER.lower(): ("connection_id", 200),
    MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER.lower(): ("channel_operator", 120),
    MODEL_GATEWAY_MODEL_AUTHOR_HEADER.lower(): ("model_author", 300),
    MODEL_GATEWAY_VENDOR_HEADER.lower(): ("vendor", 80),
    MODEL_GATEWAY_UPSTREAM_MODEL_HEADER.lower(): ("upstream_model", 300),
    MODEL_GATEWAY_EMBEDDING_SPACE_HEADER.lower(): ("embedding_space_id", 300),
}


def parse_model_gateway_metadata(
    headers: Mapping[str, str],
) -> ModelGatewayMetadata:
    """Parse trusted routing metadata without letting malformed headers escape."""

    normalized = {str(name).lower(): value for name, value in headers.items()}
    values: dict[str, Any] = {}
    for header_name, (field_name, max_length) in _HEADER_FIELDS.items():
        values[field_name] = _safe_header_value(
            normalized.get(header_name),
            max_length=max_length,
        )
    channel_operator = values.get("channel_operator") or values.get("vendor", "")
    values["channel_operator"] = channel_operator
    # Keep the first protocol revision's `vendor` attribute as a compatibility
    # alias, but its meaning is explicitly the billing/channel operator.
    values["vendor"] = channel_operator
    dimension_text = _safe_header_value(
        normalized.get(MODEL_GATEWAY_EMBEDDING_DIMENSIONS_HEADER.lower()),
        max_length=5,
    )
    values["embedding_dimensions"] = (
        int(dimension_text)
        if dimension_text.isdigit() and 1 <= int(dimension_text) <= 65536
        else 0
    )
    return ModelGatewayMetadata(**values)


def validate_model_gateway_metadata(
    metadata: ModelGatewayMetadata,
    *,
    expected_route: str,
    expected_embedding_space: str = "",
    expected_embedding_dimensions: int = 0,
) -> None:
    required_fields = {
        "route": metadata.route,
        "deployment": metadata.deployment_id,
        "connection": metadata.connection_id,
        "channel_operator": metadata.channel_operator,
        "model_author": metadata.model_author,
        "upstream_model": metadata.upstream_model,
    }
    if expected_embedding_space:
        required_fields["embedding_space"] = metadata.embedding_space_id
    if expected_embedding_dimensions:
        required_fields["embedding_dimensions"] = metadata.embedding_dimensions
    missing = [name for name, value in required_fields.items() if not value]
    if missing:
        raise ModelGatewayProtocolError(
            f"中央模型网关响应缺少归因字段：{', '.join(missing)}"
        )
    route = expected_route.strip()
    if route and metadata.route != route:
        raise ModelGatewayProtocolError(
            "中央模型网关响应 route 与请求模型别名不一致"
        )
    space = " ".join(expected_embedding_space.strip().split())
    if space and metadata.embedding_space_id != space:
        raise ModelGatewayProtocolError(
            "中央模型网关响应 embedding space 与配置不一致"
        )
    if (
        expected_embedding_dimensions
        and metadata.embedding_dimensions != expected_embedding_dimensions
    ):
        raise ModelGatewayProtocolError(
            "中央模型网关响应 embedding dimensions 与配置不一致"
        )


_OPERATION_MODEL_FIELDS = {
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


def model_gateway_model_for_operation(settings: Any, operation: str) -> str:
    normalized = operation.strip().lower()
    field_name = _OPERATION_MODEL_FIELDS.get(normalized)
    if field_name is None:
        raise ValueError(f"中央模型网关不支持 operation：{operation}")
    model = str(getattr(settings, field_name, "") or "").strip()
    if not model:
        raise ValueError(f"中央模型网关 operation {operation} 没有配置模型别名")
    return model


def _safe_header_value(value: object, *, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        return ""
    return normalized
