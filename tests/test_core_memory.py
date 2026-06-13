import json

from fastapi.testclient import TestClient

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
        type="preference",
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


def test_chat_completion_injects_core_memory(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="communication",
        content="- 用户喜欢直接、实用的回答。",
        evidence_memory_ids=[],
        confidence=0.9,
    )

    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ios-model",
            "messages": [{"role": "user", "content": "随便聊聊天气。"}],
        },
    )

    assert response.status_code == 200
    assert fake_llm.messages[0]["role"] == "system"
    assert "核心记忆" in fake_llm.messages[0]["content"]
    assert "直接、实用" in fake_llm.messages[0]["content"]


def test_core_memory_consolidation_excludes_sensitive_memory(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    normal = memory_store.create_memory(
        user_id="default",
        content="用户长期喜欢黑咖啡。",
        type="preference",
        importance=8,
        confidence=0.9,
    )
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户有一项健康隐私。",
        type="fact",
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
