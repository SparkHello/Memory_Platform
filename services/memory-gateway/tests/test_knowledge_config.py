import os

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_knowledge_search_agent
from app.config import Settings, get_settings


def test_knowledge_agent_is_local_only_by_default(monkeypatch) -> None:
    for key in (
        "KNOWLEDGE_AGENT_FLASH_MODEL",
        "KNOWLEDGE_AGENT_PRO_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KNOWLEDGE_AGENT_API_KEY", "")
    monkeypatch.setenv("KNOWLEDGE_AGENT_MIMO_API_KEY", "")
    monkeypatch.setenv("KNOWLEDGE_AGENT_KIMI_API_KEY", "")
    monkeypatch.setenv("LLM_DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("LLM_MIMO_API_KEY", "")
    monkeypatch.setenv("LLM_KIMI_API_KEY", "")
    monkeypatch.setenv("KNOWLEDGE_AGENT_EGRESS_POLICY", "none")
    monkeypatch.setenv("LLM_PROVIDER_PRIORITY", "")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.llm_deepseek_api_key == ""
    assert settings.knowledge_agent_egress_policy == "none"
    assert settings.llm_deepseek_flash_model == "deepseek-v4-flash"
    assert settings.llm_deepseek_pro_model == "deepseek-v4-pro"
    assert settings.llm_provider_priority == "D"
    get_settings.cache_clear()


def test_llm_provider_priority_is_normalized_and_validated() -> None:
    settings = Settings(
        _env_file=None,
        LLM_PROVIDER_PRIORITY=" m k d ",
    )
    assert settings.llm_provider_priority == "MKD"

    with pytest.raises(ValueError, match="同一模型代号"):
        Settings(_env_file=None, LLM_PROVIDER_PRIORITY="MMD")
    with pytest.raises(ValueError, match="M、K、D"):
        Settings(_env_file=None, LLM_PROVIDER_PRIORITY="MX")


def test_legacy_knowledge_agent_priority_alias_still_works() -> None:
    settings = Settings(
        _env_file=None,
        KNOWLEDGE_AGENT_PROVIDER_PRIORITY="KD",
    )

    assert settings.llm_provider_priority == "KD"


def test_knowledge_defaults_support_large_books_and_qwen_batch_limit() -> None:
    settings = Settings(_env_file=None)

    assert settings.knowledge_max_document_bytes == 50 * 1024 * 1024
    assert settings.knowledge_embedding_batch_size == 20
    assert settings.llm_kimi_base_url == "https://api.moonshot.cn/v1"


def test_shared_llm_provider_configuration_keeps_legacy_upstream_fallback(
) -> None:
    settings = Settings(
        _env_file=None,
        UPSTREAM_API_KEY="memory-key",
        UPSTREAM_MODEL="memory-model",
        KNOWLEDGE_AGENT_API_KEY="knowledge-key",
        KNOWLEDGE_AGENT_FLASH_MODEL="deepseek-v4-flash",
        KNOWLEDGE_AGENT_PRO_MODEL="deepseek-v4-pro",
        KNOWLEDGE_AGENT_EGRESS_POLICY="all",
    )

    assert settings.upstream_api_key == "memory-key"
    assert settings.llm_deepseek_api_key == "knowledge-key"
    assert settings.llm_deepseek_api_key != settings.upstream_api_key
    assert settings.knowledge_agent_egress_policy == "all"


def test_knowledge_agent_uses_only_central_gateway_configuration_when_enabled(
) -> None:
    settings = Settings(
        _env_file=None,
        MODEL_GATEWAY_BASE_URL="http://127.0.0.1:2030/v1",
        MODEL_GATEWAY_API_KEY="central-key",
        MODEL_GATEWAY_KNOWLEDGE_FAST_MODEL="route.knowledge.fast",
        MODEL_GATEWAY_KNOWLEDGE_PRO_MODEL="route.knowledge.pro",
        LLM_PROVIDER_PRIORITY="MKD",
        LLM_MIMO_API_KEY="must-not-be-used",
        LLM_KIMI_API_KEY="must-not-be-used",
        LLM_DEEPSEEK_API_KEY="must-not-be-used",
        KNOWLEDGE_AGENT_EGRESS_POLICY="normal",
        ALLOW_SENSITIVE_EGRESS=True,
    )

    agent = get_knowledge_search_agent(object(), settings)  # type: ignore[arg-type]

    assert agent.config.model_gateway_enabled is True
    assert agent.config.base_url == "http://127.0.0.1:2030/v1"
    assert agent.config.api_key == "central-key"
    assert agent.config.flash_model == "route.knowledge.fast"
    assert agent.config.pro_model == "route.knowledge.pro"
    assert agent.config.mimo_api_key == ""
    assert agent.config.kimi_api_key == ""
    assert agent.config.implicit_deepseek_fallback is False
    assert agent.config.egress_policy == "normal"
    assert agent.config.allow_sensitive_egress is True


def test_memory_and_knowledge_database_paths_must_be_distinct(monkeypatch, tmp_path) -> None:
    from app.main import create_app

    database = str(tmp_path / "same.db")
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", database)
    monkeypatch.setenv("KNOWLEDGE_DATABASE_PATH", database)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="不能与 DATABASE_PATH"):
        with TestClient(create_app()):
            pass
    get_settings.cache_clear()


def test_database_hardlinks_are_not_accepted_as_physical_isolation(tmp_path) -> None:
    from app.main import _validate_database_paths

    memory = tmp_path / "memory.db"
    knowledge = tmp_path / "knowledge.db"
    memory.touch()
    os.link(memory, knowledge)

    with pytest.raises(RuntimeError, match="不能与 DATABASE_PATH"):
        _validate_database_paths(str(memory), str(knowledge))


def test_knowledge_initialization_failure_does_not_take_memory_api_offline(
    monkeypatch,
    tmp_path,
) -> None:
    import app.main as main_module

    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("KNOWLEDGE_DATABASE_PATH", str(tmp_path / "knowledge.db"))
    get_settings.cache_clear()

    def fail_init(_self) -> None:
        raise RuntimeError("simulated knowledge init failure")

    monkeypatch.setattr(main_module.KnowledgeStore, "init_db", fail_init)
    with TestClient(main_module.create_app()) as client:
        headers = {"Authorization": "Bearer test-key"}
        assert client.get("/health").status_code == 200
        memories = client.get("/memories", headers=headers)
        assert memories.status_code == 200
        status = client.get("/knowledge/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["available"] is False
        assert "simulated knowledge init failure" in status.json()["error"]
    get_settings.cache_clear()
