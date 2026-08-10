import json

import pytest

from app.memory.store import MemoryStore
from app.memory.store import lifecycle_purge as store_module


def _preview(client, auth_headers, memory_ids, *, headers=None):
    return client.post(
        "/memories/deleted/purge/preview",
        headers=headers or auth_headers,
        json={"memory_ids": memory_ids},
    )


def _commit(client, auth_headers, preview, *, memory_ids=None, headers=None):
    return client.post(
        "/memories/deleted/purge/commit",
        headers=headers or auth_headers,
        json={
            "memory_ids": memory_ids or preview["requested_memory_ids"],
            "fingerprint": preview["fingerprint"],
            "preview_token": preview["preview_token"],
        },
    )


def test_batch_purge_previews_real_closure_and_commits_one_audit(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    first = memory_store.create_memory(user_id="default", content="First purge root.")
    second = memory_store.create_memory(user_id="default", content="Second purge root.")
    dependent = memory_store.create_memory(
        user_id="default",
        content="A derived memory backed by the first root.",
        origin="agent_derived",
        evidence_memory_ids=[first.id],
    )
    _, core = memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="Core content backed by the derived memory.",
        evidence_memory_ids=[dependent.id],
        confidence=0.9,
    )
    assert memory_store.archive_memory(memory_id=first.id, user_id="default")
    assert memory_store.archive_memory(memory_id=second.id, user_id="default")

    response = _preview(client, auth_headers, [second.id, first.id])

    assert response.status_code == 200
    preview = response.json()
    assert preview["requested_memory_ids"] == sorted([first.id, second.id])
    assert set(preview["purge_memory_ids"]) == {first.id, second.id, dependent.id}
    assert preview["dependent_memory_ids"] == [dependent.id]
    assert preview["affected_core_memory_sections"] == [
        {
            "id": core.id,
            "section": "profile",
            "version": 1,
            "active": True,
        }
    ]
    assert preview["effects"] == {
        "requested_memories_deleted": 2,
        "dependent_memories_deleted": 1,
        "memories_deleted": 3,
        "space_links_deleted": 0,
        "temporal_references_relinked": 0,
        "core_sections_scrubbed": 1,
        "core_history_scrubbed": 0,
        "decision_logs_scrubbed": 0,
    }
    assert len(preview["fingerprint"]) == 64
    assert preview["preview_token"]

    commit_response = _commit(client, auth_headers, preview)

    assert commit_response.status_code == 200
    result = commit_response.json()
    assert result["purged"] is True
    assert result["requested_memory_ids"] == preview["requested_memory_ids"]
    assert result["purged_memory_ids"] == preview["purge_memory_ids"]
    assert result["effects"] == preview["effects"]
    assert result["audit_log_id"]
    for memory_id in (first.id, second.id, dependent.id):
        assert memory_store.get_memory(memory_id=memory_id, user_id="default") is None
    purge_logs = [
        log
        for log in memory_store.list_decision_logs(user_id="default", limit=100)
        if log.decision == "purge"
    ]
    assert [log.id for log in purge_logs] == [result["audit_log_id"]]
    audit = json.loads(purge_logs[0].candidate_json)
    assert audit["source"] == "permanent_purge_batch"
    assert audit["requested_memory_ids"] == preview["requested_memory_ids"]
    assert audit["purged_memory_ids"] == preview["purge_memory_ids"]


def test_batch_purge_rejects_stale_closure_without_partial_delete(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    root = memory_store.create_memory(user_id="default", content="Stale preview root.")
    assert memory_store.archive_memory(memory_id=root.id, user_id="default")
    preview = _preview(client, auth_headers, [root.id]).json()
    dependent = memory_store.create_memory(
        user_id="default",
        content="Created after preview.",
        evidence_memory_ids=[root.id],
    )

    response = _commit(client, auth_headers, preview)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "purge_preview_stale"
    assert {item.id for item in memory_store.list_archived_memories(user_id="default")} == {
        root.id
    }
    assert memory_store.get_memory(memory_id=dependent.id, user_id="default") is not None
    assert not any(
        log.decision == "purge"
        for log in memory_store.list_decision_logs(user_id="default", limit=100)
    )


def test_batch_purge_rejects_cross_user_and_changed_selection(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    root = memory_store.create_memory(user_id="default", content="User-bound purge root.")
    other = memory_store.create_memory(user_id="default", content="Other archived root.")
    assert memory_store.archive_memory(memory_id=root.id, user_id="default")
    assert memory_store.archive_memory(memory_id=other.id, user_id="default")
    preview = _preview(client, auth_headers, [root.id]).json()

    cross_user = _commit(
        client,
        auth_headers,
        preview,
        headers={**auth_headers, "X-User-Id": "other"},
    )
    changed_selection = _commit(
        client,
        auth_headers,
        preview,
        memory_ids=[root.id, other.id],
    )

    assert cross_user.status_code == 409
    assert cross_user.json()["detail"]["code"] == "purge_preview_mismatch"
    assert changed_selection.status_code == 409
    assert changed_selection.json()["detail"]["code"] == "purge_preview_mismatch"
    assert {item.id for item in memory_store.list_archived_memories(user_id="default")} == {
        root.id,
        other.id,
    }


def test_batch_purge_preview_rejects_partial_missing_selection(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    archived = memory_store.create_memory(user_id="default", content="Existing root.")
    assert memory_store.archive_memory(memory_id=archived.id, user_id="default")

    response = _preview(client, auth_headers, [archived.id, "missing-memory"])

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "purge_targets_missing"
    assert detail["missing_memory_ids"] == ["missing-memory"]
    assert [item.id for item in memory_store.list_archived_memories(user_id="default")] == [
        archived.id
    ]


@pytest.mark.parametrize(
    "memory_ids",
    [[], ["duplicate", "duplicate"], [f"memory-{index}" for index in range(1001)]],
)
def test_batch_purge_selection_bounds_are_rejected_before_store_access(
    client,
    auth_headers,
    memory_ids,
) -> None:
    response = _preview(client, auth_headers, memory_ids)

    assert response.status_code == 422


def test_batch_purge_transaction_fault_rolls_back_every_partition(
    client,
    auth_headers,
    memory_store: MemoryStore,
    monkeypatch,
) -> None:
    root = memory_store.create_memory(user_id="default", content="Rollback root.")
    dependent = memory_store.create_memory(
        user_id="default",
        content="Rollback dependent.",
        evidence_memory_ids=[root.id],
    )
    _, core = memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="Rollback core.",
        evidence_memory_ids=[dependent.id],
        confidence=0.9,
    )
    assert memory_store.archive_memory(memory_id=root.id, user_id="default")
    preview = _preview(client, auth_headers, [root.id]).json()

    def fail_audit(*args, **kwargs):
        raise RuntimeError("forced audit insert failure")

    monkeypatch.setattr(store_module, "_insert_batch_purge_audit", fail_audit)
    with pytest.raises(RuntimeError, match="forced audit insert failure"):
        _commit(client, auth_headers, preview)

    assert [item.id for item in memory_store.list_archived_memories(user_id="default")] == [
        root.id
    ]
    assert memory_store.get_memory(memory_id=dependent.id, user_id="default") is not None
    [core_after] = memory_store.list_core_memory_sections(user_id="default")
    assert core_after.id == core.id
    assert core_after.content == "Rollback core."
    assert not any(
        log.decision == "purge"
        for log in memory_store.list_decision_logs(user_id="default", limit=100)
    )


def test_restore_api_exposes_domain_dry_run(
    client,
    auth_headers,
    memory_store: MemoryStore,
) -> None:
    export_data = {
        "version": 3,
        "user_id": "default",
        "memories": [{"id": "dry-run-memory", "content": "Dry-run only."}],
    }

    response = client.post(
        "/memories/restore",
        headers=auth_headers,
        json={"data": export_data, "dry_run": True},
    )

    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert response.json()["created"] == 1
    assert memory_store.get_memory(memory_id="dry-run-memory", user_id="default") is None
