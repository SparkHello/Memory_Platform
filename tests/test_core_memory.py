import json

from fastapi.testclient import TestClient

from app.memory.core import _select_source_memories
from app.memory.models import MemoryRecord
from app.memory.store import MemoryStore


def test_core_memory_consolidation_creates_section(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    preference = memory_store.create_memory(
        user_id="default",
        content="用户长期喜欢黑咖啡。",
        type="emotional",
        importance=8,
        confidence=0.9,
    )
    fake_llm.core_content = json.dumps(
        {
            "sections": [
                {
                    "section": "preferences",
                    "content": "- 用户长期喜欢黑咖啡。",
                    "evidence_memory_ids": [preference.id],
                    "confidence": 0.92,
                }
            ],
            "reason": "从高重要性长期偏好中整理核心记忆",
        },
        ensure_ascii=False,
    )

    response = client.post("/memories/core/consolidate", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] == 1
    assert payload["updated"] == 0
    assert payload["sections"][0]["section"] == "preferences"
    assert payload["sections"][0]["evidence_memory_ids"] == [preference.id]

    listed = client.get("/memories/core", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["data"][0]["content"] == "- 用户长期喜欢黑咖啡。"


def test_core_memory_consolidation_excludes_sensitive_memory(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    normal = memory_store.create_memory(
        user_id="default",
        content="用户长期喜欢黑咖啡。",
        type="emotional",
        importance=8,
        confidence=0.9,
    )
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户有一项健康隐私。",
        type="semantic",
        importance=10,
        confidence=0.95,
        sensitivity="sensitive",
    )
    fake_llm.core_content = json.dumps(
        {
            "sections": [
                {
                    "section": "profile",
                    "content": "- 用户有一项健康隐私。",
                    "evidence_memory_ids": [sensitive.id],
                    "confidence": 0.95,
                },
                {
                    "section": "preferences",
                    "content": "- 用户长期喜欢黑咖啡。",
                    "evidence_memory_ids": [normal.id],
                    "confidence": 0.9,
                },
            ],
            "reason": "测试敏感记忆过滤",
        },
        ensure_ascii=False,
    )

    response = client.post("/memories/core/consolidate", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] == 1
    assert payload["ignored"] == 1
    assert payload["sections"][0]["section"] == "preferences"
    assert "健康隐私" not in payload["sections"][0]["content"]


def test_core_memory_selection_rejects_legacy_mislabeled_sensitive_text() -> None:
    memory = MemoryRecord(
        id="legacy-mislabeled",
        user_id="default",
        content="用户的身份证号是 123456789012345678。",
        importance=10,
        confidence=0.99,
        sensitivity="normal",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    assert _select_source_memories([memory]) == []


def test_core_consolidation_does_not_send_sensitive_existing_section(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    source = memory_store.create_memory(
        user_id="default",
        content="用户长期喜欢黑咖啡。",
        importance=8,
        confidence=0.9,
    )
    secret = "123456789012345678"
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content=f"- 用户的身份证号是 {secret}。",
        evidence_memory_ids=[source.id],
        confidence=0.95,
    )

    response = client.post("/memories/core/consolidate", headers=auth_headers)

    assert response.status_code == 200
    outbound = json.dumps(fake_llm.core_messages, ensure_ascii=False)
    assert secret not in outbound
    assert "身份证号" not in outbound


def test_core_consolidation_rejects_sensitive_model_output(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    source = memory_store.create_memory(
        user_id="default",
        content="用户长期喜欢黑咖啡。",
        importance=8,
        confidence=0.9,
    )
    secret = "123456789012345678"
    fake_llm.core_content = json.dumps(
        {
            "sections": [
                {
                    "section": "profile",
                    "content": f"- 用户的身份证号是 {secret}。",
                    "evidence_memory_ids": [source.id],
                    "confidence": 0.99,
                }
            ],
            "reason": "测试敏感模型输出",
        },
        ensure_ascii=False,
    )

    response = client.post("/memories/core/consolidate", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["created"] == 0
    assert response.json()["ignored"] == 1
    assert memory_store.list_core_memory_sections(user_id="default") == []
    assert memory_store.list_core_memory_section_history(user_id="default") == []


def test_core_consolidation_rejects_unrelated_model_output(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    source = memory_store.create_memory(
        user_id="default",
        content="用户长期喜欢黑咖啡。",
        importance=8,
        confidence=0.9,
    )
    fake_llm.core_content = json.dumps(
        {
            "sections": [
                {
                    "section": "profile",
                    "content": "- 用户住在北京。",
                    "evidence_memory_ids": [source.id],
                    "confidence": 0.99,
                }
            ],
            "reason": "测试无关模型输出",
        },
        ensure_ascii=False,
    )

    response = client.post("/memories/core/consolidate", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["created"] == 0
    assert response.json()["ignored"] == 1
    assert memory_store.list_core_memory_sections(user_id="default") == []
