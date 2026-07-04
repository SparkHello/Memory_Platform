import json
from pathlib import Path

from app.memory.store import MemoryStore
from scripts.backfill_memory_classification import run_backfill


def _space_names_for(store: MemoryStore, space_ids: list[str]) -> list[str]:
    spaces = {
        space.id: space.name
        for space in store.list_memory_spaces(user_id="default")
    }
    return [spaces[space_id] for space_id in space_ids]


def test_backfill_dry_run_does_not_modify_database(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=7,
        confidence=0.9,
    )

    result = run_backfill(database=memory_store.database_path, dry_run=True)

    assert result["dry_run"] is True
    assert result["would_update_count"] == 1
    assert result["updated_count"] == 0
    assert result["backup_path"] is None
    assert list(Path(memory_store.database_path).parent.glob("memory.backup.*.db")) == []
    unchanged = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert unchanged is not None
    assert unchanged.topics == []
    assert unchanged.entities == []
    assert unchanged.space_ids == []


def test_backfill_updates_active_and_archived_memories_idempotently(
    memory_store: MemoryStore,
) -> None:
    active = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=7,
        confidence=0.9,
    )
    archived = memory_store.create_memory(
        user_id="default",
        content="用户现在主要用 Kelivo 做 AI 客户端。",
        type="semantic",
        importance=7,
        confidence=0.9,
    )
    assert memory_store.archive_memory(memory_id=archived.id, user_id="default")

    result = run_backfill(database=memory_store.database_path)

    assert result["dry_run"] is False
    assert result["would_update_count"] == 2
    assert result["updated_count"] == 2
    assert result["backup_path"] is not None
    assert Path(result["backup_path"]).exists()

    refreshed_active = memory_store.get_memory(memory_id=active.id, user_id="default")
    assert refreshed_active is not None
    assert "偏好" in refreshed_active.topics
    assert "个人偏好" in _space_names_for(memory_store, refreshed_active.space_ids)

    archived_memories = memory_store.list_archived_memories(user_id="default", limit=10)
    refreshed_archived = next(memory for memory in archived_memories if memory.id == archived.id)
    assert "工具" in refreshed_archived.topics
    assert "Kelivo" in refreshed_archived.entities
    assert "工具与设备" in _space_names_for(memory_store, refreshed_archived.space_ids)

    logs = memory_store.list_decision_logs(user_id="default", limit=10)
    backfill_logs = [log for log in logs if "classification_backfill" in log.reason]
    assert len(backfill_logs) == 2
    for log in backfill_logs:
        payload = json.loads(log.candidate_json)
        assert payload["source"] == "classification_backfill"
        assert "content" not in payload
        assert payload["content_length"] > 0
        assert len(payload["content_sha256"]) == 64

    spaces_before = memory_store.list_memory_spaces(user_id="default")
    second = run_backfill(database=memory_store.database_path)
    assert second["would_update_count"] == 0
    assert second["updated_count"] == 0
    assert len(memory_store.list_memory_spaces(user_id="default")) == len(spaces_before)
    assert len(
        [
            log
            for log in memory_store.list_decision_logs(user_id="default", limit=10)
            if "classification_backfill" in log.reason
        ]
    ) == 2
