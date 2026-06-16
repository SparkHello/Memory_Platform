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


def _post_ingest(
    client: TestClient,
    auth_headers: dict[str, str],
    content: str,
    conversation_id: str | None = None,
):
    payload: dict = {"text": content}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return client.post("/memories/ingest", headers=auth_headers, json=payload)


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

    response = _post_ingest(client, auth_headers, "我今天有点困。")

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

    response = _post_ingest(client, auth_headers, "如果我以后用 Mac，应该怎么配置？")

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

    response = _post_ingest(client, auth_headers, "帮我推荐一台笔记本。")

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

    response = _post_ingest(client, auth_headers, "我有一项健康隐私。")

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

    response = _post_ingest(client, auth_headers, "我喜欢黑咖啡。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "相同记忆" in logs[0].reason


def test_new_detail_creates_related_memory_without_overwriting(
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

    response = _post_ingest(client, auth_headers, "我现在用 iPhone 和 Kelivo 做 AI 客户端。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 2
    original = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert original is not None
    assert original.content == "用户使用 iPhone。"
    assert any("Kelivo" in memory.content for memory in memories if memory.id != old.id)
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "create"
    assert "暂不自动合并" in logs[0].reason


def test_conflicting_memory_creates_related_memory_when_explicit(
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
    response = _post_ingest(client, auth_headers, "我可能会换 ChatWise 吧，还没想好。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert memories[0].content == "用户的 AI 客户端是 Kelivo。"
    assert memory_store.list_decision_logs()[0].decision == "ignore"

    # 用户明确表达了新事实：保留旧时间线，新建并交给体检建议确认
    fake_llm.extraction_content = _extraction_json(
        memory="用户的 AI 客户端是 ChatWise。",
        confidence=0.95,
        source_quote="我已经换成 ChatWise 了",
    )
    response = _post_ingest(client, auth_headers, "我已经换成 ChatWise 了。")

    assert response.status_code == 200
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 2
    original = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert original is not None
    assert original.content == "用户的 AI 客户端是 Kelivo。"
    assert any("ChatWise" in memory.content for memory in memories if memory.id != old.id)
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "create"
    assert "暂不自动合并" in logs[0].reason


def test_invalid_extractor_json_does_not_break_chat(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = "抱歉，我没法输出你要的格式。"

    response = _post_ingest(client, auth_headers, "随便聊聊天气吧。")

    assert response.status_code == 200
    assert response.json()["ignored"] == 1
    assert memory_store.list_memories(user_id="default") == []
    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "ignore"
    assert "JSON" in logs[0].reason


def test_rest_ingest_splits_raw_text(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "memories": [
                {
                    "action": "create",
                    "memory": "用户喜欢黑咖啡。",
                    "type": "preference",
                    "importance": 7,
                    "confidence": 0.9,
                    "stability": "stable",
                    "valid_until": None,
                    "review_after": None,
                    "sensitivity": "normal",
                    "reason": "用户明确表达长期偏好",
                    "source_quote": "我喜欢黑咖啡",
                },
                {
                    "action": "create",
                    "memory": "用户使用 iPhone。",
                    "type": "fact",
                    "importance": 7,
                    "confidence": 0.9,
                    "stability": "stable",
                    "valid_until": None,
                    "review_after": None,
                    "sensitivity": "normal",
                    "reason": "用户明确描述设备",
                    "source_quote": "我现在用 iPhone",
                },
            ],
            "reason": "拆分出两条长期信息",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/memories/ingest",
        headers=auth_headers,
        json={
            "text": "我喜欢黑咖啡，另外我现在用 iPhone。",
            "conversation_id": "rest-conv",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] == 2
    assert payload["ignored"] == 0
    assert len(memory_store.list_memories(user_id="default")) == 2
    logs = memory_store.list_decision_logs(conversation_id="rest-conv")
    assert len(logs) == 2
    assert {json.loads(log.candidate_json)["source"] for log in logs} == {"rest_ingest"}


def test_decision_logs_record_create_related_create_ignore(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    # 第一轮：全新信息 -> create
    fake_llm.extraction_content = _extraction_json()
    _post_ingest(client, auth_headers, "我现在用 iPhone。", conversation_id="conv-create")

    # 第二轮：补充细节 -> create，并提示人工确认
    fake_llm.extraction_content = _extraction_json(
        memory="用户使用 iPhone，并在尝试用 Kelivo 作为 AI 客户端前端。",
        source_quote="我现在用 iPhone 和 Kelivo 做 AI 客户端",
    )
    _post_ingest(
        client,
        auth_headers,
        "我现在用 iPhone 和 Kelivo 做 AI 客户端。",
        conversation_id="conv-update",
    )

    # 第三轮：原样重复 -> ignore
    _post_ingest(
        client,
        auth_headers,
        "我现在用 iPhone 和 Kelivo 做 AI 客户端。",
        conversation_id="conv-ignore",
    )

    logs = memory_store.list_decision_logs()
    decisions = {log.conversation_id: log.decision for log in logs}
    assert decisions == {
        "conv-create": "create",
        "conv-update": "create",
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
