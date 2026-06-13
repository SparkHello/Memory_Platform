"""MCP 端点测试：直接以 JSON-RPC over streamable HTTP 调用 /mcp。

服务端配置为 stateless + json_response，每个 POST 都是独立请求，
无需先走 initialize 握手，响应是普通 JSON 而不是 SSE 流。
"""

import json

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

EXPECTED_TOOLS = {"search_memory", "save_memory", "list_memories", "delete_memory"}

VALID_SAVE_ARGUMENTS = {
    "memory": "用户使用 iPhone，并用 Kelivo 作为 AI 客户端",
    "type": "fact",
    "importance": 7,
    "confidence": 0.9,
    "source_quote": "我现在用 iPhone 和 Kelivo 做 AI 客户端",
    "reason": "用户明确描述了自己的设备与客户端",
}


def _rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _post_mcp(client, auth_headers, method: str, params: dict | None = None) -> dict:
    response = client.post(
        "/mcp",
        headers={**auth_headers, **MCP_HEADERS},
        json=_rpc(method, params),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def _call_tool(client, auth_headers, name: str, arguments: dict) -> dict:
    result = _post_mcp(
        client,
        auth_headers,
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    assert result.get("isError") is not True, result
    text = result["content"][0]["text"]
    return json.loads(text)


def _user_headers(auth_headers: dict, user_id: str | None = None) -> dict:
    headers = dict(auth_headers)
    if user_id:
        headers["X-User-Id"] = user_id
    return headers


def test_mcp_requires_auth(client):
    response = client.post("/mcp", headers=MCP_HEADERS, json=_rpc("tools/list"))
    assert response.status_code == 401


def test_mcp_rejects_wrong_key(client):
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer wrong-key", **MCP_HEADERS},
        json=_rpc("tools/list"),
    )
    assert response.status_code == 401


def test_mcp_initialize(client, auth_headers):
    result = _post_mcp(
        client,
        auth_headers,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    )
    assert result["serverInfo"]["name"] == "memory-gateway"
    assert "长期记忆" in result.get("instructions", "")


def test_mcp_tools_list(client, auth_headers):
    result = _post_mcp(client, auth_headers, "tools/list")
    names = {tool["name"] for tool in result["tools"]}
    assert names == EXPECTED_TOOLS


def test_save_memory_creates_and_logs(client, auth_headers, memory_store):
    outcome = _call_tool(client, auth_headers, "save_memory", VALID_SAVE_ARGUMENTS)
    assert outcome["action"] == "create"
    assert outcome["memory_id"]

    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    assert memories[0].content == VALID_SAVE_ARGUMENTS["memory"]

    logs = memory_store.list_decision_logs()
    assert logs[0].decision == "create"
    assert json.loads(logs[0].candidate_json)["source"] == "mcp"


def test_save_memory_rejects_low_importance(client, auth_headers, memory_store):
    arguments = {**VALID_SAVE_ARGUMENTS, "importance": 3}
    outcome = _call_tool(client, auth_headers, "save_memory", arguments)
    assert outcome["action"] == "ignore"
    assert "importance" in outcome["reason"]

    assert memory_store.list_memories(user_id="default") == []
    assert memory_store.list_decision_logs()[0].decision == "ignore"


def test_save_memory_rejects_low_confidence(client, auth_headers, memory_store):
    arguments = {**VALID_SAVE_ARGUMENTS, "confidence": 0.5}
    outcome = _call_tool(client, auth_headers, "save_memory", arguments)
    assert outcome["action"] == "ignore"
    assert "confidence" in outcome["reason"]
    assert memory_store.list_memories(user_id="default") == []


def test_save_memory_rejects_missing_quote(client, auth_headers, memory_store):
    arguments = {**VALID_SAVE_ARGUMENTS, "source_quote": ""}
    outcome = _call_tool(client, auth_headers, "save_memory", arguments)
    assert outcome["action"] == "ignore"
    assert "source_quote" in outcome["reason"]
    assert memory_store.list_memories(user_id="default") == []


def test_save_memory_rejects_assumption(client, auth_headers, memory_store):
    arguments = {
        **VALID_SAVE_ARGUMENTS,
        "memory": "用户使用 Mac",
        "source_quote": "如果我以后用 Mac，应该怎么配置",
    }
    outcome = _call_tool(client, auth_headers, "save_memory", arguments)
    assert outcome["action"] == "ignore"
    assert "假设场景" in outcome["reason"]
    assert memory_store.list_memories(user_id="default") == []


def test_save_memory_deduplicates(client, auth_headers, memory_store):
    first = _call_tool(client, auth_headers, "save_memory", VALID_SAVE_ARGUMENTS)
    assert first["action"] == "create"

    second = _call_tool(client, auth_headers, "save_memory", VALID_SAVE_ARGUMENTS)
    assert second["action"] == "ignore"
    assert len(memory_store.list_memories(user_id="default")) == 1


def test_search_memory_finds_saved(client, auth_headers):
    _call_tool(client, auth_headers, "save_memory", VALID_SAVE_ARGUMENTS)

    found = _call_tool(
        client,
        auth_headers,
        "search_memory",
        {"query": "用户的 AI 客户端是什么"},
    )
    assert any("Kelivo" in memory["content"] for memory in found)
    # 不应泄露向量字段
    assert all("embedding_json" not in memory for memory in found)


def test_search_memory_empty(client, auth_headers):
    found = _call_tool(client, auth_headers, "search_memory", {"query": "随便问问"})
    assert found == []


def test_list_and_delete_memory(client, auth_headers, memory_store):
    saved = _call_tool(client, auth_headers, "save_memory", VALID_SAVE_ARGUMENTS)

    listed = _call_tool(client, auth_headers, "list_memories", {})
    assert [memory["id"] for memory in listed] == [saved["memory_id"]]

    deleted = _call_tool(
        client, auth_headers, "delete_memory", {"memory_id": saved["memory_id"]}
    )
    assert deleted["deleted"] is True
    assert memory_store.list_memories(user_id="default") == []

    again = _call_tool(
        client, auth_headers, "delete_memory", {"memory_id": saved["memory_id"]}
    )
    assert again["deleted"] is False


def test_user_isolation_via_header(client, auth_headers):
    alice = _user_headers(auth_headers, "alice")
    bob = _user_headers(auth_headers, "bob")

    _call_tool(client, alice, "save_memory", VALID_SAVE_ARGUMENTS)

    alice_memories = _call_tool(client, alice, "list_memories", {})
    bob_memories = _call_tool(client, bob, "list_memories", {})
    assert len(alice_memories) == 1
    assert bob_memories == []


def test_rest_endpoints_still_work(client, auth_headers):
    # mount("/") 兜底不应影响 FastAPI 自有路由
    response = client.get("/health")
    assert response.status_code == 200

    response = client.get("/memories", headers=auth_headers)
    assert response.status_code == 200
