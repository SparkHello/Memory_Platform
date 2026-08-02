import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.model_gateway import (
    MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER,
    MODEL_GATEWAY_CONNECTION_HEADER,
    MODEL_GATEWAY_DEPLOYMENT_HEADER,
    MODEL_GATEWAY_EMBEDDING_DIMENSIONS_HEADER,
    MODEL_GATEWAY_EMBEDDING_SPACE_HEADER,
    MODEL_GATEWAY_ROUTE_HEADER,
    MODEL_GATEWAY_MODEL_AUTHOR_HEADER,
    MODEL_GATEWAY_UPSTREAM_MODEL_HEADER,
    MODEL_GATEWAY_VENDOR_HEADER,
    ModelGatewayProtocolError,
    model_gateway_model_for_operation,
    parse_model_gateway_metadata,
    validate_model_gateway_metadata,
)


def test_model_gateway_settings_require_base_url_and_key_together() -> None:
    with pytest.raises(ValidationError, match="必须同时配置"):
        Settings(
            _env_file=None,
            MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        )

    with pytest.raises(ValidationError, match="必须同时配置"):
        Settings(
            _env_file=None,
            MODEL_GATEWAY_API_KEY="local-model-key",
        )

    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="local-model-key",
    )
    assert settings.model_gateway_enabled is True
    assert settings.model_gateway_chat_model == "memory.chat"
    assert settings.model_gateway_memory_extract_model == "memory.extract"
    assert settings.model_gateway_memory_compact_model == "memory.compact"
    assert settings.model_gateway_memory_core_model == "memory.core"
    assert settings.model_gateway_memory_review_model == "memory.review"
    assert settings.model_gateway_knowledge_fast_model == "knowledge.fast"
    assert settings.model_gateway_knowledge_pro_model == "knowledge.pro"
    assert settings.model_gateway_embedding_model == "memory.embedding"
    assert settings.model_gateway_embedding_space_id == ""


def test_model_gateway_settings_reject_credential_leaks_and_unsafe_routes() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            _env_file=None,
            MODEL_GATEWAY_BASE_URL="http://192.168.1.8:2030/v1",
            MODEL_GATEWAY_API_KEY="local-model-key",
        )
    with pytest.raises(ValidationError, match="安全的服务 URL"):
        Settings(
            _env_file=None,
            MODEL_GATEWAY_BASE_URL="https://user:password@example.invalid/v1",
            MODEL_GATEWAY_API_KEY="local-model-key",
        )
    with pytest.raises(ValidationError, match="稳定 ID"):
        Settings(
            _env_file=None,
            MODEL_GATEWAY_BASE_URL="http://localhost:2030/v1",
            MODEL_GATEWAY_API_KEY="local-model-key",
            MODEL_GATEWAY_CHAT_MODEL="memory/chat",
        )


def test_model_gateway_metadata_parses_the_reserved_response_headers() -> None:
    metadata = parse_model_gateway_metadata(
        {
            MODEL_GATEWAY_ROUTE_HEADER.lower(): " memory.review ",
            MODEL_GATEWAY_DEPLOYMENT_HEADER: "deploy-kimi-review",
            MODEL_GATEWAY_CONNECTION_HEADER: "connection-moonshot-cn",
            MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER: "moonshot",
            MODEL_GATEWAY_MODEL_AUTHOR_HEADER: "moonshot",
            MODEL_GATEWAY_VENDOR_HEADER: "kimi",
            MODEL_GATEWAY_UPSTREAM_MODEL_HEADER: "kimi-k2.7-code",
            MODEL_GATEWAY_EMBEDDING_SPACE_HEADER: "qwen3.7/1024/v1",
            MODEL_GATEWAY_EMBEDDING_DIMENSIONS_HEADER: "1024",
        }
    )

    assert metadata.route == "memory.review"
    assert metadata.deployment_id == "deploy-kimi-review"
    assert metadata.connection_id == "connection-moonshot-cn"
    assert metadata.channel_operator == "moonshot"
    assert metadata.model_author == "moonshot"
    assert metadata.vendor == "moonshot"
    assert metadata.upstream_model == "kimi-k2.7-code"
    assert metadata.embedding_space_id == "qwen3.7/1024/v1"
    assert metadata.embedding_dimensions == 1024


def test_model_gateway_metadata_ignores_malformed_values() -> None:
    metadata = parse_model_gateway_metadata(
        {
            MODEL_GATEWAY_ROUTE_HEADER: "memory.extract\x00spoofed",
            MODEL_GATEWAY_DEPLOYMENT_HEADER: "d" * 201,
            MODEL_GATEWAY_VENDOR_HEADER: "deepseek",
        }
    )

    assert metadata.route == ""
    assert metadata.deployment_id == ""
    assert metadata.vendor == "deepseek"


def test_model_gateway_metadata_validation_requires_attribution_and_route() -> None:
    valid = parse_model_gateway_metadata(
        {
            MODEL_GATEWAY_ROUTE_HEADER: "memory.embedding",
            MODEL_GATEWAY_DEPLOYMENT_HEADER: "embedding-primary",
            MODEL_GATEWAY_CONNECTION_HEADER: "dashscope-official",
            MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER: "alibaba",
            MODEL_GATEWAY_MODEL_AUTHOR_HEADER: "alibaba",
            MODEL_GATEWAY_VENDOR_HEADER: "alibaba",
            MODEL_GATEWAY_UPSTREAM_MODEL_HEADER: "qwen3.7-text-embedding",
            MODEL_GATEWAY_EMBEDDING_SPACE_HEADER: "qwen3.7/1024/v1",
            MODEL_GATEWAY_EMBEDDING_DIMENSIONS_HEADER: "1024",
        }
    )

    validate_model_gateway_metadata(
        valid,
        expected_route="memory.embedding",
        expected_embedding_space="qwen3.7/1024/v1",
        expected_embedding_dimensions=1024,
    )
    with pytest.raises(ModelGatewayProtocolError, match="route"):
        validate_model_gateway_metadata(valid, expected_route="memory.chat")
    with pytest.raises(ModelGatewayProtocolError, match="embedding space"):
        validate_model_gateway_metadata(
            valid,
            expected_route="memory.embedding",
            expected_embedding_space="other-space",
        )
    with pytest.raises(ModelGatewayProtocolError, match="embedding dimensions"):
        validate_model_gateway_metadata(
            valid,
            expected_route="memory.embedding",
            expected_embedding_space="qwen3.7/1024/v1",
            expected_embedding_dimensions=1536,
        )
    with pytest.raises(ModelGatewayProtocolError, match="connection"):
        validate_model_gateway_metadata(
            parse_model_gateway_metadata(
                {
                    MODEL_GATEWAY_ROUTE_HEADER: "memory.chat",
                    MODEL_GATEWAY_DEPLOYMENT_HEADER: "chat-primary",
                    MODEL_GATEWAY_CHANNEL_OPERATOR_HEADER: "moonshot",
                    MODEL_GATEWAY_MODEL_AUTHOR_HEADER: "moonshot",
                    MODEL_GATEWAY_VENDOR_HEADER: "kimi",
                    MODEL_GATEWAY_UPSTREAM_MODEL_HEADER: "kimi-k2.7-code",
                }
            ),
            expected_route="memory.chat",
        )


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("memory-extractor", "custom.extract"),
        ("memory-ingester", "custom.extract"),
        ("memory-context-compactor", "custom.compact"),
        ("core-memory-consolidator", "custom.core"),
        ("memory-review-editor", "custom.review"),
        ("knowledge.fast", "custom.knowledge.fast"),
        ("knowledge.pro", "custom.knowledge.pro"),
        ("chat", "custom.chat"),
        ("embedding", "custom.embedding"),
    ],
)
def test_model_gateway_operation_uses_configured_alias(
    operation: str,
    expected: str,
) -> None:
    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="local-model-key",
        MODEL_GATEWAY_CHAT_MODEL="custom.chat",
        MODEL_GATEWAY_MEMORY_EXTRACT_MODEL="custom.extract",
        MODEL_GATEWAY_MEMORY_COMPACT_MODEL="custom.compact",
        MODEL_GATEWAY_MEMORY_CORE_MODEL="custom.core",
        MODEL_GATEWAY_MEMORY_REVIEW_MODEL="custom.review",
        MODEL_GATEWAY_KNOWLEDGE_FAST_MODEL="custom.knowledge.fast",
        MODEL_GATEWAY_KNOWLEDGE_PRO_MODEL="custom.knowledge.pro",
        MODEL_GATEWAY_EMBEDDING_MODEL="custom.embedding",
    )

    assert model_gateway_model_for_operation(settings, operation) == expected


def test_model_gateway_operation_rejects_unknown_internal_task() -> None:
    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="local-model-key",
    )

    with pytest.raises(ValueError, match="不支持 operation"):
        model_gateway_model_for_operation(settings, "unknown-internal-task")
