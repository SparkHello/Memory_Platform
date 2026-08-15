"""Stable machine-readable error codes returned by Model Gateway."""

from enum import StrEnum


class GatewayErrorCode(StrEnum):
    ERROR = "model_gateway_error"
    CONFIG_STALE = "model_gateway_config_stale"
    CONFIG_INVALID = "model_gateway_config_invalid"
    CONFIGURATION_INVALID = "model_gateway_configuration_invalid"
    CANDIDATE_KEY_REJECTED = "model_gateway_candidate_key_rejected"
    OBJECT_REFERENCED = "model_gateway_object_referenced"
    SECRET_INVALID = "model_gateway_secret_invalid"
    SECRET_DOMAIN_CONFLICT = "model_gateway_secret_domain_conflict"
    ADMIN_REQUIRED = "model_gateway_admin_required"
    USAGE_QUERY_INVALID = "model_gateway_usage_query_invalid"
    USAGE_QUERY_FORBIDDEN = "model_gateway_usage_query_forbidden"
    USAGE_METADATA_INVALID = "model_gateway_usage_metadata_invalid"
    USAGE_METADATA_FORBIDDEN = "model_gateway_usage_metadata_forbidden"
    INSUFFICIENT_STORAGE = "model_gateway_insufficient_storage"
    EMBEDDING_DIMENSIONS_MISMATCH = "model_gateway_embedding_dimensions_mismatch"
    INVALID_EMBEDDING_RESPONSE = "model_gateway_invalid_embedding_response"
    CAPABILITY_UNAVAILABLE = "model_gateway_capability_unavailable"
    AFFINITY_UNAVAILABLE = "model_gateway_affinity_unavailable"
    AMBIGUOUS_UPSTREAM_ERROR = "model_gateway_ambiguous_upstream_error"


__all__ = ["GatewayErrorCode"]
