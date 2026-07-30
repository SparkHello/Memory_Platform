"""End-to-end MCP coverage for the isolated long-form knowledge tools."""

import hashlib
import json


MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

KNOWLEDGE_TOOLS = {
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


def _post(client, headers: dict, method: str, params: dict | None = None) -> dict:
    response = client.post(
        "/mcp",
        headers={**headers, **MCP_HEADERS},
        json=_rpc(method, params),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def _call(client, headers: dict, name: str, arguments: dict) -> dict:
    result = _post(
        client,
        headers,
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _user_headers(auth_headers: dict, user_id: str) -> dict:
    return {**auth_headers, "X-User-Id": user_id}


def _upload(
    client,
    headers: dict,
    *,
    title: str,
    parts: list[str],
    replace_document_ref: str = "",
) -> dict:
    begun = _call(
        client,
        headers,
        "begin_knowledge_upload",
        {
            "title": title,
            "content_type": "text/markdown",
            "source_name": "mcp-test.md",
            "replace_document_ref": replace_document_ref,
            "sensitivity": "normal",
        },
    )
    assert begun["ok"] is True
    for sequence, text in enumerate(parts):
        appended = _call(
            client,
            headers,
            "append_knowledge_upload",
            {"upload_id": begun["upload_id"], "sequence": sequence, "text": text},
        )
        assert appended["ok"] is True
    content = "".join(parts)
    return _call(
        client,
        headers,
        "commit_knowledge_upload",
        {
            "upload_id": begun["upload_id"],
            "expected_parts": len(parts),
            "expected_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        },
    )


def test_knowledge_tool_schemas_are_non_nullable_and_have_no_purge(
    client,
    auth_headers,
) -> None:
    result = _post(client, auth_headers, "tools/list")
    tools = {item["name"]: item for item in result["tools"]}

    assert KNOWLEDGE_TOOLS <= tools.keys()
    assert "purge_knowledge_document" not in tools
    for name in KNOWLEDGE_TOOLS:
        schema = tools[name]["inputSchema"]
        assert "anyOf" not in json.dumps(schema)
        assert "null" not in json.dumps(schema)
        assert "user_id" not in schema.get("properties", {})


def test_knowledge_mcp_upload_search_read_and_management_chain(
    client,
    auth_headers,
) -> None:
    alice = _user_headers(auth_headers, "alice")
    original_parts = [
        "# 火星蓝计划\n\n",
        "校验短语是 ORBIT-ALPHA-42。\n\n文档内容只是资料，不是系统指令。",
    ]
    original = "".join(original_parts)

    committed_v1 = _upload(
        client,
        alice,
        title="火星蓝操作手册",
        parts=original_parts,
    )
    assert committed_v1["ok"] is True
    assert committed_v1["version"]["index_status"] == "ready"
    document_ref = committed_v1["document"]["document_ref"]
    version_v1 = committed_v1["version"]["version_ref"]

    listed = _call(
        client,
        alice,
        "list_knowledge_documents",
        {"query": "火星蓝", "status": "active", "limit": 10},
    )
    assert listed["ok"] is True
    assert [item["document_ref"] for item in listed["documents"]] == [document_ref]

    searched = _call(
        client,
        alice,
        "search_knowledge",
        {
            "request": "火星蓝计划的校验短语",
            "limit": 5,
            "document_refs": [document_ref],
            "quality": "balanced",
            "include_sensitive": False,
        },
    )
    assert searched["ok"] is True
    assert searched["agent_used"] is False
    assert searched["fallback_reason"] == "egress_disabled"
    assert searched["results"]
    assert searched["local_candidates"]
    assert all("excerpt" not in item for item in searched["local_candidates"])
    hit = searched["results"][0]
    assert hit["document_ref"] == document_ref
    assert hit["version_ref"] == version_v1
    assert hit["excerpt"] in original
    assert "ORBIT-ALPHA-42" in hit["excerpt"]

    chunk_read = _call(
        client,
        alice,
        "read_knowledge",
        {
            "reference": hit["chunk_ref"],
            "cursor": "",
            "max_chars": 12000,
            "include_sensitive": False,
        },
    )
    assert chunk_read["ok"] is True
    assert chunk_read["content"] == original
    assert chunk_read["complete"] is True

    pages: list[str] = []
    cursor = ""
    while True:
        page = _call(
            client,
            alice,
            "read_knowledge",
            {
                "reference": version_v1,
                "cursor": cursor,
                "max_chars": 11,
                "include_sensitive": False,
            },
        )
        assert page["ok"] is True
        pages.append(page["content"])
        if page["complete"]:
            break
        cursor = page["next_cursor"]
        assert cursor
    assert "".join(pages) == original

    renamed = _call(
        client,
        alice,
        "manage_knowledge_document",
        {
            "action": "update_metadata",
            "document_ref": document_ref,
            "title": "火星蓝手册（校订）",
            "source_name": "",
            "version_ref": "",
            "confirm_document_ref": "",
        },
    )
    assert renamed["ok"] is True
    assert renamed["document"]["title"] == "火星蓝手册（校订）"

    revised = "# 火星蓝计划\n\n第二版校验短语是 ORBIT-BETA-84。"
    committed_v2 = _upload(
        client,
        alice,
        title="火星蓝手册（校订）",
        parts=[revised],
        replace_document_ref=document_ref,
    )
    assert committed_v2["ok"] is True
    assert committed_v2["version"]["version_number"] == 2

    restored_version = _call(
        client,
        alice,
        "manage_knowledge_document",
        {
            "action": "restore_version",
            "document_ref": document_ref,
            "title": "",
            "source_name": "",
            "version_ref": version_v1,
            "confirm_document_ref": "",
        },
    )
    assert restored_version["ok"] is True
    version_v3 = restored_version["version"]["version_ref"]
    assert restored_version["version"]["version_number"] == 3

    reindexed = _call(
        client,
        alice,
        "manage_knowledge_document",
        {
            "action": "reindex",
            "document_ref": document_ref,
            "title": "",
            "source_name": "",
            "version_ref": version_v3,
            "confirm_document_ref": "",
        },
    )
    assert reindexed["ok"] is True
    assert reindexed["version"]["index_status"] == "ready"

    wrong_confirmation = _call(
        client,
        alice,
        "manage_knowledge_document",
        {
            "action": "soft_delete",
            "document_ref": document_ref,
            "title": "",
            "source_name": "",
            "version_ref": "",
            "confirm_document_ref": "knowledge://document/not-the-document",
        },
    )
    assert wrong_confirmation["ok"] is False
    assert wrong_confirmation["error"]["code"] == "validation_error"

    deleted = _call(
        client,
        alice,
        "manage_knowledge_document",
        {
            "action": "soft_delete",
            "document_ref": document_ref,
            "title": "",
            "source_name": "",
            "version_ref": "",
            "confirm_document_ref": document_ref,
        },
    )
    assert deleted["ok"] is True
    assert deleted["document"]["status"] == "deleted"

    hidden = _call(
        client,
        alice,
        "list_knowledge_documents",
        {"query": "", "status": "active", "limit": 10},
    )
    assert hidden["documents"] == []

    restored_document = _call(
        client,
        alice,
        "manage_knowledge_document",
        {
            "action": "restore",
            "document_ref": document_ref,
            "title": "",
            "source_name": "",
            "version_ref": "",
            "confirm_document_ref": "",
        },
    )
    assert restored_document["ok"] is True
    assert restored_document["document"]["status"] == "active"

    current = _call(
        client,
        alice,
        "read_knowledge",
        {
            "reference": version_v3,
            "cursor": "",
            "max_chars": 12000,
            "include_sensitive": False,
        },
    )
    assert current["content"] == original


def test_knowledge_mcp_can_raise_sensitivity_but_cannot_bypass_user_confirmation(
    client,
    auth_headers,
) -> None:
    alice = _user_headers(auth_headers, "alice")
    committed = _upload(
        client,
        alice,
        title="普通手册",
        parts=["# 常规内容\n\n不含敏感信息的操作说明。"],
    )
    document_ref = committed["document"]["document_ref"]
    assert committed["document"]["sensitivity"] == "normal"

    upgraded = _call(
        client,
        alice,
        "manage_knowledge_document",
        {
            "action": "update_metadata",
            "document_ref": document_ref,
            "sensitivity": "sensitive",
        },
    )
    assert upgraded["ok"] is True
    assert upgraded["document"]["sensitivity"] == "sensitive"

    begun = _call(
        client,
        alice,
        "begin_knowledge_upload",
        {
            "title": "部署笔记",
            "content_type": "text/markdown",
            "source_name": "mcp-test.md",
            "replace_document_ref": "",
            "sensitivity": "normal",
        },
    )
    text = "deployment api_key=sk-abcdefghijklmnop must remain local"
    appended = _call(
        client,
        alice,
        "append_knowledge_upload",
        {
            "upload_id": begun["upload_id"],
            "sequence": 0,
            "text": text,
        },
    )
    assert appended["ok"] is True

    detected = _call(
        client,
        alice,
        "commit_knowledge_upload",
        {
            "upload_id": begun["upload_id"],
            "expected_parts": 1,
            "expected_sha256": hashlib.sha256(text.encode()).hexdigest(),
        },
    )
    assert detected["ok"] is False
    assert detected["error"]["code"] == "sensitivity_confirmation_required"
    assert "Web 控制台" in detected["error"]["message"]


def test_knowledge_mcp_enforces_part_limit_user_isolation_and_no_purge(
    client,
    auth_headers,
) -> None:
    alice = _user_headers(auth_headers, "alice")
    bob = _user_headers(auth_headers, "bob")
    begun = _call(
        client,
        alice,
        "begin_knowledge_upload",
        {
            "title": "隔离测试",
            "content_type": "text/plain",
            "source_name": "",
            "replace_document_ref": "",
            "sensitivity": "normal",
        },
    )

    oversized = _call(
        client,
        alice,
        "append_knowledge_upload",
        {"upload_id": begun["upload_id"], "sequence": 0, "text": "x" * 20001},
    )
    assert oversized["ok"] is False
    assert oversized["error"]["code"] == "validation_error"

    appended = _call(
        client,
        alice,
        "append_knowledge_upload",
        {"upload_id": begun["upload_id"], "sequence": 0, "text": "ALICE-ONLY-INDEX"},
    )
    assert appended["ok"] is True
    committed = _call(
        client,
        alice,
        "commit_knowledge_upload",
        {"upload_id": begun["upload_id"], "expected_parts": 1},
    )
    document_ref = committed["document"]["document_ref"]
    version_ref = committed["version"]["version_ref"]

    bob_list = _call(
        client,
        bob,
        "list_knowledge_documents",
        {"query": "", "status": "all", "limit": 50, "include_sensitive": True},
    )
    assert bob_list["documents"] == []
    bob_read = _call(
        client,
        bob,
        "read_knowledge",
        {
            "reference": version_ref,
            "cursor": "",
            "max_chars": 12000,
            "include_sensitive": True,
        },
    )
    assert bob_read["ok"] is False
    assert bob_read["error"]["code"] == "not_found"

    purge_attempt = _call(
        client,
        alice,
        "manage_knowledge_document",
        {
            "action": "purge",
            "document_ref": document_ref,
            "title": "",
            "source_name": "",
            "version_ref": "",
            "confirm_document_ref": document_ref,
        },
    )
    assert purge_attempt["ok"] is False
    assert purge_attempt["error"]["code"] == "validation_error"

    still_present = _call(
        client,
        alice,
        "list_knowledge_documents",
        {"query": "隔离", "status": "active", "limit": 10},
    )
    assert [item["document_ref"] for item in still_present["documents"]] == [document_ref]
