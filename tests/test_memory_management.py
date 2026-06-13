from app.memory.store import MemoryStore


def test_report_and_export_rest_endpoints(client, auth_headers, memory_store: MemoryStore):
    memory_store.create_memory(
        user_id="default",
        content="User likes black coffee.",
        type="preference",
        importance=7,
        confidence=0.9,
        embedding_json="[0.1, 0.2]",
    )
    deleted = memory_store.create_memory(
        user_id="default",
        content="User used to prefer tea.",
        type="preference",
        importance=5,
    )
    memory_store.archive_memory(memory_id=deleted.id, user_id="default")

    report_response = client.get("/memories/report", headers=auth_headers)
    assert report_response.status_code == 200
    report = report_response.json()
    assert report["counts"]["active_memories"] == 1
    assert report["counts"]["deleted_memories"] == 1
    assert "Memory Report" in report["markdown"]
    assert any(
        section["section"] == "preferences" and section["memories"]
        for section in report["sections"]
    )

    markdown_response = client.get(
        "/memories/report?format=markdown",
        headers=auth_headers,
    )
    assert markdown_response.status_code == 200
    assert "text/markdown" in markdown_response.headers["content-type"]
    assert "User likes black coffee." in markdown_response.text

    export_response = client.get("/memories/export", headers=auth_headers)
    assert export_response.status_code == 200
    export = export_response.json()
    assert export["embedding_included"] is False
    assert len(export["memories"]) == 1
    assert len(export["deleted_memories"]) == 1
    assert "embedding_json" not in export["memories"][0]

    export_markdown_response = client.get(
        "/memories/export?format=markdown",
        headers=auth_headers,
    )
    assert export_markdown_response.status_code == 200
    assert "Memory Export" in export_markdown_response.text


def test_deleted_memory_rest_restore(client, auth_headers, memory_store: MemoryStore):
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes espresso.",
        type="preference",
    )
    memory_store.archive_memory(memory_id=memory.id, user_id="default")

    deleted_response = client.get("/memories/deleted", headers=auth_headers)
    assert deleted_response.status_code == 200
    assert [item["id"] for item in deleted_response.json()["data"]] == [memory.id]

    restore_response = client.post(
        f"/memories/{memory.id}/restore",
        headers=auth_headers,
    )
    assert restore_response.status_code == 200
    assert restore_response.json()["restored"] is True
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is not None
    assert memory_store.list_archived_memories(user_id="default") == []

    second_restore = client.post(
        f"/memories/{memory.id}/restore",
        headers=auth_headers,
    )
    assert second_restore.status_code == 404


def test_restore_export_imports_memories_for_current_user(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    active = memory_store.create_memory(
        user_id="default",
        content="User likes pour-over coffee.",
        type="preference",
        embedding_json="[0.3, 0.4]",
    )
    deleted = memory_store.create_memory(
        user_id="default",
        content="User used to live in a test city.",
        type="fact",
    )
    memory_store.archive_memory(memory_id=deleted.id, user_id="default")
    export = client.get("/memories/export", headers=auth_headers).json()

    target_headers = {**auth_headers, "X-User-Id": "restore-target"}
    restore_response = client.post(
        "/memories/restore",
        headers=target_headers,
        json={"data": export, "include_deleted": True},
    )

    assert restore_response.status_code == 200
    payload = restore_response.json()
    assert payload["created"] == 2
    assert payload["invalid"] == 0

    restored_active = memory_store.list_memories(user_id="restore-target")
    restored_deleted = memory_store.list_archived_memories(user_id="restore-target")
    assert [memory.content for memory in restored_active] == [active.content]
    assert [memory.content for memory in restored_deleted] == [deleted.content]
    assert restored_active[0].embedding_json is None
