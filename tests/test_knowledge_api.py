import hashlib


def _headers(auth_headers: dict[str, str], user_id: str = "default") -> dict[str, str]:
    return {**auth_headers, "X-User-Id": user_id}


def _upload(
    client,
    auth_headers: dict[str, str],
    text: str,
    *,
    title: str = "架构说明",
    user_id: str = "default",
    replace_document_ref: str = "",
    content_type: str = "text/markdown",
) -> dict:
    headers = _headers(auth_headers, user_id)
    begun = client.post(
        "/knowledge/uploads",
        headers=headers,
        json={
            "title": title,
            "content_type": content_type,
            "source_name": "architecture.md",
            "replace_document_ref": replace_document_ref,
            "sensitivity": "normal",
        },
    )
    assert begun.status_code == 200, begun.text
    upload_id = begun.json()["upload_id"]
    midpoint = max(1, len(text) // 2)
    parts = [text[:midpoint], text[midpoint:]] if midpoint < len(text) else [text]
    for sequence, part in enumerate(parts):
        appended = client.put(
            f"/knowledge/uploads/{upload_id}/parts/{sequence}",
            headers=headers,
            json={"text": part},
        )
        assert appended.status_code == 200, appended.text
    committed = client.post(
        f"/knowledge/uploads/{upload_id}/commit",
        headers=headers,
        json={
            "expected_parts": len(parts),
            "expected_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
    )
    assert committed.status_code == 200, committed.text
    return committed.json()


def test_knowledge_rest_requires_valid_bearer_token(client) -> None:
    bad_headers = {"Authorization": "Bearer wrong-key"}
    probes = [
        ("GET", "/knowledge/documents", None),
        (
            "POST",
            "/knowledge/search",
            {"request": "未授权检索", "limit": 5},
        ),
        (
            "POST",
            "/knowledge/uploads",
            {"title": "未授权上传", "content_type": "text/markdown"},
        ),
        (
            "POST",
            "/knowledge/read",
            {"reference": "knowledge://version/anything"},
        ),
        ("POST", "/knowledge/restore", {"data": {"documents": []}}),
    ]

    for method, path, payload in probes:
        for headers in ({}, bad_headers):
            response = client.request(method, path, headers=headers, json=payload)
            assert response.status_code == 401, (method, path, headers, response.text)


def test_knowledge_rest_upload_search_and_lossless_read(
    client,
    auth_headers,
    memory_store,
) -> None:
    before_memories = memory_store.list_memories(user_id="alice")
    text = (
        "# 安全边界\n\n"
        "知识库不会进入长期记忆，也不会参与记忆衰减。\n\n"
        "## 出站策略\n\n"
        "默认禁止把候选原文发送到远程搜索代理。\n"
    ) * 120
    created = _upload(client, auth_headers, text, user_id="alice")

    assert created["document"]["document_ref"].startswith("knowledge://document/")
    assert created["version"]["version_ref"].startswith("knowledge://version/")
    assert created["version"]["index_status"] == "ready"

    listed = client.get(
        "/knowledge/documents?status=active",
        headers=_headers(auth_headers, "alice"),
    )
    assert listed.status_code == 200
    assert [item["title"] for item in listed.json()["data"]] == ["架构说明"]

    searched = client.post(
        "/knowledge/search",
        headers=_headers(auth_headers, "alice"),
        json={
            "request": "远程搜索代理的默认出站策略",
            "limit": 5,
            "document_refs": [],
            "quality": "balanced",
            "include_sensitive": False,
        },
    )
    assert searched.status_code == 200, searched.text
    payload = searched.json()
    assert payload["agent_used"] is False
    assert payload["fallback_reason"] in {"egress_disabled", "agent_not_configured"}
    assert payload["data"]
    assert payload["local_candidates"]
    assert all("excerpt" not in item for item in payload["local_candidates"])
    assert all(len(item["excerpt"]) <= 800 for item in payload["data"])
    assert sum(len(item["excerpt"]) for item in payload["data"]) <= 8000
    hit = payload["data"][0]
    assert hit["excerpt"] in text
    assert hit["document_ref"] == created["document"]["document_ref"]
    assert hit["version_ref"] == created["version"]["version_ref"]
    assert hit["chunk_ref"].startswith("knowledge://chunk/")
    assert text[hit["char_start"] : hit["char_end"]].startswith(hit["excerpt"])

    rebuilt = ""
    cursor = ""
    while True:
        read = client.post(
            "/knowledge/read",
            headers=_headers(auth_headers, "alice"),
            json={
                "reference": created["version"]["version_ref"],
                "cursor": cursor,
                "max_chars": 777,
                "include_sensitive": False,
            },
        )
        assert read.status_code == 200, read.text
        page = read.json()
        rebuilt += page.get("content", page.get("text", ""))
        if page["complete"]:
            assert page.get("next_cursor", "") == ""
            break
        cursor = page["next_cursor"]
        assert cursor
    assert rebuilt == text
    assert memory_store.list_memories(user_id="alice") == before_memories


def test_knowledge_rest_versions_deduplicate_and_restore_history(client, auth_headers) -> None:
    first_text = "# v1\n\n第一版逐字正文。"
    first = _upload(client, auth_headers, first_text, user_id="alice")
    document_ref = first["document"]["document_ref"]

    duplicate = _upload(
        client,
        auth_headers,
        first_text,
        title="标题变化不应产生正文版本",
        user_id="alice",
        replace_document_ref=document_ref,
    )
    assert duplicate["deduplicated"] is True
    assert duplicate["version"]["version_number"] == 1

    second = _upload(
        client,
        auth_headers,
        "# v2\n\n第二版逐字正文。",
        user_id="alice",
        replace_document_ref=document_ref,
    )
    assert second["version"]["version_number"] == 2

    restored = client.post(
        f"/knowledge/documents/{first['document']['id']}/versions/{first['version']['id']}/restore",
        headers=_headers(auth_headers, "alice"),
    )
    assert restored.status_code == 200, restored.text
    restored_payload = restored.json()
    assert restored_payload["version"]["version_number"] == 3
    assert restored_payload["version"]["sha256"] == first["version"]["sha256"]
    assert restored_payload["version"]["id"] != first["version"]["id"]


def test_knowledge_rest_isolation_delete_restore_and_confirmed_purge(
    client,
    auth_headers,
) -> None:
    alice = _upload(client, auth_headers, "Alice 的私有项目文本", user_id="alice")
    document = alice["document"]

    bob_list = client.get(
        "/knowledge/documents?status=all",
        headers=_headers(auth_headers, "bob"),
    )
    assert bob_list.status_code == 200
    assert bob_list.json()["data"] == []
    hidden = client.get(
        f"/knowledge/documents/{document['id']}",
        headers=_headers(auth_headers, "bob"),
    )
    assert hidden.status_code == 404

    deleted = client.delete(
        f"/knowledge/documents/{document['id']}",
        headers=_headers(auth_headers, "alice"),
    )
    assert deleted.status_code == 204
    unreadable = client.post(
        "/knowledge/read",
        headers=_headers(auth_headers, "alice"),
        json={"reference": alice["version"]["version_ref"]},
    )
    assert unreadable.status_code == 404

    restored = client.post(
        f"/knowledge/documents/{document['id']}/restore",
        headers=_headers(auth_headers, "alice"),
    )
    assert restored.status_code == 200
    deleted_again = client.delete(
        f"/knowledge/documents/{document['id']}",
        headers=_headers(auth_headers, "alice"),
    )
    assert deleted_again.status_code == 204

    wrong = client.request(
        "DELETE",
        f"/knowledge/deleted/{document['id']}/purge",
        headers=_headers(auth_headers, "alice"),
        json={"confirm_document_id": "wrong"},
    )
    assert wrong.status_code == 400
    purged = client.request(
        "DELETE",
        f"/knowledge/deleted/{document['id']}/purge",
        headers=_headers(auth_headers, "alice"),
        json={"confirm_document_id": document["id"]},
    )
    assert purged.status_code == 204
    missing = client.get(
        f"/knowledge/documents/{document['id']}",
        headers=_headers(auth_headers, "alice"),
    )
    assert missing.status_code == 404


def test_knowledge_export_restore_is_separate_and_rebinds_user(client, auth_headers) -> None:
    first = _upload(client, auth_headers, "第一版", user_id="alice", title="迁移文档")
    _upload(
        client,
        auth_headers,
        "第二版",
        user_id="alice",
        title="迁移文档",
        replace_document_ref=first["document"]["document_ref"],
    )

    exported = client.get("/knowledge/export", headers=_headers(auth_headers, "alice"))
    assert exported.status_code == 200, exported.text
    data = exported.json()
    assert len(data["documents"]) == 1
    assert len(data["documents"][0]["versions"]) == 2
    assert all("chunks" not in version for version in data["documents"][0]["versions"])

    restored = client.post(
        "/knowledge/restore",
        headers=_headers(auth_headers, "bob"),
        json={"data": data},
    )
    assert restored.status_code == 200, restored.text
    bob = client.get(
        "/knowledge/documents?status=active",
        headers=_headers(auth_headers, "bob"),
    ).json()["data"]
    assert len(bob) == 1
    assert bob[0]["user_id"] == "bob"
    assert bob[0]["document_ref"] != first["document"]["document_ref"]


def test_knowledge_status_reports_agent_egress_and_timeout(client, auth_headers) -> None:
    status = client.get("/knowledge/status", headers=auth_headers)
    assert status.status_code == 200, status.text
    payload = status.json()
    assert payload["available"] is True
    assert payload["agent_enabled"] is False
    assert payload["agent_egress_policy"] == "none"
    assert payload["agent_timeout_seconds"] == 25.0
