import io
import json
from pathlib import Path
import zipfile

import app.api.memories as memories_api_module
import pytest
from app.memory.models import RecentContextTurn
from app.memory.report import build_memory_export
from app.memory.store import MemoryStore


def test_report_and_export_rest_endpoints(client, auth_headers, memory_store: MemoryStore):
    memory_store.create_memory(
        user_id="default",
        content="User likes black coffee.",
        type="emotional",
        importance=7,
        confidence=0.9,
        embedding_json="[0.1, 0.2]",
    )
    deleted = memory_store.create_memory(
        user_id="default",
        content="User used to prefer tea.",
        type="emotional",
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


def test_selection_export_is_exact_user_scoped_and_sanitizes_references(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    selected_space = memory_store.upsert_memory_space(
        user_id="default", name="Selected Space"
    )
    unused_space = memory_store.upsert_memory_space(
        user_id="default", name="UNSELECTED-SPACE-CANARY"
    )
    selected_source = memory_store.create_memory(
        user_id="default",
        content="SELECTED-SOURCE",
        space_ids=[selected_space.id],
    )
    unselected = memory_store.create_memory(
        user_id="default",
        content="UNSELECTED-MEMORY-CANARY",
        space_ids=[unused_space.id],
    )
    selected_dependent = memory_store.create_memory(
        user_id="default",
        content="SELECTED-DEPENDENT",
        evidence_memory_ids=[selected_source.id, unselected.id],
        space_ids=[selected_space.id],
    )
    selected_deleted = memory_store.create_memory(
        user_id="default", content="SELECTED-DELETED"
    )
    assert memory_store.archive_memory(
        memory_id=selected_deleted.id, user_id="default"
    )
    with memory_store._connect() as connection:
        connection.execute(
            "UPDATE memories SET supersedes = ? WHERE id = ?",
            (unselected.id, selected_dependent.id),
        )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="CORE-CANARY",
        evidence_memory_ids=[unselected.id],
        confidence=0.9,
    )
    memory_store.upsert_recent_context_summary(
        user_id="default",
        conversation_id="selection-test",
        summary="RECENT-CONTEXT-CANARY",
    )
    memory_store.create_decision_log(
        user_id="default",
        conversation_id=None,
        candidate_json='{"canary":"DECISION-LOG-CANARY"}',
        decision="create",
        reason="selection export isolation test",
    )
    other_user = memory_store.create_memory(
        user_id="other-user", content="OTHER-USER-CANARY"
    )

    response = client.post(
        "/memories/export/selection",
        headers=auth_headers,
        json={
            "memory_ids": [
                selected_dependent.id,
                selected_source.id,
                selected_deleted.id,
            ]
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["id"] for item in payload["memories"]] == [
        selected_dependent.id,
        selected_source.id,
    ]
    assert [item["id"] for item in payload["deleted_memories"]] == [
        selected_deleted.id
    ]
    dependent = payload["memories"][0]
    assert dependent["evidence_memory_ids"] == [selected_source.id]
    assert dependent["supersedes"] is None
    assert payload["selection_contract"] == {
        "requested_count": 3,
        "exported_count": 3,
        "sanitized_reference_count": 2,
    }
    assert [space["id"] for space in payload["memory_spaces"]] == [selected_space.id]
    for partition in (
        "core_memory_sections",
        "core_memory_section_history",
        "recent_context_summaries",
        "conversation_branch_nodes",
        "decision_logs",
    ):
        assert payload[partition] == []
    serialized = response.text
    for canary in (
        unselected.content,
        "UNSELECTED-SPACE-CANARY",
        "CORE-CANARY",
        "RECENT-CONTEXT-CANARY",
        "DECISION-LOG-CANARY",
        other_user.content,
    ):
        assert canary not in serialized

    stale = client.post(
        "/memories/export/selection",
        headers=auth_headers,
        json={"memory_ids": [selected_source.id, other_user.id]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "memory_selection_stale"


def test_selection_export_rejects_duplicate_ids(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(user_id="default", content="duplicate")
    response = client.post(
        "/memories/export/selection",
        headers=auth_headers,
        json={"memory_ids": [memory.id, memory.id]},
    )
    assert response.status_code == 422


def test_memory_export_includes_rows_beyond_legacy_ten_thousand_limit(
    memory_store: MemoryStore,
) -> None:
    template = memory_store.create_memory(
        user_id="default",
        content="bulk export memory 0",
        type="semantic",
    )
    with memory_store._connect() as connection:
        row = connection.execute(
            "SELECT * FROM memories WHERE id = ?",
            (template.id,),
        ).fetchone()
        assert row is not None
        columns = list(row.keys())
        id_index = columns.index("id")
        content_index = columns.index("content")
        values = list(row)
        records: list[tuple] = []
        for index in range(1, 10_001):
            clone = list(values)
            clone[id_index] = f"bulk-export-{index}"
            clone[content_index] = f"bulk export memory {index}"
            records.append(tuple(clone))
        placeholders = ", ".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO memories ({', '.join(columns)}) VALUES ({placeholders})",
            records,
        )

    exported = build_memory_export(store=memory_store, user_id="default")

    assert len(exported["memories"]) == 10_001
    assert any(
        memory["id"] == "bulk-export-10000"
        for memory in exported["memories"]
    )


def test_memory_export_reads_all_partitions_from_one_connection_snapshot(
    memory_store: MemoryStore,
    monkeypatch,
) -> None:
    active = memory_store.create_memory(user_id="default", content="Active snapshot row.")
    deleted = memory_store.create_memory(user_id="default", content="Deleted snapshot row.")
    assert memory_store.archive_memory(memory_id=deleted.id, user_id="default")
    original_connect = memory_store._connect
    connection_calls = 0

    def counted_connect():
        nonlocal connection_calls
        connection_calls += 1
        return original_connect()

    monkeypatch.setattr(memory_store, "_connect", counted_connect)

    exported = build_memory_export(store=memory_store, user_id="default")

    assert connection_calls == 1
    assert [memory["id"] for memory in exported["memories"]] == [active.id]
    assert [memory["id"] for memory in exported["deleted_memories"]] == [deleted.id]


def test_sensitive_redaction_for_rest_views_does_not_change_stored_or_exported_content(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    normal = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee.",
        type="emotional",
        importance=4,
        source_message="I like black coffee.",
    )
    space = memory_store.upsert_memory_space(user_id="default", name="Private IDs")
    private = memory_store.create_memory(
        user_id="default",
        content="User's private email address is private@example.com.",
        type="semantic",
        importance=10,
        sensitivity="private",
        source_message="My email address is private@example.com.",
        space_ids=[space.id],
    )
    deleted_private = memory_store.create_memory(
        user_id="default",
        content="User's deleted sensitive account code is DEL-987.",
        type="semantic",
        importance=8,
        sensitivity="sensitive",
        source_message="My deleted account code is DEL-987.",
    )
    memory_store.archive_memory(memory_id=deleted_private.id, user_id="default")

    list_response = client.get(
        "/memories?redact_sensitive=true",
        headers=auth_headers,
    )
    assert list_response.status_code == 200
    listed = {item["id"]: item for item in list_response.json()["data"]}
    assert listed[normal.id]["content"] == normal.content
    assert listed[private.id]["redacted"] is True
    assert listed[private.id]["redaction_reason"] == "private"
    assert "content" in listed[private.id]["redacted_fields"]
    assert listed[private.id]["content"] != private.content
    assert listed[private.id]["source_message"] != private.source_message

    deleted_response = client.get(
        "/memories/deleted?redact_sensitive=true",
        headers=auth_headers,
    )
    assert deleted_response.status_code == 200
    deleted = {item["id"]: item for item in deleted_response.json()["data"]}
    assert deleted[deleted_private.id]["redacted"] is True
    assert deleted[deleted_private.id]["content"] != deleted_private.content

    search_response = client.post(
        "/memories/search",
        headers=auth_headers,
        json={
            "query": "email address",
            "limit": 5,
            "include_sensitive": True,
            "redact_sensitive": True,
        },
    )
    assert search_response.status_code == 200
    search_hit = next(
        item for item in search_response.json()["data"] if item["id"] == private.id
    )
    assert search_hit["redacted"] is True
    assert search_hit["content"] != private.content
    assert "score_breakdown" in search_hit

    surface_response = client.post(
        "/memories/surface",
        headers=auth_headers,
        json={"limit": 5, "include_sensitive": True, "redact_sensitive": True},
    )
    assert surface_response.status_code == 200
    surfaced = {item["id"]: item for item in surface_response.json()["data"]}
    assert surfaced[private.id]["redacted"] is True
    assert surfaced[private.id]["content"] != private.content
    assert "surface_score" in surfaced[private.id]

    space_response = client.get(
        f"/memories/spaces/{space.id}?redact_sensitive=true",
        headers=auth_headers,
    )
    assert space_response.status_code == 200
    space_memory = space_response.json()["memories"][0]
    assert space_memory["id"] == private.id
    assert space_memory["redacted"] is True
    assert space_memory["content"] != private.content

    redacted_single = client.get(
        f"/memories/{private.id}?redact_sensitive=true",
        headers=auth_headers,
    )
    assert redacted_single.status_code == 200
    assert redacted_single.json()["memory"]["redacted"] is True

    full_single = client.get(f"/memories/{private.id}", headers=auth_headers)
    assert full_single.status_code == 200
    assert full_single.json()["memory"]["content"] == private.content
    assert full_single.json()["memory"]["source_message"] == private.source_message

    why_redacted = client.get(
        f"/memories/{private.id}/why?redact_sensitive=true",
        headers=auth_headers,
    )
    assert why_redacted.status_code == 200
    assert why_redacted.json()["redacted"] is True
    assert why_redacted.json()["content"] != private.content
    assert why_redacted.json()["source_excerpt"] != private.source_message

    why_full = client.get(f"/memories/{private.id}/why", headers=auth_headers)
    assert why_full.status_code == 200
    assert why_full.json()["content"] == private.content
    assert why_full.json()["source_excerpt"] == private.source_message

    stored = memory_store.get_memory(memory_id=private.id, user_id="default")
    assert stored is not None
    assert stored.content == private.content
    assert stored.source_message == private.source_message

    export_response = client.get("/memories/export", headers=auth_headers)
    assert export_response.status_code == 200
    exported = {item["id"]: item for item in export_response.json()["memories"]}
    assert exported[private.id]["content"] == private.content
    assert exported[private.id]["source_message"] == private.source_message


def test_obsidian_markdown_export_returns_zip(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    space = memory_store.upsert_memory_space(user_id="default", name="Coffee")
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee.",
        type="emotional",
        importance=7,
        confidence=0.9,
        embedding_json="[0.1, 0.2]",
        topics=["coffee"],
        entities=["espresso machine"],
        space_ids=[space.id],
        review_after="2026-07-01",
    )
    deleted = memory_store.create_memory(
        user_id="default",
        content="User used to prefer tea.",
        type="emotional",
        importance=5,
    )
    memory_store.archive_memory(memory_id=deleted.id, user_id="default")
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="User prefers black coffee.",
        evidence_memory_ids=[memory.id],
        confidence=0.86,
    )

    response = client.get(
        "/memories/export?format=obsidian_markdown",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert "application/zip" in response.headers["content-type"]
    assert "memory-obsidian-export-default.zip" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        note_name = next(
            name
            for name in names
            if name.startswith("Memories/notes/emotional-") and name.endswith(".md")
        )
        assert {
            "Memories/by-type/emotional.md",
            "Memories/by-space/Coffee.md",
            "Core Memory/preferences.md",
            "Review/review-due.md",
            "Review/deleted-memories.md",
            "Reports/memory-report.md",
            "Reports/export-summary.md",
        }.issubset(names)

        note = archive.read(note_name).decode("utf-8")
        for field in [
            "id:",
            "type:",
            "importance:",
            "confidence:",
            "stability:",
            "sensitivity:",
            "valence:",
            "arousal:",
            "topics:",
            "entities:",
            "space_ids:",
            "spaces:",
            "review_after:",
            "created_at:",
            "updated_at:",
        ]:
            assert field in note
        assert "embedding_json" not in note
        assert "User likes black coffee." in note
        assert "Coffee" in note

        by_type = archive.read("Memories/by-type/emotional.md").decode("utf-8")
        by_space = archive.read("Memories/by-space/Coffee.md").decode("utf-8")
        assert "[[Memories/notes/emotional-" in by_type
        assert "[[Memories/notes/emotional-" in by_space
        assert "User likes black coffee." in archive.read("Review/review-due.md").decode("utf-8")
        assert "[[Memories/notes/emotional-" in archive.read(
            "Core Memory/preferences.md"
        ).decode("utf-8")
        assert "User used to prefer tea." in archive.read(
            "Review/deleted-memories.md"
        ).decode("utf-8")

    no_deleted_response = client.get(
        "/memories/export?format=obsidian_markdown&include_deleted=false",
        headers=auth_headers,
    )
    assert no_deleted_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(no_deleted_response.content)) as archive:
        deleted_index = archive.read("Review/deleted-memories.md").decode("utf-8")
        assert "User used to prefer tea." not in deleted_index
        assert "No deleted memories exported." in deleted_index


def test_deleted_memory_rest_restore(client, auth_headers, memory_store: MemoryStore):
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes espresso.",
        type="emotional",
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


def test_deleted_memory_rest_purge_success_audit_and_exports(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User private purge target is SECRET-PURGE-123.",
        type="semantic",
        sensitivity="private",
        source_message="The source also includes SECRET-PURGE-123.",
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="Earlier core fact includes SECRET-PURGE-123.",
        evidence_memory_ids=[memory.id],
        confidence=0.9,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="User has a private purge target.",
        evidence_memory_ids=[memory.id],
        confidence=0.9,
    )
    derived = memory_store.create_memory(
        user_id="default",
        content="Derived private fact SECRET-PURGE-123.",
        origin="agent_derived",
        evidence_memory_ids=[memory.id],
        sensitivity="private",
    )
    memory_store.create_decision_log(
        user_id="default",
        conversation_id=None,
        candidate_json=json.dumps({"memory": memory.content, "source_quote": memory.source_message}),
        decision="create",
        reason="Created SECRET-PURGE-123",
    )
    eval_init = client.post("/memories/evaluation/recall/init", headers=auth_headers)
    assert eval_init.status_code == 200
    snapshot_path = Path(eval_init.json()["snapshot"])
    assert snapshot_path.exists()
    memory_store.archive_memory(memory_id=memory.id, user_id="default")

    response = client.request(
        "DELETE",
        f"/memories/deleted/{memory.id}/purge",
        headers=auth_headers,
        json={"confirm_memory_id": memory.id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["purged"] is True
    assert payload["id"] == memory.id
    assert payload["audit_log_id"]
    assert payload["affected_core_memory_sections"][0]["section"] == "profile"
    assert payload["evaluation_cleanup"]["workspace_removed"] is True
    assert not snapshot_path.exists()
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is None
    assert memory_store.get_memory(memory_id=derived.id, user_id="default") is None
    assert memory_store.list_archived_memories(user_id="default") == []

    restore_response = client.post(
        f"/memories/{memory.id}/restore",
        headers=auth_headers,
    )
    assert restore_response.status_code == 404

    assert memory_store.list_core_memory_sections(user_id="default") == []
    [core_history] = memory_store.list_core_memory_section_history(user_id="default")
    assert core_history.content == "[redacted: purged evidence]"
    assert core_history.evidence_memory_ids == []

    logs = memory_store.list_decision_logs(user_id="default", limit=5)
    purge_log = next(log for log in logs if log.id == payload["audit_log_id"])
    assert purge_log.decision == "purge"
    audit = json.loads(purge_log.candidate_json)
    assert audit["source"] == "permanent_purge"
    assert audit["memory_id"] == memory.id
    assert audit["affected_core_sections"][0]["section"] == "profile"
    assert "SECRET-PURGE-123" not in purge_log.candidate_json
    assert "User has a private purge target" not in purge_log.candidate_json
    assert audit["scrubbed_artifacts"] == {
        "dependent_memories_deleted": 1,
        "derived_memories_deleted": 1,
        "temporal_references_relinked": 0,
        "core_sections_scrubbed": 1,
        "core_history_scrubbed": 1,
        "decision_logs_scrubbed": 1,
    }

    json_export = client.get("/memories/export", headers=auth_headers).json()
    exported_text = json.dumps(json_export, ensure_ascii=False)
    assert "SECRET-PURGE-123" not in exported_text
    assert "User has a private purge target" not in exported_text
    assert memory.id not in {item["id"] for item in json_export["deleted_memories"]}
    markdown_export = client.get(
        "/memories/export?format=markdown",
        headers=auth_headers,
    )
    assert "SECRET-PURGE-123" not in markdown_export.text
    obsidian_export = client.get(
        "/memories/export?format=obsidian_markdown",
        headers=auth_headers,
    )
    with zipfile.ZipFile(io.BytesIO(obsidian_export.content)) as archive:
        deleted_index = archive.read("Review/deleted-memories.md").decode("utf-8")
    assert "SECRET-PURGE-123" not in deleted_index


def test_deleted_memory_purge_reports_eval_cleanup_failure_after_commit(
    client,
    auth_headers,
    memory_store: MemoryStore,
    monkeypatch,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="Memory whose evaluation cleanup will fail.",
    )
    assert memory_store.archive_memory(memory_id=memory.id, user_id="default")

    def fail_cleanup(staged):
        return staged.result(cleanup_failed=True)

    monkeypatch.setattr(
        memories_api_module.common,
        "discard_staged_eval_workspace",
        fail_cleanup,
    )
    response = client.request(
        "DELETE",
        f"/memories/deleted/{memory.id}/purge",
        headers=auth_headers,
        json={"confirm_memory_id": memory.id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["purged"] is True
    assert payload["evaluation_cleanup"]["cleanup_failed"] is True
    assert payload["warnings"]
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is None
    assert memory_store.list_archived_memories(user_id="default") == []


def test_deleted_memory_purge_restores_eval_workspace_when_database_fails(
    client,
    auth_headers,
    memory_store: MemoryStore,
    monkeypatch,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="Memory whose database purge will fail.",
    )
    assert memory_store.archive_memory(memory_id=memory.id, user_id="default")
    initialized = client.post(
        "/memories/evaluation/recall/init",
        headers=auth_headers,
    )
    assert initialized.status_code == 200, initialized.text
    snapshot = Path(initialized.json()["snapshot"])
    assert snapshot.exists()

    def fail_purge(*args, **kwargs):
        del args, kwargs
        assert not snapshot.exists(), "workspace must move before the DB purge"
        raise RuntimeError("injected database purge failure")

    monkeypatch.setattr(
        type(memory_store),
        "purge_archived_memory",
        fail_purge,
    )

    with pytest.raises(RuntimeError, match="injected database purge failure"):
        client.request(
            "DELETE",
            f"/memories/deleted/{memory.id}/purge",
            headers=auth_headers,
            json={"confirm_memory_id": memory.id},
        )

    assert snapshot.exists()
    assert memory.id in {
        item.id for item in memory_store.list_archived_memories(user_id="default")
    }
    trash_root = snapshot.parents[2] / ".trash"
    assert trash_root.is_dir()
    assert {path.name for path in trash_root.iterdir()} == {
        ".memory-platform-evaluation-trash-v1"
    }


def test_deleted_memory_rest_purge_rejects_unsafe_requests(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    deleted = memory_store.create_memory(
        user_id="default",
        content="User deleted memory can be purged.",
        type="semantic",
    )
    memory_store.archive_memory(memory_id=deleted.id, user_id="default")
    active = memory_store.create_memory(
        user_id="default",
        content="User active memory must not be purged.",
        type="semantic",
    )
    other_user = memory_store.create_memory(
        user_id="other",
        content="Other user's deleted memory.",
        type="semantic",
    )
    memory_store.archive_memory(memory_id=other_user.id, user_id="other")

    mismatch = client.request(
        "DELETE",
        f"/memories/deleted/{deleted.id}/purge",
        headers=auth_headers,
        json={"confirm_memory_id": "not-the-same-id"},
    )
    assert mismatch.status_code == 422
    assert memory_store.list_archived_memories(user_id="default")

    missing_body = client.request(
        "DELETE",
        f"/memories/deleted/{deleted.id}/purge",
        headers=auth_headers,
    )
    assert missing_body.status_code == 422

    unauthorized = client.request(
        "DELETE",
        f"/memories/deleted/{deleted.id}/purge",
        json={"confirm_memory_id": deleted.id},
    )
    assert unauthorized.status_code == 401

    eval_init = client.post("/memories/evaluation/recall/init", headers=auth_headers)
    assert eval_init.status_code == 200
    snapshot_path = Path(eval_init.json()["snapshot"])
    assert snapshot_path.exists()

    active_response = client.request(
        "DELETE",
        f"/memories/deleted/{active.id}/purge",
        headers=auth_headers,
        json={"confirm_memory_id": active.id},
    )
    assert active_response.status_code == 404
    assert memory_store.get_memory(memory_id=active.id, user_id="default") is not None
    assert snapshot_path.exists()

    cross_user = client.request(
        "DELETE",
        f"/memories/deleted/{other_user.id}/purge",
        headers=auth_headers,
        json={"confirm_memory_id": other_user.id},
    )
    assert cross_user.status_code == 404

    success = client.request(
        "DELETE",
        f"/memories/deleted/{deleted.id}/purge",
        headers=auth_headers,
        json={"confirm_memory_id": deleted.id},
    )
    assert success.status_code == 200
    assert not snapshot_path.exists()

    repeat = client.request(
        "DELETE",
        f"/memories/deleted/{deleted.id}/purge",
        headers=auth_headers,
        json={"confirm_memory_id": deleted.id},
    )
    assert repeat.status_code == 404


def test_restore_export_imports_memories_for_current_user(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    space = memory_store.upsert_memory_space(user_id="default", name="Coffee")
    active = memory_store.create_memory(
        user_id="default",
        content="User likes pour-over coffee.",
        type="emotional",
        embedding_json="[0.3, 0.4]",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="coffee_method",
        topics=["coffee"],
        entities=["pour-over"],
        space_ids=[space.id],
    )
    deleted = memory_store.create_memory(
        user_id="default",
        content="User used to live in a test city.",
        type="semantic",
    )
    memory_store.archive_memory(memory_id=deleted.id, user_id="default")
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="User likes pour-over coffee.",
        evidence_memory_ids=[active.id],
        confidence=0.9,
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="User strongly prefers pour-over coffee.",
        evidence_memory_ids=[active.id],
        confidence=0.95,
    )
    memory_store.create_decision_log(
        user_id="default",
        conversation_id=None,
        candidate_json="{}",
        decision="create",
        reason="test audit",
    )
    export = client.get("/memories/export", headers=auth_headers).json()
    assert export["version"] == 3
    assert export["memory_spaces"][0]["name"] == "Coffee"
    assert export["memories"][0]["topics"] == ["coffee"]
    assert export["memories"][0]["valid_from"] == "2025-01-01"
    assert export["memories"][0]["temporal_subject"] == "user"
    assert export["memories"][0]["temporal_predicate"] == "coffee_method"
    assert export["restore_contract"]["snapshot_only_sections"] == [
        "core_memory_sections",
        "core_memory_section_history",
        "decision_logs",
    ]

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
    assert payload["not_restored_sections"] == [
        "core_memory_sections",
        "core_memory_section_history",
        "decision_logs",
    ]
    assert payload["warnings"]
    assert memory_store.list_core_memory_sections(user_id="restore-target") == []

    restored_active = memory_store.list_memories(user_id="restore-target")
    restored_deleted = memory_store.list_archived_memories(user_id="restore-target")
    restored_spaces = memory_store.list_memory_spaces(user_id="restore-target")
    assert [memory.content for memory in restored_active] == [active.content]
    assert [memory.content for memory in restored_deleted] == [deleted.content]
    assert restored_active[0].topics == ["coffee"]
    assert restored_active[0].entities == ["pour-over"]
    assert restored_active[0].valid_from == "2025-01-01"
    assert restored_active[0].temporal_subject == "user"
    assert restored_active[0].temporal_predicate == "coffee_method"
    assert restored_spaces[0].name == "Coffee"
    assert restored_active[0].space_ids == [restored_spaces[0].id]
    assert restored_active[0].embedding_json is None


def test_cross_user_restore_rebinds_memory_graph_references(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    source = memory_store.create_memory(
        user_id="default",
        content="Source memory for restore graph.",
        type="semantic",
    )
    derived = memory_store.create_memory(
        user_id="default",
        content="Derived memory for restore graph.",
        type="reflective",
        origin="agent_derived",
        evidence_memory_ids=[source.id],
    )
    with memory_store._connect() as connection:
        connection.execute(
            "UPDATE memories SET superseded_by = ? WHERE id = ? AND user_id = ?",
            (derived.id, source.id, "default"),
        )
        connection.execute(
            "UPDATE memories SET supersedes = ? WHERE id = ? AND user_id = ?",
            (source.id, derived.id, "default"),
        )

    export = client.get("/memories/export", headers=auth_headers).json()
    target_headers = {**auth_headers, "X-User-Id": "restore-graph-target"}
    response = client.post(
        "/memories/restore",
        headers=target_headers,
        json={"data": export},
    )

    assert response.status_code == 200
    restored = {
        memory.content: memory
        for memory in memory_store.list_memories(user_id="restore-graph-target")
    }
    restored_source = restored[source.content]
    restored_derived = restored[derived.content]
    assert restored_source.id != source.id
    assert restored_derived.id != derived.id
    assert restored_derived.evidence_memory_ids == [restored_source.id]
    assert restored_source.superseded_by == restored_derived.id
    assert restored_derived.supersedes == restored_source.id
    assert memory_store.get_memory(memory_id=source.id, user_id="default") is not None
    assert memory_store.get_memory(memory_id=derived.id, user_id="default") is not None


def test_same_user_partial_restore_preserves_existing_graph_reference(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    evidence = memory_store.create_memory(
        user_id="default",
        content="Existing evidence outside the partial restore.",
    )
    derived = memory_store.create_memory(
        user_id="default",
        content="Derived row restored by itself.",
        origin="agent_derived",
        evidence_memory_ids=[evidence.id],
    )
    export = build_memory_export(store=memory_store, user_id="default")
    export["memories"] = [
        memory for memory in export["memories"] if memory["id"] == derived.id
    ]
    export["deleted_memories"] = []

    response = client.post(
        "/memories/restore",
        headers=auth_headers,
        json={"data": export, "overwrite": True},
    )

    assert response.status_code == 200
    restored = memory_store.get_memory(memory_id=derived.id, user_id="default")
    assert restored is not None
    assert restored.evidence_memory_ids == [evidence.id]
    assert response.json()["dangling_references_removed"] == 0


def test_partial_overwrite_restore_rebuilds_both_temporal_keys(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    latest = memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    export = build_memory_export(store=memory_store, user_id="default")
    [raw_old] = [
        memory for memory in export["memories"] if memory["id"] == old.id
    ]
    raw_old["temporal_subject"] = "former_user"
    export["memories"] = [raw_old]
    export["deleted_memories"] = []

    response = client.post(
        "/memories/restore",
        headers=auth_headers,
        json={"data": export, "overwrite": True},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == 1
    moved = memory_store.get_memory(memory_id=old.id, user_id="default")
    latest_after = memory_store.get_memory(
        memory_id=latest.id,
        user_id="default",
    )
    assert moved is not None
    assert latest_after is not None
    assert moved.temporal_subject == "former_user"
    assert moved.valid_until is None
    assert moved.supersedes is None
    assert moved.superseded_by is None
    assert moved.status == "dynamic"
    assert latest_after.temporal_subject == "user"
    assert latest_after.supersedes is None
    assert latest_after.superseded_by is None
    assert latest_after.valid_until is None
    assert latest_after.status == "dynamic"
    [restored_payload] = response.json()["restored_memories"]
    assert restored_payload["superseded_by"] is None
    assert restored_payload["valid_until"] is None


def test_restore_prunes_reference_to_invalid_preallocated_source(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    export = {
        "version": 3,
        "user_id": "source-user",
        "memories": [
            {"id": "invalid-source", "content": ""},
            {
                "id": "valid-dependent",
                "content": "A valid dependent row.",
                "origin": "agent_derived",
                "evidence_memory_ids": ["invalid-source"],
            },
        ],
        "deleted_memories": [],
    }
    target_headers = {**auth_headers, "X-User-Id": "restore-invalid-target"}

    response = client.post(
        "/memories/restore",
        headers=target_headers,
        json={"data": export},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["invalid"] == 1
    assert payload["created"] == 1
    assert payload["dangling_references_removed"] == 1
    [restored] = memory_store.list_memories(user_id="restore-invalid-target")
    assert restored.evidence_memory_ids == []
    assert payload["restored_memories"][0]["evidence_memory_ids"] == []


def test_restore_counts_invalid_classification_instead_of_failing_request(
    client,
    auth_headers,
) -> None:
    response = client.post(
        "/memories/restore",
        headers=auth_headers,
        json={
            "data": {
                "version": 3,
                "user_id": "default",
                "memories": [
                    {
                        "id": "invalid-long-topic",
                        "content": "Valid content with corrupt metadata.",
                        "topics": ["x" * 41],
                    }
                ],
                "deleted_memories": [],
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["invalid"] == 1


def test_restore_counts_invalid_archived_context_metadata(
    client,
    auth_headers,
) -> None:
    response = client.post(
        "/memories/restore",
        headers=auth_headers,
        json={
            "data": {
                "version": 3,
                "user_id": "default",
                "memories": [],
                "deleted_memories": [],
                "recent_context_summaries": [
                    {"summary": "valid summary", "archived": "not-an-integer"}
                ],
                "conversation_branch_nodes": [
                    {"archived": "not-an-integer"}
                ],
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["recent_context_invalid"] == 1
    assert payload["branch_nodes_invalid"] == 1


def test_merge_and_review_request_limits_are_bounded(client, auth_headers) -> None:
    too_many_ids = [f"memory-{index}" for index in range(101)]
    assert client.post(
        "/memories/merge",
        headers=auth_headers,
        json={"memory_ids": too_many_ids},
    ).status_code == 422
    assert client.post(
        "/memories/merge",
        headers=auth_headers,
        json={"memory_ids": ["memory-a", "memory-b"], "content": "x" * 20_001},
    ).status_code == 422
    assert client.post(
        "/memories/review?limit=0",
        headers=auth_headers,
    ).status_code == 422
    assert client.post(
        "/memories/review?limit=1001",
        headers=auth_headers,
    ).status_code == 422


def test_restore_export_imports_recent_context_with_overwrite_policy(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory_store.upsert_recent_context_state(
        user_id="default",
        conversation_id="ctx-export",
        summary="较早摘要\n\n用户：最近问题",
        compressed_summary="较早摘要",
        recent_turns=[
            RecentContextTurn(user="最近问题", assistant="最近回答"),
        ],
        turn_count=9,
    )
    export = client.get("/memories/export", headers=auth_headers).json()
    exported_context = export["recent_context_summaries"][0]
    assert exported_context["compressed_summary"] == "较早摘要"
    assert exported_context["recent_turns"][0]["user"] == "最近问题"
    assert exported_context["turn_count"] == 9

    target_headers = {**auth_headers, "X-User-Id": "recent-restore-target"}
    memory_store.upsert_recent_context_summary(
        user_id="recent-restore-target",
        conversation_id="ctx-export",
        summary="用户：旧摘要",
    )

    skipped = client.post(
        "/memories/restore",
        headers=target_headers,
        json={"data": export, "overwrite": False},
    )
    assert skipped.status_code == 200
    assert skipped.json()["recent_context_skipped"] == 1
    unchanged = memory_store.get_recent_context_summary_for_conversation(
        user_id="recent-restore-target",
        conversation_id="ctx-export",
    )
    assert unchanged is not None
    assert unchanged.summary == "用户：旧摘要"

    overwritten = client.post(
        "/memories/restore",
        headers=target_headers,
        json={"data": export, "overwrite": True},
    )
    assert overwritten.status_code == 200
    assert overwritten.json()["recent_context_updated"] == 1
    restored = memory_store.get_recent_context_summary_for_conversation(
        user_id="recent-restore-target",
        conversation_id="ctx-export",
    )
    assert restored is not None
    assert restored.compressed_summary == "较早摘要"
    assert restored.recent_turns[0].assistant == "最近回答"
    assert restored.turn_count == 9


def test_export_restore_includes_conversation_branch_nodes(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint="1" * 64,
        parent_history_fingerprint="",
        turn_fingerprint="2" * 64,
        assistant_digest="3" * 64,
        summary="用户：问题\n助手：回答",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="问题", assistant="回答")],
        turn_count=1,
    )
    export = client.get("/memories/export", headers=auth_headers).json()

    assert export["version"] == 3
    assert export["conversation_branch_nodes"][0]["history_fingerprint"] == "1" * 64

    target_headers = {**auth_headers, "X-User-Id": "branch-restore-target"}
    restored = client.post(
        "/memories/restore",
        headers=target_headers,
        json={"data": export},
    )

    assert restored.status_code == 200
    assert restored.json()["branch_nodes_created"] == 1
    node = memory_store.get_conversation_branch_node(
        user_id="branch-restore-target",
        history_fingerprint="1" * 64,
    )
    assert node is not None
    assert node.recent_turns[0].assistant == "回答"


def test_conversation_branch_rest_list_and_archive_subtree(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    root = memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint="a" * 64,
        parent_history_fingerprint="",
        turn_fingerprint="b" * 64,
        assistant_digest="c" * 64,
        summary="根节点",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="根问题", assistant="根回答")],
        turn_count=1,
    )
    child = memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint="d" * 64,
        parent_history_fingerprint=root.history_fingerprint,
        turn_fingerprint="e" * 64,
        assistant_digest="f" * 64,
        summary="子节点",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="子问题", assistant="子回答")],
        turn_count=2,
    )
    grandchild = memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint="1" * 64,
        parent_history_fingerprint=child.history_fingerprint,
        turn_fingerprint="2" * 64,
        assistant_digest="3" * 64,
        summary="孙节点",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="孙问题", assistant="孙回答")],
        turn_count=3,
    )
    other = memory_store.upsert_conversation_branch_node(
        user_id="other",
        conversation_id=None,
        history_fingerprint="4" * 64,
        parent_history_fingerprint="",
        turn_fingerprint="5" * 64,
        assistant_digest="6" * 64,
        summary="其他用户",
        compressed_summary="",
        recent_turns=[],
        turn_count=1,
    )

    listed = client.get(
        "/memories/conversation-branches?limit=2",
        headers=auth_headers,
    )
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["meta"] == {
        "status": "active",
        "total": 3,
        "returned": 2,
        "truncated": True,
    }
    assert all(node["user_id"] == "default" for node in payload["data"])

    wrong_user = client.delete(
        f"/memories/conversation-branches/{other.id}",
        headers=auth_headers,
    )
    assert wrong_user.status_code == 404

    archived = client.delete(
        f"/memories/conversation-branches/{child.id}",
        headers=auth_headers,
    )
    assert archived.status_code == 200
    assert archived.json()["archived_count"] == 2
    assert memory_store.get_conversation_branch_node(
        user_id="default",
        history_fingerprint=child.history_fingerprint,
    ) is None
    assert memory_store.get_conversation_branch_node(
        user_id="default",
        history_fingerprint=grandchild.history_fingerprint,
    ) is None
    assert memory_store.get_conversation_branch_node(
        user_id="default",
        history_fingerprint=root.history_fingerprint,
    ) is not None

    archived_list = client.get(
        "/memories/conversation-branches?status=archived",
        headers=auth_headers,
    )
    assert archived_list.status_code == 200
    archived_payload = archived_list.json()
    assert archived_payload["meta"]["status"] == "archived"
    assert archived_payload["meta"]["total"] == 2
    assert {node["id"] for node in archived_payload["data"]} == {
        child.id,
        grandchild.id,
    }

    repeated = client.delete(
        f"/memories/conversation-branches/{child.id}",
        headers=auth_headers,
    )
    assert repeated.status_code == 404

    restored = client.post(
        f"/memories/conversation-branches/{child.id}/restore",
        headers=auth_headers,
    )
    assert restored.status_code == 200
    assert restored.json()["restored_count"] == 2

    repeated_restore = client.post(
        f"/memories/conversation-branches/{child.id}/restore",
        headers=auth_headers,
    )
    assert repeated_restore.status_code == 404


def test_memory_spaces_and_classification_rest_endpoints(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User is organizing memory spaces.",
        type="semantic",
        topics=["phase four"],
        entities=["Memory Gateway"],
    )

    patch_response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"topics": ["phase four", "classification"], "entities": ["SQLite"]},
    )
    assert patch_response.status_code == 200
    patched = patch_response.json()["memory"]
    assert patched["topics"] == ["phase four", "classification"]
    assert patched["entities"] == ["SQLite"]
    assert patched["space_ids"] == []

    spaces_response = client.patch(
        f"/memories/{memory.id}/spaces",
        headers=auth_headers,
        json={"create_space_names": ["Work", "Work"]},
    )
    assert spaces_response.status_code == 200
    updated = spaces_response.json()["memory"]
    assert len(updated["space_ids"]) == 1

    list_response = client.get("/memories", headers=auth_headers)
    assert list_response.status_code == 200
    listed = list_response.json()["data"][0]
    assert listed["topics"] == ["phase four", "classification"]
    assert listed["entities"] == ["SQLite"]
    assert listed["space_ids"] == updated["space_ids"]

    search_response = client.post(
        "/memories/search",
        headers=auth_headers,
        json={"query": "organizing memory", "limit": 5},
    )
    assert search_response.status_code == 200
    assert search_response.json()["data"][0]["topics"] == ["phase four", "classification"]

    surface_response = client.post(
        "/memories/surface",
        headers=auth_headers,
        json={"limit": 5, "mode": "balanced"},
    )
    assert surface_response.status_code == 200
    assert surface_response.json()["data"][0]["space_ids"] == updated["space_ids"]

    spaces = client.get("/memories/spaces", headers=auth_headers).json()["data"]
    assert [(space["name"], space["active_memory_count"]) for space in spaces] == [("Work", 1)]

    detail = client.get(f"/memories/spaces/{updated['space_ids'][0]}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["space"]["name"] == "Work"
    assert [item["id"] for item in detail.json()["memories"]] == [memory.id]

    logs = memory_store.list_decision_logs(user_id="default", limit=10)
    classification_logs = [
        log for log in logs if json.loads(log.candidate_json).get("source") == "classification_update"
    ]
    assert len(classification_logs) == 2


def test_decision_logs_rest_filter_by_memory_id(client, auth_headers, memory_store: MemoryStore):
    coffee = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee.",
        type="emotional",
    )
    tea = memory_store.create_memory(
        user_id="default",
        content="User used to prefer tea.",
        type="emotional",
    )
    for memory, feedback in ((coffee, "useful"), (tea, "not_useful")):
        response = client.post(
            "/memories/search-feedback",
            headers=auth_headers,
            json={"memory_id": memory.id, "query": "drinks", "feedback": feedback},
        )
        assert response.status_code == 200

    filtered = client.get(
        f"/memories/decision-logs?memory_id={coffee.id}",
        headers=auth_headers,
    )
    assert filtered.status_code == 200
    logs = filtered.json()["data"]
    assert len(logs) == 1
    payload = json.loads(logs[0]["candidate_json"])
    assert payload["memory_id"] == coffee.id

    # 其他用户命名空间下查同一个 memory_id，不应命中当前用户的日志
    cross_user = client.get(
        f"/memories/decision-logs?memory_id={tea.id}",
        headers={**auth_headers, "X-User-Id": "alice"},
    )
    assert cross_user.status_code == 200
    assert cross_user.json()["data"] == []

    assert client.get(
        "/memories/decision-logs?limit=-1",
        headers=auth_headers,
    ).status_code == 422
    assert client.get(
        "/memories/core/history?limit=-1",
        headers=auth_headers,
    ).status_code == 422


def test_restore_version_one_export_defaults_classification_fields(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    export = {
        "version": 1,
        "exported_at": "2026-01-01T00:00:00+00:00",
        "user_id": "default",
        "embedding_included": False,
        "memories": [
            {
                "id": "legacy-memory",
                "content": "User likes local-first tools.",
                "type": "emotional",
                "importance": 7,
                "confidence": 0.9,
                "valence": 0.5,
                "arousal": 0.3,
                "usage_count": 0,
                "stability": "stable",
                "sensitivity": "normal",
                "evidence_memory_ids": [],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "archived": 0,
            }
        ],
        "deleted_memories": [],
    }

    response = client.post(
        "/memories/restore",
        headers=auth_headers,
        json={"data": export},
    )

    assert response.status_code == 200
    memory = memory_store.get_memory(memory_id="legacy-memory", user_id="default")
    assert memory is not None
    assert memory.topics == []
    assert memory.entities == []
    assert memory.space_ids == []


def test_patch_memory_updates_content_and_clears_embedding(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee.",
        type="emotional",
        importance=7,
        confidence=0.9,
        embedding_json="[0.1, 0.2]",
        topics=["Solarized"],
        entities=["Solarized Dark"],
    )

    response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"content": "User prefers the Nord theme."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["updated"] is True
    assert payload["memory"]["content"] == "User prefers the Nord theme."
    assert "embedding_json" not in payload["memory"]

    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert stored is not None
    assert stored.content == "User prefers the Nord theme."
    assert stored.embedding_json is None
    assert "Solarized" not in stored.topics
    assert "Solarized Dark" not in stored.entities
    assert "Nord" in stored.entities


def test_patch_memory_can_explicitly_preserve_derived_metadata(
    client, auth_headers, memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User prefers Solarized Dark.",
        type="emotional",
        topics=["theme"],
        entities=["Solarized Dark"],
    )

    response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"content": "User prefers Nord.", "preserve_metadata": True},
    )

    assert response.status_code == 200
    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert stored is not None
    assert stored.entities == ["Solarized Dark"]
    assert stored.embedding_json is None


def test_gateway_key_is_bound_to_configured_user_by_default(
    client, auth_headers, monkeypatch,
):
    from app.config import get_settings

    monkeypatch.setenv("GATEWAY_ALLOW_USER_ID_HEADER", "false")
    monkeypatch.setenv("GATEWAY_USER_ID", "alice")
    get_settings.cache_clear()
    try:
        allowed = client.get("/memories", headers={**auth_headers, "X-User-Id": "alice"})
        rejected = client.get("/memories", headers={**auth_headers, "X-User-Id": "bob"})
        implicit = client.get("/memories", headers=auth_headers)
        assert allowed.status_code == 200
        assert implicit.status_code == 200
        assert rejected.status_code == 403
    finally:
        monkeypatch.setenv("GATEWAY_ALLOW_USER_ID_HEADER", "true")
        monkeypatch.delenv("GATEWAY_USER_ID", raising=False)
        get_settings.cache_clear()


def test_patch_memory_partial_update_preserves_other_fields_and_embedding(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes pour-over coffee.",
        type="emotional",
        importance=6,
        confidence=0.8,
        source_message="I like pour-over coffee.",
        source_conversation_id="conv-1",
        embedding_json="[0.3, 0.4]",
        stability="stable",
        valid_from="2025-01-01",
        valid_until="2026-12-31",
        review_after="2026-07-01",
        sensitivity="private",
        temporal_subject="user",
        temporal_predicate="coffee_preference",
    )

    response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"importance": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["memory"]["importance"] == 5
    assert payload["memory"]["content"] == memory.content
    assert payload["memory"]["type"] == memory.type
    assert payload["memory"]["confidence"] == memory.confidence
    assert payload["memory"]["source_message"] == memory.source_message
    assert payload["memory"]["source_conversation_id"] == memory.source_conversation_id
    assert payload["memory"]["stability"] == memory.stability
    assert payload["memory"]["valid_from"] == memory.valid_from
    assert payload["memory"]["valid_until"] == memory.valid_until
    assert payload["memory"]["review_after"] == memory.review_after
    assert payload["memory"]["sensitivity"] == memory.sensitivity
    assert payload["memory"]["temporal_subject"] == memory.temporal_subject
    assert payload["memory"]["temporal_predicate"] == memory.temporal_predicate
    assert "embedding_json" not in payload["memory"]

    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert stored is not None
    assert stored.embedding_json == "[0.3, 0.4]"


def test_patch_memory_can_clear_nullable_fields(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User has a temporary testing note.",
        source_message="Please remember this temporary note.",
        source_conversation_id="conv-clear",
        valid_from="2025-01-01",
        valid_until="2026-12-31",
        review_after="2026-07-01",
        temporal_subject="user",
        temporal_predicate="temporary_note",
    )

    response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={
            "source_message": None,
            "source_conversation_id": None,
            "valid_from": None,
            "valid_until": None,
            "review_after": None,
            "temporal_subject": None,
            "temporal_predicate": None,
        },
    )

    assert response.status_code == 200
    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert stored is not None
    assert stored.source_message is None
    assert stored.source_conversation_id is None
    assert stored.valid_from is None
    assert stored.valid_until is None
    assert stored.review_after is None
    assert stored.temporal_subject is None
    assert stored.temporal_predicate is None


def test_patch_memory_rejects_invalid_valid_from(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    memory = memory_store.create_memory(
        user_id="default",
        content="User has a temporal note.",
    )

    response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"valid_from": "not-a-date"},
    )

    assert response.status_code == 422


def test_patch_memory_rejects_invalid_valid_until_and_review_after(
    client,
    auth_headers,
    memory_store: MemoryStore,
):
    for field in ("valid_until", "review_after"):
        memory = memory_store.create_memory(
            user_id="default",
            content=f"User has an invalid {field} test.",
        )
        response = client.patch(
            f"/memories/{memory.id}",
            headers=auth_headers,
            json={field: "not-a-date"},
        )
        assert response.status_code == 422


def test_patch_memory_missing_id_returns_404(client, auth_headers):
    response = client.patch(
        "/memories/missing-memory-id",
        headers=auth_headers,
        json={"importance": 5},
    )

    assert response.status_code == 404


def test_patch_deleted_memory_returns_404(client, auth_headers, memory_store: MemoryStore):
    memory = memory_store.create_memory(
        user_id="default",
        content="User used to like tea.",
    )
    memory_store.archive_memory(memory_id=memory.id, user_id="default")

    response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"importance": 5},
    )

    assert response.status_code == 404


def test_patch_memory_rejects_empty_content(client, auth_headers, memory_store: MemoryStore):
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes coffee.",
    )

    response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"content": "   "},
    )

    assert response.status_code == 422
    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert stored is not None
    assert stored.content == "User likes coffee."


def test_patch_memory_requires_authorization(client, memory_store: MemoryStore):
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes coffee.",
    )

    response = client.patch(
        f"/memories/{memory.id}",
        json={"importance": 5},
    )

    assert response.status_code == 401


def test_patch_status_archived_uses_full_archive_semantics(client, auth_headers, memory_store: MemoryStore):
    memory = memory_store.create_memory(user_id="default", content="稍后归档的一条记忆。")

    response = client.patch(
        f"/memories/{memory.id}",
        headers=auth_headers,
        json={"status": "archived"},
    )

    assert response.status_code == 200
    assert response.json()["archived"] is True
    deleted = client.get("/memories/deleted", headers=auth_headers)
    assert deleted.status_code == 200
    assert memory.id in {item["id"] for item in deleted.json()["data"]}

    other = memory_store.create_memory(user_id="default", content="另一条记忆。")
    rejected = client.patch(
        f"/memories/{other.id}",
        headers=auth_headers,
        json={"status": "archived", "importance": 9},
    )
    assert rejected.status_code == 422


def test_manual_resolved_status_survives_temporal_chain_rebuild(client, auth_headers, memory_store: MemoryStore):
    older = memory_store.create_memory(
        user_id="default",
        content="用户的旧编辑器是 Vim。",
        temporal_subject="用户",
        temporal_predicate="编辑器",
    )
    current = memory_store.create_memory(
        user_id="default",
        content="用户的编辑器是 Neovim。",
        temporal_subject="用户",
        temporal_predicate="编辑器",
    )
    response = client.patch(
        f"/memories/{current.id}",
        headers=auth_headers,
        json={"status": "resolved"},
    )
    assert response.status_code == 200

    assert memory_store.archive_memory(memory_id=older.id, user_id="default")

    after = memory_store.get_memory(memory_id=current.id, user_id="default")
    assert after is not None
    assert after.status == "resolved"
