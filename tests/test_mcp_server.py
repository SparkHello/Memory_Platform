"""MCP 端点测试：直接以 JSON-RPC over streamable HTTP 调用 /mcp。

服务端配置为 stateless + json_response，每个 POST 都是独立请求，
无需先走 initialize 握手，响应是普通 JSON 而不是 SSE 流。
"""

import json

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

EXPECTED_TOOLS = {
    "search_memory",
    "submit_memory_text",
    "get_recent_context_summary",
    "get_core_memory",
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
    instructions = result.get("instructions", "")
    assert "长期记忆" in instructions
    assert "你只有 4 个工具" in instructions

def test_mcp_tools_list(client, auth_headers):
    result = _post_mcp(client, auth_headers, "tools/list")
    names = {tool["name"] for tool in result["tools"]}
    assert names == EXPECTED_TOOLS

def test_submit_memory_text_splits_and_saves(
    client,
    auth_headers,
    memory_store,
    fake_llm,
    monkeypatch,
):
    import app.mcp_server.server as server_module

    monkeypatch.setattr(server_module, "get_llm_client", lambda settings: fake_llm)
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
                    "memory": "用户使用 iPhone，并用 Kelivo 作为 AI 客户端。",
                    "type": "fact",
                    "importance": 7,
                    "confidence": 0.9,
                    "stability": "medium",
                    "valid_until": None,
                    "review_after": None,
                    "sensitivity": "normal",
                    "reason": "用户明确描述设备与客户端",
                    "source_quote": "我现在用 iPhone 和 Kelivo 做 AI 客户端",
                },
            ],
            "reason": "拆分出两条长期信息",
        },
        ensure_ascii=False,
    )

    outcome = _call_tool(
        client,
        auth_headers,
        "submit_memory_text",
        {
            "text": "我喜欢黑咖啡。我现在用 iPhone 和 Kelivo 做 AI 客户端。",
            "conversation_id": "conv-ingest",
        },
    )

    assert outcome["created"] == 2
    assert outcome["updated"] == 0
    assert outcome["ignored"] == 0
    assert len(outcome["items"]) == 2

    contents = {memory.content for memory in memory_store.list_memories(user_id="default")}
    assert "用户喜欢黑咖啡。" in contents
    assert "用户使用 iPhone，并用 Kelivo 作为 AI 客户端。" in contents

    logs = memory_store.list_decision_logs(conversation_id="conv-ingest")
    assert len(logs) == 2
    assert {log.decision for log in logs} == {"create"}
    assert {json.loads(log.candidate_json)["source"] for log in logs} == {"mcp_ingest"}

def test_search_memory_finds_saved(client, auth_headers, memory_store):
    memory_store.create_memory(
        user_id="default",
        content="用户使用 Kelivo 作为 AI 客户端",
        type="fact",
        importance=7,
        confidence=0.95,
    )

    found = _call_tool(
        client,
        auth_headers,
        "search_memory",
        {"query": "用户的 AI 客户端是什么"},
    )
    assert any("Kelivo" in memory["content"] for memory in found)
    matched = next(memory for memory in found if "Kelivo" in memory["content"])
    assert matched["usage_count"] == 1
    assert matched["last_used_at"] is not None
    # 不应泄露向量字段
    assert all("embedding_json" not in memory for memory in found)

def test_review_memories_rest_endpoint(client, auth_headers, memory_store):
    expired = memory_store.create_memory(
        user_id="default",
        content="用户最近在减少咖啡摄入。",
        type="fact",
        importance=8,
        stability="temporary",
        valid_until="2020-01-01",
    )
    low_value = memory_store.create_memory(
        user_id="default",
        content="用户那天晚上吃了火锅。",
        type="fact",
        importance=2,
        stability="temporary",
        valid_until="2020-01-01",
    )

    response = client.post("/memories/review", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert any(
        recommendation["action"] == "lower" and recommendation["memory_ids"] == [expired.id]
        for recommendation in payload["recommendations"]
    )
    assert any(
        recommendation["action"] == "delete" and recommendation["memory_ids"] == [low_value.id]
        for recommendation in payload["recommendations"]
    )

def test_search_memory_empty(client, auth_headers):
    found = _call_tool(client, auth_headers, "search_memory", {"query": "随便问问"})
    assert found == []

def test_get_core_memory(client, auth_headers, memory_store):
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="communication",
        content="- 用户喜欢直接、实用的回答。",
        evidence_memory_ids=[],
        confidence=0.9,
    )

    core = _call_tool(client, auth_headers, "get_core_memory", {})

    assert core[0]["section"] == "communication"
    assert "直接、实用" in core[0]["content"]

def test_get_recent_context_summary_tool(client, auth_headers, memory_store):
    memory_store.upsert_recent_context_summary(
        user_id="default",
        conversation_id="conv-1",
        summary="用户：聊周末早餐",
    )

    summary = _call_tool(
        client,
        auth_headers,
        "get_recent_context_summary",
        {"conversation_id": "conv-1"},
    )

    assert summary["found"] is True
    assert summary["summary"] == "用户：聊周末早餐"

def test_user_isolation_via_header(client, auth_headers):
    alice = _user_headers(auth_headers, "alice")
    bob = _user_headers(auth_headers, "bob")

    client.post("/memories", headers=alice, json={
        "content": "用户使用 iPhone，并用 Kelivo 作为 AI 客户端",
        "type": "fact",
        "importance": 7,
        "confidence": 0.9,
        "source_quote": "我现在用 iPhone 和 Kelivo 做 AI 客户端",
    })

    alice_resp = client.get("/memories", headers=alice)
    bob_resp = client.get("/memories", headers=bob)
    assert len(alice_resp.json()["data"]) == 1
    assert bob_resp.json()["data"] == []

def test_decision_logs_are_scoped_via_header(client, auth_headers, memory_store):
    alice = _user_headers(auth_headers, "alice")
    bob = _user_headers(auth_headers, "bob")

    memory_store.create_memory(
        user_id="alice",
        content="用户使用 iPhone，并用 Kelivo 作为 AI 客户端",
        type="fact",
        importance=7,
    )
    memory_store.create_decision_log(
        user_id="alice",
        conversation_id=None,
        candidate_json='{"memory": "alice test"}',
        decision="create",
        reason="test",
    )
    memory_store.create_memory(
        user_id="bob",
        content="用户使用 Android，并用 Kelivo 作为 AI 客户端",
        type="fact",
        importance=7,
    )
    memory_store.create_decision_log(
        user_id="bob",
        conversation_id=None,
        candidate_json='{"memory": "bob test"}',
        decision="create",
        reason="test",
    )

    alice_response = client.get("/memories/decision-logs", headers=alice)
    bob_response = client.get("/memories/decision-logs", headers=bob)

    assert alice_response.status_code == 200
    assert bob_response.status_code == 200

    alice_logs = alice_response.json()["data"]
    bob_logs = bob_response.json()["data"]

    assert len(alice_logs) == 1
    assert len(bob_logs) == 1
    assert alice_logs[0]["user_id"] == "alice"
    assert bob_logs[0]["user_id"] == "bob"

def test_rest_endpoints_still_work(client, auth_headers):
    # mount("/") 兜底不应影响 FastAPI 自有路由
    response = client.get("/health")
    assert response.status_code == 200

    response = client.get("/memories", headers=auth_headers)
    assert response.status_code == 200
