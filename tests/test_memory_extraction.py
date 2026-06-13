"""记忆提取与解析的行为测试。

覆盖保存门槛（importance / confidence / source_quote / 假设场景）、
去重与更新逻辑，以及 memory_decision_logs 的记录行为。
"""

import json

from fastapi.testclient import TestClient

from app.memory.store import MemoryStore


def _extraction_json(**overrides) -> str:
    data = {
        "action": "create",
        "memory": "用户使用 iPhone。",
        "type": "fact",
        "importance": 8,
        "confidence": 0.9,
        "reason": "用户明确表达的长期事实",
        "source_quote": "我现在用 iPhone",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _post_chat(
    client: TestClient,
    auth_headers: dict[str, str],
    content: str,
    conversation_id: str | None = None,
):
    payload: dict = {
        "model": "ios-model",
        "messages": [{"role": "user", "content": content}],
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return client.post("/v1/chat/completions", headers=auth_headers, json=payload)


def test_low_importance_memory_is_not_saved(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户今天有点困。",
        importance=3,
        source_quote="我今天有点困",
    )

    response = _post_chat(client, auth_headers, "我今天有点困。")

    assert response.status_code == 200
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert len(logs) == 1
    assert logs[0].decision == "ignore"
    assert "importance" in logs[0].reason


def test_hypothetical_scenario_is_not_saved(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    # 即使提取模型给出了高分，代码层也必须拦下假设场景
    fake_llm.extraction_content = _extraction_json(
        memory="用户使用 Mac。",
        importance=8,
        confidence=0.9,
        source_quote="我以后用 Mac",
    )

    response = _post_chat(client, auth_headers, "如果我以后用 Mac，应该怎么配置？")

    assert response.status_code == 200
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "假设场景" in logs[0].reason


def test_fabricated_source_quote_is_not_saved(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(source_quote="我是 Mac 重度用户")

    response = _post_chat(client, auth_headers, "帮我推荐一台笔记本。")

    assert response.status_code == 200
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "source_quote" in logs[0].reason


def test_sensitive_memory_requires_explicit_memory_request(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = _extraction_json(
        memory="用户有一项健康隐私。",
        type="fact",
        importance=8,
        confidence=0.95,
        sensitivity="sensitive",
        source_quote="我有一项健康隐私",
    )

    response = _post_chat(client, auth_headers, "我有一项健康隐私。")

    assert response.status_code == 200
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "敏感信息" in logs[0].reason or "隐私" in logs[0].reason


def test_similar_memory_is_not_duplicated(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="preference",
        importance=7,
        confidence=0.9,
    )
    fake_llm.extraction_content = _extraction_json(
        memory="用户喜欢黑咖啡。",
        type="preference",
        source_quote="我喜欢黑咖啡",
    )

    response = _post_chat(client, auth_headers, "我喜欢黑咖啡。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "相同记忆" in logs[0].reason


def test_new_detail_updates_existing_memory(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户使用 iPhone。",
        type="fact",
        importance=7,
        confidence=0.9,
    )
    fake_llm.extraction_content = _extraction_json(
        memory="用户使用 iPhone，并在尝试用 Kelivo 作为 AI 客户端前端。",
        source_quote="我现在用 iPhone 和 Kelivo 做 AI 客户端",
    )

    response = _post_chat(client, auth_headers, "我现在用 iPhone 和 Kelivo 做 AI 客户端。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    updated = memories[0]
    assert updated.id == old.id
    assert "Kelivo" in updated.content
    assert updated.created_at == old.created_at
    assert updated.updated_at != old.updated_at


def test_conflicting_memory_updates_only_when_explicit(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户的 AI 客户端是 Kelivo。",
        type="fact",
        importance=7,
        confidence=0.9,
    )

    # 只是猜测（confidence 低）：不允许覆盖旧记忆
    fake_llm.extraction_content = _extraction_json(
        memory="用户的 AI 客户端是 ChatWise。",
        confidence=0.5,
        source_quote="我可能会换 ChatWise",
    )
    response = _post_chat(client, auth_headers, "我可能会换 ChatWise 吧，还没想好。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert memories[0].content == "用户的 AI 客户端是 Kelivo。"
    assert memory_store.list_decision_logs()[0].decision == "ignore"

    # 用户明确表达了新事实：更新旧记忆而不是新建
    fake_llm.extraction_content = _extraction_json(
        memory="用户的 AI 客户端是 ChatWise。",
        confidence=0.95,
        source_quote="我已经换成 ChatWise 了",
    )
    response = _post_chat(client, auth_headers, "我已经换成 ChatWise 了。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    assert memories[0].id == old.id
    assert "ChatWise" in memories[0].content
    assert memories[0].created_at == old.created_at


def test_invalid_extractor_json_does_not_break_chat(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = "抱歉，我没法输出你要的格式。"

    response = _post_chat(client, auth_headers, "随便聊聊天气吧。")

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"]
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "JSON" in logs[0].reason


def test_decision_logs_record_create_update_ignore(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    # 第一轮：全新信息 -> create
    fake_llm.extraction_content = _extraction_json()
    _post_chat(client, auth_headers, "我现在用 iPhone。", conversation_id="conv-create")

    # 第二轮：补充细节 -> update
    fake_llm.extraction_content = _extraction_json(
        memory="用户使用 iPhone，并在尝试用 Kelivo 作为 AI 客户端前端。",
        source_quote="我现在用 iPhone 和 Kelivo 做 AI 客户端",
    )
    _post_chat(
        client,
        auth_headers,
        "我现在用 iPhone 和 Kelivo 做 AI 客户端。",
        conversation_id="conv-update",
    )

    # 第三轮：原样重复 -> ignore
    _post_chat(
        client,
        auth_headers,
        "我现在用 iPhone 和 Kelivo 做 AI 客户端。",
        conversation_id="conv-ignore",
    )

    logs = memory_store.list_decision_logs()
    decisions = {log.conversation_id: log.decision for log in logs}
    assert decisions == {
        "conv-create": "create",
        "conv-update": "update",
        "conv-ignore": "ignore",
    }
    # candidate_json 必须是可回放的合法 JSON，方便调试
    for log in logs:
        parsed = json.loads(log.candidate_json)
        assert parsed["action"] in {"create", "update", "ignore"}

    # 按 conversation_id 过滤
    filtered = memory_store.list_decision_logs(conversation_id="conv-create")
    assert len(filtered) == 1
    assert filtered[0].decision == "create"
