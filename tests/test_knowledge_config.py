import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


def test_knowledge_agent_is_local_only_by_default(monkeypatch) -> None:
    for key in (
        "KNOWLEDGE_AGENT_API_KEY",
        "KNOWLEDGE_AGENT_EGRESS_POLICY",
        "KNOWLEDGE_AGENT_FLASH_MODEL",
        "KNOWLEDGE_AGENT_PRO_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.knowledge_agent_api_key == ""
    assert settings.knowledge_agent_egress_policy == "none"
    assert settings.knowledge_agent_flash_model == "deepseek-v4-flash"
    assert settings.knowledge_agent_pro_model == "deepseek-v4-pro"
    get_settings.cache_clear()


def test_knowledge_agent_configuration_is_independent(monkeypatch) -> None:
    monkeypatch.setenv("UPSTREAM_API_KEY", "memory-key")
    monkeypatch.setenv("UPSTREAM_MODEL", "memory-model")
    monkeypatch.setenv("KNOWLEDGE_AGENT_API_KEY", "knowledge-key")
    monkeypatch.setenv("KNOWLEDGE_AGENT_FLASH_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PRO_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("KNOWLEDGE_AGENT_EGRESS_POLICY", "all")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.upstream_api_key == "memory-key"
    assert settings.knowledge_agent_api_key == "knowledge-key"
    assert settings.knowledge_agent_api_key != settings.upstream_api_key
    assert settings.knowledge_agent_egress_policy == "all"
    get_settings.cache_clear()


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
