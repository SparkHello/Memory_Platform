import json

from fastapi.testclient import TestClient

from app.memory.store import MemoryStore


def test_chat_completion_requires_auth(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "ios-model", "messages": [{"role": "user", "content": "你好"}]},
    )

    assert response.status_code == 401


def test_chat_completion_injects_memory_context(
    client: TestClient,
    auth_headers: dict[str, str],
    memory_store: MemoryStore,
    fake_llm,
) -> None:
    memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="preference",
        importance=4,
    )

    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ios-model",
            "messages": [{"role": "user", "content": "我想喝咖啡，帮我推荐早餐。"}],
            "temperature": 0.7,
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "好的，我会参考这些信息。"
    assert fake_llm.messages[0]["role"] == "system"
    assert "长期记忆" in fake_llm.messages[0]["content"]
    assert "黑咖啡" in fake_llm.messages[0]["content"]


def test_streaming_returns_not_implemented(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ios-model",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": True,
        },
    )

    assert response.status_code == 501


def test_chat_completion_returns_chinese_without_mojibake(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_llm,
) -> None:
    fake_llm.response["choices"][0]["message"]["content"] = "好的，我已经记住你喜欢黑咖啡。"

    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ios-model",
            "messages": [{"role": "user", "content": "我喜欢黑咖啡，请记住。"}],
        },
    )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert content == "好的，我已经记住你喜欢黑咖啡。"
    assert "å" not in content
    assert "ç" not in content


def test_chat_completion_removes_reasoning_content(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_llm,
) -> None:
    fake_llm.response["choices"][0]["message"] = {
        "role": "assistant",
        "content": "这是给客户端看的回答。",
        "reasoning_content": "这里是模型推理过程，不能透传。",
        "tool_calls": [{"id": "call-1"}],
    }

    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ios-model",
            "messages": [{"role": "user", "content": "你好"}],
        },
    )

    assert response.status_code == 200
    message = response.json()["choices"][0]["message"]
    assert message == {"role": "assistant", "content": "这是给客户端看的回答。"}
    assert "reasoning_content" not in message
    assert "tool_calls" not in message


def test_chat_completion_saves_preference_memory(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_llm,
) -> None:
    fake_llm.extraction_content = json.dumps(
        {
            "action": "create",
            "memory": "用户喜欢黑咖啡。",
            "type": "preference",
            "importance": 7,
            "confidence": 0.9,
            "reason": "用户明确表达的长期偏好",
            "source_quote": "我喜欢黑咖啡",
        },
        ensure_ascii=False,
    )

    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ios-model",
            "messages": [{"role": "user", "content": "我喜欢黑咖啡，请记住。"}],
        },
    )

    assert response.status_code == 200
    memories_response = client.get("/memories", headers=auth_headers)

    assert memories_response.status_code == 200
    data = memories_response.json()["data"]
    contents = [memory["content"] for memory in data]
    assert "用户喜欢黑咖啡。" in contents
    # 列表接口不应再返回向量字段
    assert all("embedding_json" not in memory for memory in data)
