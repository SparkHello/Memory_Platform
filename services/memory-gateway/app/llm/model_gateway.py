from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any

from model_gateway_contracts import (
    GatewayErrorCode,
    MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER,
    MODEL_GATEWAY_CONNECTION_HEADER,
    MODEL_GATEWAY_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_EMBEDDING_DIMENSIONS_HEADER,
    MODEL_GATEWAY_EMBEDDING_SPACE_HEADER,
    MODEL_GATEWAY_MODEL_AUTHOR_HEADER,
    MODEL_GATEWAY_PREFERRED_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_REASONING_ORIGIN_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_REQUIRE_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_ROUTE_HEADER,
    MODEL_GATEWAY_UPSTREAM_MODEL_HEADER,
    MODEL_GATEWAY_VENDOR_HEADER,
)

from app.llm.runtime import resolve_model_runtime


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
    expected_deployment: str = "",
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
    deployment = expected_deployment.strip()
    if deployment and metadata.deployment_id != deployment:
        raise ModelGatewayProtocolError(
            "中央模型网关响应 deployment 与严格亲和要求不一致"
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


def model_gateway_model_for_operation(settings: Any, operation: str) -> str:
    return resolve_model_runtime(settings).route_for(operation)


def is_model_gateway_affinity_unavailable(
    status_code: int,
    content: bytes,
) -> bool:
    """Match only the central gateway's explicit strict-affinity error."""
    if status_code != 409:
        return False
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return (
        error.get("code") == GatewayErrorCode.AFFINITY_UNAVAILABLE.value
        or error.get("type") == GatewayErrorCode.AFFINITY_UNAVAILABLE.value
    )


def _safe_header_value(value: object, *, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        return ""
    return normalized
