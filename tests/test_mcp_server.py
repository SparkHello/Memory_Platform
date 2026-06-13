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
    "save_memory",
    "why_remember",
    "merge_memories",
    "get_recent_context_summary",
    "get_core_memory",
    "get_core_memory_history",
    "consolidate_core_memory",
    "review_memories",
    "memory_report",
    "export_memories",
    "list_memories",
    "list_deleted_memories",
    "delete_memory",
    "restore_memory",
    "forget_memories",
}

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
    instructions = result.get("instructions", "")
    assert "长期记忆" in instructions
    assert "不是二选一" in instructions


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


def test_save_memory_accepts_review_after(client, auth_headers, memory_store):
    outcome = _call_tool(
        client,
        auth_headers,
        "save_memory",
        {**VALID_SAVE_ARGUMENTS, "review_after": "2020-01-01"},
    )

    memory = memory_store.get_memory(
        memory_id=outcome["memory_id"],
        user_id="default",
    )

    assert outcome["action"] == "create"
    assert memory is not None
    assert memory.review_after == "2020-01-01"


def test_why_remember_returns_source_and_core_evidence(client, auth_headers, memory_store):
    outcome = _call_tool(client, auth_headers, "save_memory", VALID_SAVE_ARGUMENTS)
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="- 用户使用 iPhone 和 Kelivo。",
        evidence_memory_ids=[outcome["memory_id"]],
        confidence=0.9,
    )

    explanation = _call_tool(
        client,
        auth_headers,
        "why_remember",
        {"memory_id": outcome["memory_id"]},
    )

    assert explanation["found"] is True
    assert explanation["source_excerpt"] == VALID_SAVE_ARGUMENTS["source_quote"]
    assert explanation["confidence"] == VALID_SAVE_ARGUMENTS["confidence"]
    assert explanation["is_core_memory_evidence"] is True
    assert explanation["core_memory_sections"] == ["profile"]


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
    saved = _call_tool(client, auth_headers, "save_memory", VALID_SAVE_ARGUMENTS)

    found = _call_tool(
        client,
        auth_headers,
        "search_memory",
        {"query": "用户的 AI 客户端是什么"},
    )
    assert any("Kelivo" in memory["content"] for memory in found)
    matched = next(memory for memory in found if memory["id"] == saved["memory_id"])
    assert matched["usage_count"] == 1
    assert matched["last_used_at"] is not None
    # 不应泄露向量字段
    assert all("embedding_json" not in memory for memory in found)
    assert matched["stability"] == "stable"
    assert matched["sensitivity"] == "normal"


def test_save_memory_reports_supersede_relation(client, auth_headers, memory_store):
    _call_tool(
        client,
        auth_headers,
        "save_memory",
        {
            "memory": "用户的 AI 客户端是 Kelivo。",
            "type": "fact",
            "importance": 7,
            "confidence": 0.9,
            "source_quote": "我现在用 Kelivo",
            "reason": "用户明确描述当前客户端",
        },
    )

    outcome = _call_tool(
        client,
        auth_headers,
        "save_memory",
        {
            "memory": "用户的 AI 客户端是 ChatWise。",
            "type": "fact",
            "importance": 8,
            "confidence": 0.95,
            "source_quote": "我已经换成 ChatWise 了",
            "reason": "用户明确描述新客户端",
        },
    )

    assert outcome["action"] == "update"
    assert outcome["relation"] == "supersede"
    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    assert "ChatWise" in memories[0].content


def test_review_memories_tool_reports_recommendations(client, auth_headers, memory_store):
    first = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="preference",
        importance=7,
    )
    second = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="preference",
        importance=7,
    )

    outcome = _call_tool(client, auth_headers, "review_memories", {})

    assert outcome["total"] == 2
    assert any(
        recommendation["action"] == "merge"
        and set(recommendation["memory_ids"]) == {first.id, second.id}
        for recommendation in outcome["recommendations"]
    )


def test_merge_memories_tool_archives_fragments(client, auth_headers, memory_store):
    first = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="preference",
        importance=7,
    )
    second = memory_store.create_memory(
        user_id="default",
        content="用户喜欢浅烘咖啡豆。",
        type="preference",
        importance=6,
    )

    outcome = _call_tool(
        client,
        auth_headers,
        "merge_memories",
        {
            "memory_ids": [first.id, second.id],
            "content": "用户喜欢黑咖啡，偏好浅烘咖啡豆。",
        },
    )

    assert outcome["action"] == "update"
    assert outcome["memory"]["id"] == first.id
    assert outcome["memory"]["evidence_memory_ids"] == [first.id, second.id]
    assert outcome["archived_memory_ids"] == [second.id]
    assert memory_store.get_memory(memory_id=second.id, user_id="default") is None


def test_review_memories_reports_due_review_after(client, auth_headers, memory_store):
    due = memory_store.create_memory(
        user_id="default",
        content="用户最近在准备旅行。",
        type="fact",
        importance=7,
        review_after="2020-01-01",
    )

    outcome = _call_tool(client, auth_headers, "review_memories", {})

    assert any(
        recommendation["action"] == "review"
        and recommendation["memory_ids"] == [due.id]
        and "复核时间" in recommendation["reason"]
        for recommendation in outcome["recommendations"]
    )


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


def test_save_memory_rejects_sensitive_without_explicit_memory_request(
    client, auth_headers, memory_store
):
    outcome = _call_tool(
        client,
        auth_headers,
        "save_memory",
        {
            **VALID_SAVE_ARGUMENTS,
            "memory": "用户有一项健康隐私。",
            "importance": 8,
            "confidence": 0.95,
            "sensitivity": "sensitive",
            "source_quote": "我有一项健康隐私",
        },
    )

    assert outcome["action"] == "ignore"
    assert "敏感信息" in outcome["reason"] or "隐私" in outcome["reason"]
    assert memory_store.list_memories(user_id="default") == []


def test_save_memory_accepts_person_type(client, auth_headers, memory_store):
    outcome = _call_tool(
        client,
        auth_headers,
        "save_memory",
        {
            "memory": "用户的朋友小林正在准备考研",
            "type": "person",
            "importance": 7,
            "confidence": 0.9,
            "source_quote": "我朋友小林正在准备考研",
            "reason": "用户明确提到重要朋友的信息",
        },
    )

    assert outcome["action"] == "create"
    memories = memory_store.list_memories(user_id="default")
    assert memories[0].type == "person"


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


def test_get_core_memory_history_tool(client, auth_headers, memory_store):
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="communication",
        content="- 用户喜欢直接回答。",
        evidence_memory_ids=[],
        confidence=0.85,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="communication",
        content="- 用户喜欢直接、实用的回答。",
        evidence_memory_ids=[],
        confidence=0.9,
    )

    history = _call_tool(
        client,
        auth_headers,
        "get_core_memory_history",
        {"section": "communication"},
    )

    assert len(history) == 1
    assert history[0]["version"] == 1
    assert "直接回答" in history[0]["content"]


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


def test_mcp_report_export_and_restore_memory(client, auth_headers, memory_store):
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes espresso.",
        type="preference",
        importance=7,
    )

    report = _call_tool(client, auth_headers, "memory_report", {"format": "json"})
    assert report["counts"]["active_memories"] == 1
    assert "markdown" in report

    exported = _call_tool(client, auth_headers, "export_memories", {"format": "json"})
    assert exported["embedding_included"] is False
    assert exported["memories"][0]["id"] == memory.id

    deleted = _call_tool(client, auth_headers, "delete_memory", {"memory_id": memory.id})
    assert deleted["deleted"] is True

    deleted_memories = _call_tool(client, auth_headers, "list_deleted_memories", {})
    assert [item["id"] for item in deleted_memories] == [memory.id]

    restored = _call_tool(client, auth_headers, "restore_memory", {"memory_id": memory.id})
    assert restored["restored"] is True
    assert restored["memory"]["id"] == memory.id
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is not None


def test_forget_memories_deletes_by_query(client, auth_headers, memory_store):
    coffee = _call_tool(
        client,
        auth_headers,
        "save_memory",
        {
            **VALID_SAVE_ARGUMENTS,
            "memory": "用户喜欢黑咖啡",
            "type": "preference",
            "source_quote": "我喜欢黑咖啡",
            "reason": "用户明确表达饮食偏好",
        },
    )
    phone = _call_tool(client, auth_headers, "save_memory", VALID_SAVE_ARGUMENTS)

    outcome = _call_tool(
        client,
        auth_headers,
        "forget_memories",
        {"query": "咖啡"},
    )

    assert outcome["deleted_count"] == 1
    assert outcome["query"] == "咖啡"
    assert outcome["deleted"][0]["id"] == coffee["memory_id"]
    remaining_ids = {
        memory.id for memory in memory_store.list_memories(user_id="default")
    }
    assert coffee["memory_id"] not in remaining_ids
    assert phone["memory_id"] in remaining_ids


def test_user_isolation_via_header(client, auth_headers):
    alice = _user_headers(auth_headers, "alice")
    bob = _user_headers(auth_headers, "bob")

    _call_tool(client, alice, "save_memory", VALID_SAVE_ARGUMENTS)

    alice_memories = _call_tool(client, alice, "list_memories", {})
    bob_memories = _call_tool(client, bob, "list_memories", {})
    assert len(alice_memories) == 1
    assert bob_memories == []


def test_decision_logs_are_scoped_via_header(client, auth_headers):
    alice = _user_headers(auth_headers, "alice")
    bob = _user_headers(auth_headers, "bob")

    _call_tool(client, alice, "save_memory", VALID_SAVE_ARGUMENTS)
    _call_tool(
        client,
        bob,
        "save_memory",
        {
            **VALID_SAVE_ARGUMENTS,
            "memory": "用户使用 Android，并用 Kelivo 作为 AI 客户端",
            "source_quote": "我现在用 Android 和 Kelivo 做 AI 客户端",
        },
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
