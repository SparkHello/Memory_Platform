import pytest

from app.memory.report import restore_memory_export
from app.memory.store import MemoryStore


def _restore_payload() -> dict:
    return {
        "version": 3,
        "user_id": "source-user",
        "memory_spaces": [
            {
                "id": "source-space",
                "name": "Atomic restore space",
            }
        ],
        "memories": [
            {
                "id": "source-memory-1",
                "content": "First atomic restore memory.",
                "space_ids": ["source-space"],
            },
            {
                "id": "source-memory-2",
                "content": "Second atomic restore memory.",
                "evidence_memory_ids": ["source-memory-1"],
                "space_ids": ["source-space"],
            },
        ],
        "deleted_memories": [],
        "recent_context_summaries": [
            {
                "conversation_id": "atomic-restore",
                "summary": "Atomic recent context.",
            }
        ],
        "conversation_branch_nodes": [],
    }


def test_restore_rolls_back_every_partition_on_unexpected_write_failure(
    memory_store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = memory_store._import_prepared_memory_record_on_connection
    calls = 0

    def fail_on_second_memory(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected restore write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        memory_store,
        "_import_prepared_memory_record_on_connection",
        fail_on_second_memory,
    )

    with pytest.raises(RuntimeError, match="injected restore write failure"):
        restore_memory_export(
            store=memory_store,
            user_id="restore-target",
            export_data=_restore_payload(),
        )

    assert memory_store.list_memories(user_id="restore-target") == []
    assert memory_store.list_memory_spaces(user_id="restore-target") == []
    assert memory_store.list_recent_context_summaries(user_id="restore-target") == []


def test_restore_dry_run_returns_the_real_plan_without_persisting(
    memory_store: MemoryStore,
) -> None:
    result = restore_memory_export(
        store=memory_store,
        user_id="restore-target",
        export_data=_restore_payload(),
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["spaces_created"] == 1
    assert result["created"] == 2
    assert result["recent_context_created"] == 1
    assert len(result["restored_memories"]) == 2
    assert memory_store.list_memories(user_id="restore-target") == []
    assert memory_store.list_memory_spaces(user_id="restore-target") == []
    assert memory_store.list_recent_context_summaries(user_id="restore-target") == []


def test_restore_keeps_valid_rows_when_other_records_are_invalid(
    memory_store: MemoryStore,
) -> None:
    payload = _restore_payload()
    payload["memories"].insert(1, {"id": "invalid-memory", "content": ""})
    payload["memory_spaces"].append({"id": "invalid-space", "name": ""})

    result = restore_memory_export(
        store=memory_store,
        user_id="restore-target",
        export_data=payload,
    )

    assert result["invalid"] == 1
    assert result["spaces_invalid"] == 1
    assert result["created"] == 2
    assert len(memory_store.list_memories(user_id="restore-target")) == 2

