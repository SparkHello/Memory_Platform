"""MCP 端点测试：直接以 JSON-RPC over streamable HTTP 调用 /mcp。

服务端配置为 stateless + json_response，每个 POST 都是独立请求，
无需先走 initialize 握手，响应是普通 JSON 而不是 SSE 流。
"""

import json

from app.config import get_settings

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

EXPECTED_TOOLS = {
    "search_memory",
    "surface_memories",
    "submit_memory_text",
    "update_recent_context_summary",
    "get_recent_context_summary",
    "get_core_memory",
    "digest_memories",
    "list_knowledge_documents",
    "search_knowledge",
    "read_knowledge",
    "begin_knowledge_upload",
    "append_knowledge_upload",
    "commit_knowledge_upload",
    "manage_knowledge_document",
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
    assert "独立长文本知识" in instructions
    assert "你只有" not in instructions

def test_mcp_tools_list(client, auth_headers):
    result = _post_mcp(client, auth_headers, "tools/list")
    names = {tool["name"] for tool in result["tools"]}
    assert names == EXPECTED_TOOLS
    submit_tool = next(tool for tool in result["tools"] if tool["name"] == "submit_memory_text")
    schema = submit_tool["inputSchema"]
    assert schema["required"] == ["text"]
    assert schema["properties"]["text"]["type"] == "string"
    assert schema["properties"]["conversation_id"]["type"] == "string"
    assert "anyOf" not in schema["properties"]["conversation_id"]
    update_tool = next(
        tool for tool in result["tools"] if tool["name"] == "update_recent_context_summary"
    )
    update_schema = update_tool["inputSchema"]
    assert update_schema["properties"]["summary"]["type"] == "string"
    assert update_schema["properties"]["conversation_id"]["type"] == "string"
    assert "anyOf" not in update_schema["properties"]["conversation_id"]

    for name in (
        "list_knowledge_documents",
        "search_knowledge",
        "read_knowledge",
        "begin_knowledge_upload",
        "append_knowledge_upload",
        "commit_knowledge_upload",
        "manage_knowledge_document",
    ):
        tool = next(tool for tool in result["tools"] if tool["name"] == name)
        for property_schema in tool["inputSchema"].get("properties", {}).values():
            assert "anyOf" not in property_schema

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
                    "type": "emotional",
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
                    "type": "semantic",
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

def test_submit_memory_text_accepts_text_only_unicode_payload(client, auth_headers, fake_llm, monkeypatch):
    import app.mcp_server.server as server_module

    monkeypatch.setattr(server_module, "get_llm_client", lambda settings: fake_llm)
    fake_llm.extraction_content = json.dumps(
        {
            "action": "ignore",
            "memory": "",
            "type": "semantic",
            "importance": 1,
            "confidence": 0.0,
            "reason": "测试只验证 MCP 参数兼容",
            "source_quote": "",
        },
        ensure_ascii=False,
    )

    outcome = _call_tool(
        client,
        auth_headers,
        "submit_memory_text",
        {
            "text": (
                "我做的很复杂，不只是 RAG 包括嵌入检索，还有完整的记忆衰减机制，"
                "一段时间后记忆时效性复核机制，还有 arousal 调 λ 的情绪坐标，动态抽取，"
                "海马索引理论等等😆"
            )
        },
    )

    assert outcome["ignored"] >= 0

def test_search_memory_finds_saved(client, auth_headers, memory_store):
    memory_store.create_memory(
        user_id="default",
        content="用户使用 Kelivo 作为 AI 客户端",
        type="semantic",
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
    assert matched["activation_count"] == 1
    assert matched["last_active_at"] is not None
    assert matched["relevance"] > 0
    assert matched["channels"] == ["keyword"]
    assert matched["final_score"] > 0
    # 不应泄露向量字段
    assert all("embedding_json" not in memory for memory in found)


def test_rest_search_uses_time_ripple_config(
    client,
    auth_headers,
    memory_store,
    monkeypatch,
):
    monkeypatch.setenv("TIME_RIPPLE_DELTA", "0.4")
    monkeypatch.setenv("TIME_RIPPLE_WINDOW_HOURS", "48")
    get_settings.cache_clear()
    seed = memory_store.create_memory(
        user_id="default",
        content="用户使用 Kelivo 做 AI 客户端。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["kelivo"],
    )
    neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在整理客户端记忆体验。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["kelivo"],
    )

    response = client.post(
        "/memories/search",
        headers=auth_headers,
        json={"query": "Kelivo", "limit": 5},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [seed.id]
    refreshed_neighbor = memory_store.get_memory(memory_id=neighbor.id, user_id="default")
    assert refreshed_neighbor is not None
    assert refreshed_neighbor.usage_count == 0.4


def test_mcp_search_uses_time_ripple_config(
    client,
    auth_headers,
    memory_store,
    monkeypatch,
):
    monkeypatch.setenv("TIME_RIPPLE_DELTA", "0.4")
    monkeypatch.setenv("TIME_RIPPLE_WINDOW_HOURS", "48")
    get_settings.cache_clear()
    seed = memory_store.create_memory(
        user_id="default",
        content="用户使用 Kelivo 做 AI 客户端。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["kelivo"],
    )
    neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在整理客户端记忆体验。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["kelivo"],
    )

    found = _call_tool(
        client,
        auth_headers,
        "search_memory",
        {"query": "Kelivo", "limit": 5},
    )

    assert [item["id"] for item in found] == [seed.id]
    refreshed_neighbor = memory_store.get_memory(memory_id=neighbor.id, user_id="default")
    assert refreshed_neighbor is not None
    assert refreshed_neighbor.usage_count == 0.4


def test_surface_memories_tool(client, auth_headers, memory_store):
    cold = memory_store.create_memory(
        user_id="default",
        content="用户正在开发 My_Memory。",
        type="semantic",
        importance=9,
    )

    surfaced = _call_tool(client, auth_headers, "surface_memories", {"limit": 5})

    assert surfaced[0]["id"] == cold.id
    assert surfaced[0]["surface_reason"] == "fresh_high_importance"
    assert surfaced[0]["activation_count"] == 0
    assert surfaced[0]["last_active_at"] is not None
    assert surfaced[0]["freshness_bonus"] >= 1.0
    assert surfaced[0]["final_score"] > 0
    assert surfaced[0]["surface_score"] == surfaced[0]["final_score"]
    assert surfaced[0]["surface_mode"] == "balanced"
    assert surfaced[0]["surface_reason_text"]
    assert surfaced[0]["life_score"] > 0
    assert surfaced[0]["days_since_last_active"] >= 0
    assert surfaced[0]["review_signals"] == []

    emotional = _call_tool(client, auth_headers, "surface_memories", {"limit": 5, "mode": "emotional"})
    assert emotional[0]["surface_mode"] == "emotional"

def test_surface_memories_rest_endpoint(client, auth_headers, memory_store):
    memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=8,
    )

    response = client.post("/memories/surface", headers=auth_headers, json={"limit": 3})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["data"]) == 1
    surfaced = payload["data"][0]
    assert surfaced["activation_count"] == 0
    assert surfaced["last_active_at"] is not None
    assert surfaced["surface_reason"] == "fresh_high_importance"
    assert surfaced["surface_mode"] == "balanced"
    assert surfaced["surface_reason_text"]

def test_surface_memories_accepts_mode(client, auth_headers, memory_store):
    memory_store.create_memory(
        user_id="default",
        content="用户对一次发布复盘仍然很紧张。",
        type="semantic",
        importance=4,
        confidence=0.9,
        arousal=0.95,
    )

    response = client.post(
        "/memories/surface",
        headers=auth_headers,
        json={"limit": 3, "mode": "emotional"},
    )

    assert response.status_code == 200
    surfaced = response.json()["data"][0]
    assert surfaced["surface_mode"] == "emotional"
    assert surfaced["surface_reason"] == "emotional_signal"
    assert surfaced["surface_score"] > surfaced["final_score"]


def test_digest_memories_two_phase_tool(client, auth_headers, memory_store):
    source = memory_store.create_memory(
        user_id="default",
        content="用户最近完成了记忆系统 P0 收口。",
        type="semantic",
        importance=7,
    )
    resolved = memory_store.create_memory(
        user_id="default",
        content="用户此前还在等待 P0 收口。",
        type="semantic",
        importance=6,
    )

    draft = _call_tool(client, auth_headers, "digest_memories", {"limit": 5})
    assert {memory["id"] for memory in draft["memories"]} >= {source.id, resolved.id}
    assert memory_store.get_memory(memory_id=source.id, user_id="default").digested is False

    submitted = _call_tool(
        client,
        auth_headers,
        "digest_memories",
        {
            "source_ids": [source.id, resolved.id],
            "reflection": "用户倾向于先把复杂需求收束成可验收的 P0。",
            "feel": "我对这次收口形成了更稳定的协作节奏感。",
            "resolved_ids": [resolved.id],
        },
    )

    assert submitted["digested_ids"] == [source.id, resolved.id]
    assert submitted["resolved_ids"] == [resolved.id]
    assert len(submitted["created"]) == 2
    assert {item["type"] for item in submitted["created"]} == {"reflective", "emotional"}
    assert all(item["origin"] == "agent_derived" for item in submitted["created"])
    assert all(item["sensitivity"] == "normal" for item in submitted["created"])
    assert all(
        item["evidence_memory_ids"] == [source.id, resolved.id]
        for item in submitted["created"]
    )
    feel = next(item for item in submitted["created"] if item["content"] == "我对这次收口形成了更稳定的协作节奏感。")
    assert feel["type"] == "emotional"
    assert feel["valence"] != 0.5
    assert feel["arousal"] != 0.4
    assert memory_store.get_memory(memory_id=source.id, user_id="default").digested is True
    assert memory_store.get_memory(memory_id=resolved.id, user_id="default").status == "resolved"


def test_digest_memories_phase_one_excludes_sensitive_and_derived_by_default(
    client,
    auth_headers,
    memory_store,
):
    eligible = memory_store.create_memory(
        user_id="default",
        content="用户正在验证默认消化候选。",
    )
    private = memory_store.create_memory(
        user_id="default",
        content="用户的私密消化候选。",
        sensitivity="private",
    )
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户的敏感消化候选。",
        sensitivity="sensitive",
    )
    derived = memory_store.create_memory(
        user_id="default",
        content="模型基于来源形成的派生结论。",
        type="reflective",
        origin="agent_derived",
        evidence_memory_ids=[eligible.id],
    )
    legacy_mislabeled = memory_store.create_memory(
        user_id="default",
        content="用户正在验证旧标签消化候选。",
    )
    with memory_store._connect() as connection:
        connection.execute(
            "UPDATE memories SET content = ?, sensitivity = 'normal' WHERE id = ?",
            (
                "用户的身份证号是 123456789012345678。",
                legacy_mislabeled.id,
            ),
        )

    draft = _call_tool(client, auth_headers, "digest_memories", {"limit": 10})

    returned_ids = {memory["id"] for memory in draft["memories"]}
    assert returned_ids == {eligible.id}
    assert returned_ids.isdisjoint(
        {private.id, sensitive.id, derived.id, legacy_mislabeled.id}
    )

    submission = _call_tool(
        client,
        auth_headers,
        "digest_memories",
        {
            "source_ids": [legacy_mislabeled.id],
            "reflection": "不应创建的派生结论。",
        },
    )
    assert submission["created"] == []
    assert submission["digested_ids"] == []
    assert submission["error"]


def test_digest_memories_rejects_sensitive_opt_in_when_egress_is_disabled(
    client,
    auth_headers,
    memory_store,
    monkeypatch,
):
    monkeypatch.setenv("ALLOW_SENSITIVE_EGRESS", "false")
    get_settings.cache_clear()
    private = memory_store.create_memory(
        user_id="default",
        content="用户的私密消化候选。",
        sensitivity="private",
    )

    result = _call_tool(
        client,
        auth_headers,
        "digest_memories",
        {"limit": 5, "include_sensitive": True},
    )

    assert result == {
        "error": "Sensitive digestion requires ALLOW_SENSITIVE_EGRESS=true.",
        "created": [],
        "digested_ids": [],
        "resolved_ids": [],
    }
    stored = memory_store.get_memory(memory_id=private.id, user_id="default")
    assert stored is not None
    assert stored.digested is False


def test_digest_memories_rejects_invalid_source_sets(
    client,
    auth_headers,
    memory_store,
):
    source = memory_store.create_memory(
        user_id="default",
        content="用户正在验证消化证据边界。",
    )
    other_user = memory_store.create_memory(
        user_id="other",
        content="其他用户的私有证据。",
    )

    invalid_submissions = [
        {
            "source_ids": [],
            "reflection": "没有来源的派生结论。",
        },
        {
            "source_ids": [source.id, "missing-memory"],
            "reflection": "包含不存在来源的派生结论。",
        },
        {
            "source_ids": [source.id, other_user.id],
            "reflection": "包含跨用户来源的派生结论。",
        },
        {
            "source_ids": [source.id],
            "reflection": "resolved 越过证据集合。",
            "resolved_ids": [other_user.id],
        },
    ]

    for arguments in invalid_submissions:
        result = _call_tool(
            client,
            auth_headers,
            "digest_memories",
            arguments,
        )
        assert result["error"]
        assert result["created"] == []
        assert result["digested_ids"] == []
        assert result["resolved_ids"] == []

    stored_source = memory_store.get_memory(memory_id=source.id, user_id="default")
    assert stored_source is not None
    assert stored_source.digested is False
    assert stored_source.status == "dynamic"
    assert all(
        memory.origin != "agent_derived"
        for memory in memory_store.list_memories(user_id="default")
    )


def test_review_memories_rest_endpoint(client, auth_headers, memory_store):
    expired = memory_store.create_memory(
        user_id="default",
        content="用户最近在减少咖啡摄入。",
        type="semantic",
        importance=8,
        stability="temporary",
        valid_until="2020-01-01",
    )
    low_value = memory_store.create_memory(
        user_id="default",
        content="用户那天晚上吃了火锅。",
        type="semantic",
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
    source = memory_store.create_memory(
        user_id="default",
        content="用户喜欢直接、实用的回答。",
        importance=8,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="communication",
        content="- 用户喜欢直接、实用的回答。",
        evidence_memory_ids=[source.id],
        confidence=0.9,
    )

    core = _call_tool(client, auth_headers, "get_core_memory", {})

    assert core[0]["section"] == "communication"
    assert "直接、实用" in core[0]["content"]


def test_get_core_memory_excludes_legacy_sensitive_section(
    client,
    auth_headers,
    memory_store,
):
    source = memory_store.create_memory(
        user_id="default",
        content="用户长期喜欢黑咖啡。",
        importance=8,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="- 用户长期喜欢黑咖啡。",
        evidence_memory_ids=[source.id],
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

    core = _call_tool(client, auth_headers, "get_core_memory", {})

    assert [section["section"] for section in core] == ["preferences"]
    assert secret not in json.dumps(core, ensure_ascii=False)


def test_get_core_memory_excludes_section_with_sensitive_evidence(
    client,
    auth_headers,
    memory_store,
):
    source = memory_store.create_memory(
        user_id="default",
        content="用户确诊糖尿病。",
        importance=10,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="- 用户需要控制血糖。",
        evidence_memory_ids=[source.id],
        confidence=0.95,
    )

    core = _call_tool(client, auth_headers, "get_core_memory", {})

    assert core == []

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


def test_update_recent_context_summary_tool(client, auth_headers):
    updated = _call_tool(
        client,
        auth_headers,
        "update_recent_context_summary",
        {
            "conversation_id": "conv-mcp",
            "summary": "用户：继续聊周末早餐\n助手：整理了菜单",
        },
    )

    assert updated["updated"] is True
    assert updated["conversation_id"] == "conv-mcp"
    assert updated["summary"] == "用户：继续聊周末早餐\n助手：整理了菜单"

    by_conversation = _call_tool(
        client,
        auth_headers,
        "get_recent_context_summary",
        {"conversation_id": "conv-mcp"},
    )
    latest = _call_tool(client, auth_headers, "get_recent_context_summary", {})

    assert by_conversation["found"] is True
    assert by_conversation["id"] == updated["id"]
    assert latest["found"] is True
    assert latest["id"] == updated["id"]


def test_user_isolation_via_header(client, auth_headers):
    alice = _user_headers(auth_headers, "alice")
    bob = _user_headers(auth_headers, "bob")

    client.post("/memories", headers=alice, json={
        "content": "用户使用 iPhone，并用 Kelivo 作为 AI 客户端",
        "type": "semantic",
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
        type="semantic",
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
        type="semantic",
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
