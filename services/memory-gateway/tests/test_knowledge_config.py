"""Knowledge-related settings that remain after Model Gateway convergence."""

from app.config import Settings


def test_knowledge_agent_defaults_are_conservative() -> None:
    settings = Settings(_env_file=None)

    assert settings.knowledge_agent_egress_policy == "none"
    assert settings.knowledge_agent_timeout_seconds == 25.0
    assert settings.knowledge_max_document_bytes == 50 * 1024 * 1024
    assert settings.embedding_dimensions == 1024


def test_model_gateway_route_aliases_default_for_knowledge() -> None:
    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-key",
    )
    assert settings.model_gateway_knowledge_fast_model == "knowledge.fast"
    assert settings.model_gateway_knowledge_pro_model == "knowledge.pro"
