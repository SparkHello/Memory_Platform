import json
import sqlite3

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.memory.models import RecentContextTurn
from app.memory.store import MemoryStore
from app.memory.temporal import is_current_temporal_memory


def test_existing_database_gets_default_emotion_columns(tmp_path) -> None:
    db_path = tmp_path / "legacy-memory.db"
    legacy = MemoryStore(str(db_path))
    with legacy._connect() as connection:
        connection.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                content TEXT,
                type TEXT,
                importance INTEGER,
                confidence REAL,
                source_message TEXT,
                source_conversation_id TEXT,
                embedding_json TEXT,
                last_used_at TEXT,
                usage_count INTEGER DEFAULT 0,
                stability TEXT DEFAULT 'stable',
                valid_until TEXT,
                review_after TEXT,
                sensitivity TEXT DEFAULT 'normal',
                evidence_memory_ids_json TEXT,
                created_at TEXT,
                updated_at TEXT,
                archived_at TEXT,
                archived INTEGER DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memories (
                id, user_id, content, type, importance, confidence,
                created_at, updated_at, archived
            )
            VALUES ('legacy-1', 'default', '用户喜欢安静的工作环境。', 'preference', 7, 0.9, 'now', 'now', 0)
            """
        )
        connection.execute(
            """
            INSERT INTO memories (
                id, user_id, content, type, importance, confidence,
                source_message, created_at, updated_at, archived
            )
            VALUES (
                'legacy-digest', 'default', '旧版模型生成的反思。', 'reflective',
                6, 0.8, 'digest_memories:reflection', 'now', 'now', 0
            )
            """
        )

    store = MemoryStore(str(db_path))
    store.init_db()

    memory = store.get_memory(memory_id="legacy-1", user_id="default")
    assert memory is not None
    assert memory.valence == 0.5
    assert memory.arousal == 0.3
    assert memory.origin == "user_asserted"
    legacy_digest = store.get_memory(memory_id="legacy-digest", user_id="default")
    assert legacy_digest is not None
    assert legacy_digest.origin == "agent_derived"


def test_existing_database_gets_default_classification_columns(tmp_path) -> None:
    db_path = tmp_path / "legacy-classification.db"
    legacy = MemoryStore(str(db_path))
    with legacy._connect() as connection:
        connection.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                content TEXT,
                type TEXT,
                importance INTEGER,
                confidence REAL,
                valence REAL DEFAULT 0.5,
                arousal REAL DEFAULT 0.3,
                source_message TEXT,
                source_conversation_id TEXT,
                embedding_json TEXT,
                last_used_at TEXT,
                usage_count INTEGER DEFAULT 0,
                stability TEXT DEFAULT 'stable',
                valid_until TEXT,
                review_after TEXT,
                sensitivity TEXT DEFAULT 'normal',
                evidence_memory_ids_json TEXT,
                created_at TEXT,
                updated_at TEXT,
                archived_at TEXT,
                archived INTEGER DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memories (
                id, user_id, content, type, importance, confidence,
                created_at, updated_at, archived
            )
            VALUES ('legacy-tags', 'default', '用户喜欢安静。', 'preference', 7, 0.9, 'now', 'now', 0)
            """
        )

    store = MemoryStore(str(db_path))
    store.init_db()

    memory = store.get_memory(memory_id="legacy-tags", user_id="default")
    assert memory is not None
    assert memory.topics == []
    assert memory.entities == []
    assert memory.space_ids == []
    assert store.list_memory_spaces(user_id="default") == []


def test_create_list_and_archive_memory(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=3,
        confidence=0.8,
    )

    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    assert memories[0].id == memory.id
    assert memories[0].content == "用户喜欢黑咖啡。"

    assert memory_store.archive_memory(memory_id=memory.id, user_id="default") is True
    assert memory_store.list_memories(user_id="default") == []


def test_memory_classification_fields_and_spaces(memory_store: MemoryStore) -> None:
    work = memory_store.upsert_memory_space(user_id="default", name=" 工作  空间 ")
    reused = memory_store.upsert_memory_space(user_id="default", name="工作 空间")
    private = memory_store.upsert_memory_space(user_id="default", name="私人")
    memory = memory_store.create_memory(
        user_id="default",
        content="用户正在整理记忆空间。",
        topics=[" 分类 ", "分类", "工作流"],
        entities=["Memory Gateway", "memory gateway"],
        space_ids=[work.id],
    )

    assert reused.id == work.id
    assert memory.topics == ["分类", "工作流"]
    assert memory.entities == ["Memory Gateway"]
    assert memory.space_ids == [work.id]

    updated = memory_store.update_memory(
        memory_id=memory.id,
        user_id="default",
        content=memory.content,
        type=memory.type,
        importance=memory.importance,
        confidence=memory.confidence,
        valence=memory.valence,
        arousal=memory.arousal,
        source_message=memory.source_message,
        source_conversation_id=memory.source_conversation_id,
        embedding_json=memory.embedding_json,
        stability=memory.stability,
        valid_until=memory.valid_until,
        review_after=memory.review_after,
        sensitivity=memory.sensitivity,
        evidence_memory_ids=memory.evidence_memory_ids,
        topics=["阶段四"],
        entities=["Memory Gateway", "SQLite"],
    )
    assert updated is not None
    replaced = memory_store.replace_memory_spaces(
        memory_id=memory.id,
        user_id="default",
        space_ids=[private.id],
        create_space_names=["工作 空间"],
    )

    assert replaced is not None
    assert replaced.topics == ["阶段四"]
    assert replaced.entities == ["Memory Gateway", "SQLite"]
    assert replaced.space_ids == [private.id, work.id]

    summaries = memory_store.list_memory_space_summaries(user_id="default")
    counts = {space["id"]: space["active_memory_count"] for space in summaries}
    assert counts[work.id] == 1
    assert counts[private.id] == 1
    assert [item.id for item in memory_store.list_memories_for_space(user_id="default", space_id=work.id)] == [memory.id]


def test_memory_spaces_are_scoped_by_user(memory_store: MemoryStore) -> None:
    default_space = memory_store.upsert_memory_space(user_id="default", name="工作")
    other_space = memory_store.upsert_memory_space(user_id="other", name="工作")
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢本地优先。",
        space_ids=[default_space.id],
    )

    assert default_space.id != other_space.id
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is not None
    assert memory_store.list_memories_for_space(user_id="other", space_id=default_space.id) == []


def test_create_memory_default_type_is_valid(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户正在测试默认记忆类型。",
    )

    assert memory.type == "semantic"
    assert memory.stability == "stable"
    assert memory.valid_until is None
    assert memory.sensitivity == "normal"
    assert memory.origin == "user_asserted"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"content": "用户的银行卡密码是 123456。"}, "sensitive"),
        (
            {
                "content": "用户提供了联系方式。",
                "source_message": "我的邮箱是 user@example.com",
            },
            "private",
        ),
        (
            {
                "content": "用户提供了一项账号资料。",
                "entities": ["银行卡 6222021234567890"],
            },
            "sensitive",
        ),
    ],
)
def test_create_memory_enforces_local_sensitivity_floor(
    memory_store: MemoryStore,
    kwargs: dict,
    expected: str,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        sensitivity="normal",
        **kwargs,
    )

    assert memory.sensitivity == expected


def test_update_memory_enforces_local_sensitivity_floor(
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户提供了一条普通资料。",
    )

    updated = memory_store.update_memory(
        memory_id=memory.id,
        user_id="default",
        content="用户的银行卡密码是 123456。",
        type=memory.type,
        importance=memory.importance,
        confidence=memory.confidence,
        valence=memory.valence,
        arousal=memory.arousal,
        sensitivity="normal",
    )

    assert updated is not None
    assert updated.sensitivity == "sensitive"


def test_apply_memory_digest_rolls_back_every_write_on_failure(
    memory_store: MemoryStore,
    monkeypatch,
) -> None:
    source = memory_store.create_memory(
        user_id="default",
        content="用户正在测试原子化消化。",
    )
    resolved = memory_store.create_memory(
        user_id="default",
        content="用户此前尚未完成原子化消化。",
    )
    original_insert = memory_store._insert_memory_row
    insert_count = 0

    def fail_second_insert(*, connection, memory) -> None:
        nonlocal insert_count
        insert_count += 1
        if insert_count == 2:
            raise RuntimeError("forced second insert failure")
        original_insert(connection=connection, memory=memory)

    monkeypatch.setattr(memory_store, "_insert_memory_row", fail_second_insert)

    with pytest.raises(RuntimeError, match="forced second insert failure"):
        memory_store.apply_memory_digest(
            user_id="default",
            source_ids=[source.id, resolved.id],
            resolved_ids=[resolved.id],
            reflection="派生反思。",
            feel="派生感受。",
        )

    source_after = memory_store.get_memory(memory_id=source.id, user_id="default")
    resolved_after = memory_store.get_memory(memory_id=resolved.id, user_id="default")
    assert source_after is not None
    assert resolved_after is not None
    assert source_after.digested is False
    assert resolved_after.digested is False
    assert resolved_after.status == "dynamic"
    assert all(
        memory.origin != "agent_derived"
        for memory in memory_store.list_memories(user_id="default")
    )


def test_apply_memory_digest_inherits_explicitly_allowed_sensitive_sources(
    memory_store: MemoryStore,
) -> None:
    private = memory_store.create_memory(
        user_id="default",
        content="用户的私密消化来源。",
        sensitivity="private",
    )
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户的敏感消化来源。",
        sensitivity="sensitive",
    )

    with pytest.raises(ValueError, match="missing or inaccessible"):
        memory_store.get_digest_source_memories(
            user_id="default",
            memory_ids=[private.id, sensitive.id],
        )

    created, resolved_count = memory_store.apply_memory_digest(
        user_id="default",
        source_ids=[private.id, sensitive.id],
        resolved_ids=[],
        reflection="基于敏感来源形成的派生反思。",
        include_sensitive=True,
    )

    assert resolved_count == 0
    assert len(created) == 1
    assert created[0].origin == "agent_derived"
    assert created[0].sensitivity == "sensitive"
    assert created[0].evidence_memory_ids == [private.id, sensitive.id]


def test_apply_memory_digest_rejects_replayed_sources(
    memory_store: MemoryStore,
) -> None:
    source = memory_store.create_memory(
        user_id="default",
        content="用户提供了一条只应消化一次的事实。",
    )
    created, _ = memory_store.apply_memory_digest(
        user_id="default",
        source_ids=[source.id],
        resolved_ids=[],
        reflection="第一次派生反思。",
    )

    with pytest.raises(ValueError, match="missing or inaccessible"):
        memory_store.apply_memory_digest(
            user_id="default",
            source_ids=[source.id],
            resolved_ids=[],
            reflection="重放后不应落库的派生反思。",
        )

    derived = [
        memory
        for memory in memory_store.list_memories(user_id="default")
        if memory.origin == "agent_derived"
    ]
    assert [memory.id for memory in derived] == [created[0].id]


def test_apply_memory_digest_upgrades_sensitivity_from_derived_content(
    memory_store: MemoryStore,
) -> None:
    source = memory_store.create_memory(
        user_id="default",
        content="用户提供了一条普通来源。",
    )

    created, _ = memory_store.apply_memory_digest(
        user_id="default",
        source_ids=[source.id],
        resolved_ids=[],
        reflection="派生内容声称银行卡密码是 123456。",
    )

    assert len(created) == 1
    assert created[0].origin == "agent_derived"
    assert created[0].sensitivity == "sensitive"


def test_import_memory_record_preserves_agent_derived_origin(
    memory_store: MemoryStore,
    tmp_path,
) -> None:
    source = memory_store.create_memory(
        user_id="default",
        content="用户提供的来源记忆。",
    )
    derived = memory_store.create_memory(
        user_id="default",
        content="基于来源形成的派生记忆。",
        type="reflective",
        origin="agent_derived",
        evidence_memory_ids=[source.id],
    )
    restored_store = MemoryStore(str(tmp_path / "restored-origin.db"))
    restored_store.init_db()

    action, restored = restored_store.import_memory_record(
        user_id="default",
        data=derived.model_dump(exclude={"embedding_json"}),
    )

    assert action == "created"
    assert restored is not None
    assert restored.origin == "agent_derived"
    assert restored.evidence_memory_ids == [source.id]


def test_import_memory_record_enforces_local_sensitivity_floor(
    memory_store: MemoryStore,
) -> None:
    action, restored = memory_store.import_memory_record(
        user_id="default",
        data={
            "id": "imported-sensitive",
            "content": "用户的银行卡密码是 123456。",
            "sensitivity": "normal",
        },
    )

    assert action == "created"
    assert restored is not None
    assert restored.sensitivity == "sensitive"


@pytest.mark.parametrize(
    "overrides",
    [
        {"temporal_subject": "user"},
        {"valid_from": "2027-01-01", "valid_until": "2026-01-01"},
        {"decay_lambda": -1},
        {"decay_lambda": 11},
    ],
)
def test_import_memory_record_rejects_invalid_temporal_and_decay_metadata(
    memory_store: MemoryStore,
    overrides: dict,
) -> None:
    action, restored = memory_store.import_memory_record(
        user_id="default",
        data={"content": "invalid imported memory", **overrides},
    )

    assert action == "invalid"
    assert restored is None


def test_import_memory_record_never_overwrites_another_users_id(
    memory_store: MemoryStore,
) -> None:
    existing = memory_store.create_memory(
        user_id="other",
        content="Other user's original memory.",
    )
    import_data = {
        **existing.model_dump(exclude={"embedding_json"}),
        "content": "Current user's restored memory.",
    }

    action, restored = memory_store.import_memory_record(
        user_id="default",
        data=import_data,
        overwrite=True,
    )

    assert action == "created"
    assert restored is not None
    assert restored.id != existing.id
    unchanged = memory_store.get_memory(memory_id=existing.id, user_id="other")
    assert unchanged is not None
    assert unchanged.content == existing.content


def test_create_memory_with_validity_and_sensitivity(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户这个月在减少咖啡摄入。",
        type="semantic",
        importance=7,
        confidence=0.9,
        stability="temporary",
        valid_until="2026-07-01",
        sensitivity="private",
    )

    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")

    assert stored is not None
    assert stored.stability == "temporary"
    assert stored.valid_until == "2026-07-01"
    assert stored.sensitivity == "private"


def test_existing_database_gets_temporal_columns(tmp_path) -> None:
    db_path = tmp_path / "legacy-temporal.db"
    legacy = MemoryStore(str(db_path))
    with legacy._connect() as connection:
        connection.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                content TEXT,
                type TEXT,
                importance INTEGER,
                confidence REAL,
                valence REAL DEFAULT 0.5,
                arousal REAL DEFAULT 0.3,
                source_message TEXT,
                source_conversation_id TEXT,
                embedding_json TEXT,
                last_used_at TEXT,
                usage_count REAL DEFAULT 0.0,
                stability TEXT DEFAULT 'stable',
                valid_until TEXT,
                review_after TEXT,
                sensitivity TEXT DEFAULT 'normal',
                evidence_memory_ids_json TEXT,
                topics_json TEXT,
                entities_json TEXT,
                status TEXT DEFAULT 'dynamic',
                digested INTEGER DEFAULT 0,
                decay_lambda REAL,
                created_at TEXT,
                updated_at TEXT,
                archived_at TEXT,
                archived INTEGER DEFAULT 0
            )
            """
        )

    store = MemoryStore(str(db_path))
    store.init_db()

    with store._connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(memories)").fetchall()
        }
    assert {
        "valid_from",
        "temporal_subject",
        "temporal_predicate",
        "supersedes",
        "superseded_by",
    } <= columns


def test_temporal_invalidation_closes_older_fact_and_logs(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User works at Company A.",
        type="semantic",
        valid_from="2025-01-01",
        temporal_subject=" user ",
        temporal_predicate=" current_employer ",
    )

    new = memory_store.create_memory(
        user_id="default",
        content="User works at Company B.",
        type="semantic",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )

    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    new_after = memory_store.get_memory(memory_id=new.id, user_id="default")
    assert old_after is not None
    assert new_after is not None
    assert old_after.valid_until == "2026-01-01"
    assert old_after.status == "resolved"
    assert old_after.superseded_by == new.id
    assert new_after.supersedes == old.id

    logs = memory_store.list_decision_logs(user_id="default")
    payload = json.loads(logs[0].candidate_json)
    assert payload["source"] == "temporal_invalidation"
    assert payload["new_memory_id"] == new.id
    assert payload["superseded_memory_ids"] == [old.id]


@pytest.mark.parametrize(
    "overrides",
    [
        {"temporal_subject": "user"},
        {"valid_from": "2027-01-01", "valid_until": "2026-01-01"},
        {"decay_lambda": -0.01},
        {"decay_lambda": 10.01},
    ],
)
def test_create_memory_rejects_invalid_temporal_and_decay_metadata(
    memory_store: MemoryStore,
    overrides: dict,
) -> None:
    with pytest.raises(ValueError):
        memory_store.create_memory(
            user_id="default",
            content="invalid metadata must not be stored",
            **overrides,
        )
    assert memory_store.list_memories(user_id="default") == []


def test_update_memory_validates_before_writing_and_rebuilds_temporal_chain(
    memory_store: MemoryStore,
) -> None:
    first = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    moved = memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    current = memory_store.create_memory(
        user_id="default",
        content="User lives in City C.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )

    with pytest.raises(ValueError):
        memory_store.update_memory(
            memory_id=moved.id,
            user_id="default",
            content=moved.content,
            type=moved.type,
            importance=moved.importance,
            confidence=moved.confidence,
            valence=moved.valence,
            arousal=moved.arousal,
            temporal_predicate=None,
        )
    unchanged = memory_store.get_memory(memory_id=moved.id, user_id="default")
    assert unchanged is not None
    assert unchanged.temporal_predicate == "current_city"

    updated = memory_store.update_memory(
        memory_id=moved.id,
        user_id="default",
        content=moved.content,
        type=moved.type,
        importance=moved.importance,
        confidence=moved.confidence,
        valence=moved.valence,
        arousal=moved.arousal,
        valid_from="2027-01-01",
    )
    assert updated is not None
    first_after = memory_store.get_memory(memory_id=first.id, user_id="default")
    current_after = memory_store.get_memory(memory_id=current.id, user_id="default")
    assert first_after is not None
    assert current_after is not None
    assert first_after.valid_until == current.valid_from
    assert first_after.superseded_by == current.id
    assert current_after.supersedes == first.id
    assert current_after.valid_until == updated.valid_from
    assert current_after.superseded_by == updated.id
    assert updated.supersedes == current.id


def test_update_memory_reads_and_returns_row_inside_write_transaction(
    memory_store: MemoryStore,
    monkeypatch,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="Original content.",
    )

    def fail_external_read(*args, **kwargs):
        raise AssertionError("update_memory must not read through get_memory")

    monkeypatch.setattr(memory_store, "get_memory", fail_external_read)
    updated = memory_store.update_memory(
        memory_id=memory.id,
        user_id="default",
        content="Updated inside one write transaction.",
        type=memory.type,
        importance=memory.importance,
        confidence=memory.confidence,
        valence=memory.valence,
        arousal=memory.arousal,
    )

    assert updated is not None
    assert updated.content == "Updated inside one write transaction."


def test_future_temporal_replacement_keeps_current_fact_active_until_effective(
    memory_store: MemoryStore,
) -> None:
    now = datetime.now(UTC)
    old = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from=(now - timedelta(days=30)).isoformat(),
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    future_start = (now + timedelta(days=30)).isoformat()
    future = memory_store.create_memory(
        user_id="default",
        content="User will live in City B.",
        valid_from=future_start,
        temporal_subject="user",
        temporal_predicate="current_city",
    )

    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    future_after = memory_store.get_memory(memory_id=future.id, user_id="default")
    assert old_after is not None
    assert future_after is not None
    assert old_after.valid_until == future_start
    assert old_after.status == "dynamic"
    assert old_after.superseded_by == future.id
    assert future_after.supersedes == old.id

    payload = json.loads(memory_store.list_decision_logs(user_id="default")[0].candidate_json)
    assert payload["after"] == [
        {
            "id": old.id,
            "valid_until": future_start,
            "status": "dynamic",
            "superseded_by": future.id,
        }
    ]


def test_backdated_temporal_insert_relinks_predecessor_and_successor(
    memory_store: MemoryStore,
) -> None:
    now = datetime.now(UTC)
    old_start = (now - timedelta(days=60)).isoformat()
    middle_start = (now - timedelta(days=10)).isoformat()
    future_start = (now + timedelta(days=30)).isoformat()
    old = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from=old_start,
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    future = memory_store.create_memory(
        user_id="default",
        content="User will live in City C.",
        valid_from=future_start,
        temporal_subject="user",
        temporal_predicate="current_city",
    )

    middle = memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from=middle_start,
        temporal_subject="user",
        temporal_predicate="current_city",
    )

    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    middle_after = memory_store.get_memory(memory_id=middle.id, user_id="default")
    future_after = memory_store.get_memory(memory_id=future.id, user_id="default")
    assert old_after is not None
    assert middle_after is not None
    assert future_after is not None
    assert old_after.valid_until == middle_start
    assert old_after.superseded_by == middle.id
    assert middle_after.supersedes == old.id
    assert middle_after.valid_until == future_start
    assert middle_after.superseded_by == future.id
    assert middle_after.status == "dynamic"
    assert future_after.supersedes == middle.id

    timeline = memory_store.list_memory_timeline(
        user_id="default",
        subject="user",
        predicate="current_city",
    )
    assert [memory.id for memory in timeline] == [old.id, middle.id, future.id]


def test_list_decision_logs_filters_by_memory_id(memory_store: MemoryStore) -> None:
    old_job = memory_store.create_memory(
        user_id="default",
        content="User works at Company A.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )
    new_job = memory_store.create_memory(
        user_id="default",
        content="User works at Company B.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )
    memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )

    job_logs = memory_store.list_decision_logs(user_id="default", memory_id=new_job.id)
    assert len(job_logs) == 1
    payload = json.loads(job_logs[0].candidate_json)
    assert payload["new_memory_id"] == new_job.id

    superseded_logs = memory_store.list_decision_logs(user_id="default", memory_id=old_job.id)
    assert [log.id for log in superseded_logs] == [log.id for log in job_logs]

    assert memory_store.list_decision_logs(user_id="default", memory_id="missing-id") == []


def test_list_decision_logs_finds_old_exact_reference_beyond_recent_window(
    memory_store: MemoryStore,
) -> None:
    target_id = "old-target-memory"
    target_log = memory_store.create_decision_log(
        user_id="default",
        conversation_id="target-conversation",
        candidate_json=json.dumps({"memory_id": target_id, "note": "target"}),
        decision="update",
        reason="old target log",
    )
    memory_store.create_decision_log(
        user_id="default",
        conversation_id="false-positive",
        candidate_json=json.dumps({"note": target_id}),
        decision="ignore",
        reason="the id is text, not a memory reference",
    )
    for index in range(600):
        memory_store.create_decision_log(
            user_id="default",
            conversation_id=f"noise-{index}",
            candidate_json=json.dumps({"memory_id": f"noise-memory-{index}"}),
            decision="ignore",
            reason="noise",
        )

    logs = memory_store.list_decision_logs(
        user_id="default",
        memory_id=target_id,
        limit=10,
    )

    assert [log.id for log in logs] == [target_log.id]


def test_temporal_invalidation_compares_offset_datetimes_by_instant(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User works at Company A.",
        valid_from="2026-01-01T12:00:00+08:00",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )

    new = memory_store.create_memory(
        user_id="default",
        content="User works at Company B.",
        valid_from="2026-01-01T05:00:00+00:00",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )

    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None
    assert old_after.status == "resolved"
    assert old_after.valid_until == new.valid_from
    assert old_after.superseded_by == new.id
    assert new.supersedes == old.id


def test_temporal_invalidation_preserves_pin_but_closes_its_interval(
    memory_store: MemoryStore,
) -> None:
    other_user = memory_store.create_memory(
        user_id="other",
        content="Other user works at Company A.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )
    pinned = memory_store.create_memory(
        user_id="default",
        content="Pinned employer fact.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )
    memory_store.update_memory_statuses(
        user_id="default",
        memory_ids=[pinned.id],
        status="pinned",
    )
    archived_status = memory_store.create_memory(
        user_id="default",
        content="Lifecycle archived employer fact.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="archived_employer",
    )
    memory_store.update_memory_statuses(
        user_id="default",
        memory_ids=[archived_status.id],
        status="archived",
    )
    soft_deleted = memory_store.create_memory(
        user_id="default",
        content="Deleted employer fact.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="deleted_employer",
    )
    assert memory_store.archive_memory(memory_id=soft_deleted.id, user_id="default")

    replacement = memory_store.create_memory(
        user_id="default",
        content="User works at Company B.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )

    pinned_after = memory_store.get_memory(memory_id=pinned.id, user_id="default")
    assert pinned_after is not None
    assert pinned_after.status == "pinned"
    assert pinned_after.valid_until == replacement.valid_from
    assert pinned_after.superseded_by == replacement.id
    assert memory_store.get_memory(memory_id=archived_status.id, user_id="default").superseded_by is None
    assert memory_store.get_memory(memory_id=other_user.id, user_id="other").superseded_by is None


def test_temporal_timeline_and_restore(memory_store: MemoryStore) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User uses Tool A.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="primary_tool",
    )
    new = memory_store.create_memory(
        user_id="default",
        content="User uses Tool B.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="primary_tool",
    )

    timeline = memory_store.list_memory_timeline(
        user_id="default",
        subject=" user ",
        predicate="primary_tool",
    )
    assert [memory.id for memory in timeline] == [old.id, new.id]

    restored = memory_store.restore_temporal_memory(
        memory_id=old.id,
        user_id="default",
    )
    assert restored is not None
    assert restored.id not in {old.id, new.id}
    assert restored.valid_until is None
    assert restored.status == "dynamic"
    assert restored.superseded_by is None
    assert restored.supersedes == new.id

    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    new_after = memory_store.get_memory(memory_id=new.id, user_id="default")
    assert old_after is not None
    assert new_after is not None
    assert old_after.valid_from == "2025-01-01"
    assert old_after.valid_until == "2026-01-01"
    assert old_after.superseded_by == new.id
    assert new_after.supersedes == old.id
    assert new_after.valid_until == restored.valid_from
    assert new_after.superseded_by == restored.id
    assert sum(
        is_current_temporal_memory(memory)
        for memory in (old_after, new_after, restored)
    ) == 1

    timeline = memory_store.list_memory_timeline(
        user_id="default",
        subject="user",
        predicate="primary_tool",
    )
    assert [memory.id for memory in timeline] == [old.id, new.id, restored.id]

    logs = memory_store.list_decision_logs(user_id="default")
    payload = json.loads(logs[0].candidate_json)
    assert payload["source"] == "temporal_restore"
    assert payload["source_memory_id"] == old.id
    assert payload["restored_memory_id"] == restored.id


def test_soft_deleted_temporal_head_rejoins_chain_after_newer_fact(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    deleted_head = memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    assert memory_store.archive_memory(
        memory_id=deleted_head.id,
        user_id="default",
    )

    latest = memory_store.create_memory(
        user_id="default",
        content="User lives in City C.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    restored = memory_store.restore_memory(
        memory_id=deleted_head.id,
        user_id="default",
    )

    assert restored is not None
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    latest_after = memory_store.get_memory(memory_id=latest.id, user_id="default")
    assert old_after is not None
    assert latest_after is not None
    assert old_after.superseded_by == restored.id
    assert old_after.valid_until == restored.valid_from
    assert restored.supersedes == old.id
    assert restored.superseded_by == latest.id
    assert restored.valid_until == latest.valid_from
    assert restored.status == "resolved"
    assert latest_after.supersedes == restored.id
    assert latest_after.superseded_by is None
    assert latest_after.valid_until is None
    assert sum(
        is_current_temporal_memory(memory)
        for memory in (old_after, restored, latest_after)
    ) == 1


def test_soft_delete_rebuilds_active_temporal_links_without_dangling_ids(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    middle = memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    latest = memory_store.create_memory(
        user_id="default",
        content="User lives in City C.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )

    assert memory_store.archive_memory(memory_id=middle.id, user_id="default")

    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    latest_after = memory_store.get_memory(memory_id=latest.id, user_id="default")
    assert old_after is not None
    assert latest_after is not None
    assert old_after.superseded_by == latest.id
    assert latest_after.supersedes == old.id
    assert middle.id not in {
        old_after.supersedes,
        old_after.superseded_by,
        latest_after.supersedes,
        latest_after.superseded_by,
    }


def test_init_repairs_legacy_active_links_into_recycle_bin(tmp_path) -> None:
    database_path = tmp_path / "legacy-temporal-links.db"
    store = MemoryStore(str(database_path))
    store.init_db()
    old = store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    middle = store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    latest = store.create_memory(
        user_id="default",
        content="User lives in City C.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    assert store.archive_memory(memory_id=middle.id, user_id="default")
    with store._connect() as connection:
        connection.execute(
            "UPDATE memories SET superseded_by = ? WHERE id = ?",
            (middle.id, old.id),
        )
        connection.execute(
            "UPDATE memories SET supersedes = ? WHERE id = ?",
            (middle.id, latest.id),
        )

    reopened = MemoryStore(str(database_path))
    reopened.init_db()

    old_after = reopened.get_memory(memory_id=old.id, user_id="default")
    latest_after = reopened.get_memory(memory_id=latest.id, user_id="default")
    assert old_after is not None
    assert latest_after is not None
    assert old_after.superseded_by == latest.id
    assert latest_after.supersedes == old.id


def test_time_ripple_delta_zero_has_no_neighbor_side_effect(
    memory_store: MemoryStore,
) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="用户在推进记忆网关。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["memory"],
    )
    neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在整理记忆召回体验。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["memory"],
    )

    used_at = memory_store.mark_memories_used(
        memory_ids=[seed.id],
        user_id="default",
        time_ripple_delta=0.0,
        time_ripple_window_hours=48,
    )

    assert used_at is not None
    refreshed_seed = memory_store.get_memory(memory_id=seed.id, user_id="default")
    refreshed_neighbor = memory_store.get_memory(memory_id=neighbor.id, user_id="default")
    assert refreshed_seed is not None
    assert refreshed_neighbor is not None
    assert refreshed_seed.usage_count == 1
    assert refreshed_seed.last_used_at == used_at
    assert refreshed_neighbor.usage_count == 0
    assert refreshed_neighbor.last_used_at is None


def test_time_ripple_activates_same_space_or_topic_within_window(
    memory_store: MemoryStore,
) -> None:
    space = memory_store.upsert_memory_space(user_id="default", name="Work")
    seed = memory_store.create_memory(
        user_id="default",
        content="用户在推进 Kelivo 记忆体验。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["kelivo"],
        space_ids=[space.id],
    )
    topic_neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在梳理长期记忆的召回规则。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["kelivo"],
    )
    space_neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在工作空间记录产品决策。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T10:00:00+00:00",
        topics=["product"],
        space_ids=[space.id],
    )

    used_at = memory_store.mark_memories_used(
        memory_ids=[seed.id],
        user_id="default",
        time_ripple_delta=0.25,
        time_ripple_window_hours=48,
    )

    assert used_at is not None
    refreshed_seed = memory_store.get_memory(memory_id=seed.id, user_id="default")
    refreshed_topic = memory_store.get_memory(memory_id=topic_neighbor.id, user_id="default")
    refreshed_space = memory_store.get_memory(memory_id=space_neighbor.id, user_id="default")
    assert refreshed_seed is not None
    assert refreshed_topic is not None
    assert refreshed_space is not None
    assert refreshed_seed.usage_count == 1
    assert refreshed_topic.usage_count == 0.25
    assert refreshed_topic.last_used_at == used_at
    assert refreshed_space.usage_count == 0.25
    assert refreshed_space.last_used_at == used_at


def test_time_ripple_skips_ineligible_neighbors(memory_store: MemoryStore) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="用户在推进记忆系统。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["memory"],
    )
    other_user = memory_store.create_memory(
        user_id="other",
        content="其他用户也在推进记忆系统。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["memory"],
    )
    outside_window = memory_store.create_memory(
        user_id="default",
        content="用户很久以前整理过记忆系统。",
        type="semantic",
        importance=8,
        valid_from="2026-06-20T09:00:00+00:00",
        topics=["memory"],
    )
    no_shared_tag = memory_store.create_memory(
        user_id="default",
        content="用户喜欢安静的阅读环境。",
        type="emotional",
        importance=8,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["reading"],
    )
    soft_deleted = memory_store.create_memory(
        user_id="default",
        content="用户删除前的记忆系统记录。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["memory"],
    )
    status_archived = memory_store.create_memory(
        user_id="default",
        content="用户归档的记忆系统记录。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["memory"],
    )
    pinned = memory_store.create_memory(
        user_id="default",
        content="用户钉选的记忆系统记录。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["memory"],
    )
    private = memory_store.create_memory(
        user_id="default",
        content="用户的私密记忆系统记录。",
        type="semantic",
        importance=8,
        sensitivity="private",
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["memory"],
    )
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户的敏感记忆系统记录。",
        type="semantic",
        importance=8,
        sensitivity="sensitive",
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["memory"],
    )
    memory_store.archive_memory(memory_id=soft_deleted.id, user_id="default")
    _set_memory_status(memory_store, status_archived.id, "archived")
    _set_memory_status(memory_store, pinned.id, "pinned")

    memory_store.mark_memories_used(
        memory_ids=[seed.id],
        user_id="default",
        time_ripple_delta=0.5,
        time_ripple_window_hours=24,
    )

    assert memory_store.get_memory(memory_id=other_user.id, user_id="other").usage_count == 0
    for memory_id in [
        outside_window.id,
        no_shared_tag.id,
        status_archived.id,
        pinned.id,
        private.id,
        sensitive.id,
    ]:
        memory = memory_store.get_memory(memory_id=memory_id, user_id="default")
        assert memory is not None
        assert memory.usage_count == 0

    with memory_store._connect() as connection:
        row = connection.execute(
            "SELECT usage_count FROM memories WHERE id = ? AND user_id = ?",
            (soft_deleted.id, "default"),
        ).fetchone()
    assert row is not None
    assert row["usage_count"] == 0


def test_time_ripple_deduplicates_neighbor_across_multiple_seeds(
    memory_store: MemoryStore,
) -> None:
    first = memory_store.create_memory(
        user_id="default",
        content="用户在推进 A 计划。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["alpha"],
    )
    second = memory_store.create_memory(
        user_id="default",
        content="用户在推进 B 计划。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:30:00+00:00",
        topics=["beta"],
    )
    neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在整合 A/B 计划。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["alpha", "beta"],
    )

    memory_store.mark_memories_used(
        memory_ids=[first.id, second.id],
        user_id="default",
        time_ripple_delta=0.2,
        time_ripple_window_hours=48,
    )

    refreshed_first = memory_store.get_memory(memory_id=first.id, user_id="default")
    refreshed_second = memory_store.get_memory(memory_id=second.id, user_id="default")
    refreshed_neighbor = memory_store.get_memory(memory_id=neighbor.id, user_id="default")
    assert refreshed_first is not None
    assert refreshed_second is not None
    assert refreshed_neighbor is not None
    assert refreshed_first.usage_count == 1
    assert refreshed_second.usage_count == 1
    assert refreshed_neighbor.usage_count == 0.2


def test_create_memory_with_review_after_and_evidence(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户最近在准备旅行。",
        type="semantic",
        importance=7,
        review_after="2026-07-01",
        evidence_memory_ids=["source-a"],
    )

    stored = memory_store.get_memory(memory_id=memory.id, user_id="default")

    assert stored is not None
    assert stored.review_after == "2026-07-01"
    assert stored.evidence_memory_ids == ["source-a"]


def test_merge_memories_archives_fragments_and_keeps_evidence(memory_store: MemoryStore) -> None:
    first = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=7,
    )
    second = memory_store.create_memory(
        user_id="default",
        content="用户喜欢浅烘咖啡豆。",
        type="emotional",
        importance=6,
        evidence_memory_ids=["older-source"],
    )

    result = memory_store.merge_memories(
        user_id="default",
        memory_ids=[first.id, second.id],
        content="用户喜欢黑咖啡，偏好浅烘咖啡豆。",
    )

    assert result.action == "update"
    assert result.memory is not None
    assert result.memory.id == first.id
    assert result.memory.evidence_memory_ids == [first.id, second.id, "older-source"]
    assert result.archived_memory_ids == [second.id]
    assert memory_store.get_memory(memory_id=second.id, user_id="default") is None
    assert len(memory_store.list_memories(user_id="default")) == 1


def test_merge_memories_rejects_different_temporal_versions(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    current = memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )

    result = memory_store.merge_memories(
        user_id="default",
        memory_ids=[old.id, current.id],
    )

    assert result.action == "ignore"
    assert "不同时间版本" in result.reason
    assert memory_store.get_memory(memory_id=old.id, user_id="default") is not None
    assert memory_store.get_memory(memory_id=current.id, user_id="default") is not None


def test_merge_memories_rolls_back_target_links_and_sources_on_archive_failure(
    memory_store: MemoryStore,
) -> None:
    first_space = memory_store.upsert_memory_space(user_id="default", name="First")
    second_space = memory_store.upsert_memory_space(user_id="default", name="Second")
    first = memory_store.create_memory(
        user_id="default",
        content="First merge fragment.",
        embedding_json="[0.1]",
        space_ids=[first_space.id],
    )
    second = memory_store.create_memory(
        user_id="default",
        content="Second merge fragment.",
        space_ids=[second_space.id],
    )
    with memory_store._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_merge_archive
            BEFORE UPDATE OF archived ON memories
            WHEN NEW.archived = 1
            BEGIN
                SELECT RAISE(ABORT, 'forced merge archive failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced merge archive failure"):
        memory_store.merge_memories(
            user_id="default",
            memory_ids=[first.id, second.id],
            content="Merged content that must roll back.",
        )

    restored_first = memory_store.get_memory(memory_id=first.id, user_id="default")
    restored_second = memory_store.get_memory(memory_id=second.id, user_id="default")
    assert restored_first is not None
    assert restored_second is not None
    assert restored_first.content == first.content
    assert restored_first.embedding_json == "[0.1]"
    assert restored_first.evidence_memory_ids == []
    assert restored_first.space_ids == [first_space.id]
    assert restored_second.space_ids == [second_space.id]
    assert memory_store.list_archived_memories(user_id="default") == []


def test_memory_source_explanation_marks_core_evidence(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢直接、实用的回答。",
        type="emotional",
        confidence=0.9,
        source_message="我喜欢你直接一点",
        source_conversation_id="conv-1",
    )
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="communication",
        content="- 用户喜欢直接、实用的回答。",
        evidence_memory_ids=[memory.id],
        confidence=0.9,
    )

    explanation = memory_store.explain_memory_source(
        memory_id=memory.id,
        user_id="default",
    )

    assert explanation is not None
    assert explanation.source_excerpt == "我喜欢你直接一点"
    assert explanation.source_conversation_id == "conv-1"
    assert explanation.is_core_memory_evidence is True
    assert explanation.core_memory_sections == ["communication"]


def test_core_memory_section_history_is_saved_before_update(
    memory_store: MemoryStore,
) -> None:
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="- 用户喜欢黑咖啡。",
        evidence_memory_ids=["m1"],
        confidence=0.9,
    )
    _, updated = memory_store.upsert_core_memory_section(
        user_id="default",
        section="preferences",
        content="- 用户喜欢黑咖啡，也喜欢浅烘豆。",
        evidence_memory_ids=["m1", "m2"],
        confidence=0.92,
    )

    history = memory_store.list_core_memory_section_history(
        user_id="default",
        section="preferences",
    )

    assert updated.version == 2
    assert len(history) == 1
    assert history[0].version == 1
    assert history[0].content == "- 用户喜欢黑咖啡。"


def test_store_list_limits_do_not_treat_negative_values_as_unbounded(
    memory_store: MemoryStore,
) -> None:
    for index in range(3):
        memory_store.create_decision_log(
            user_id="default",
            conversation_id=None,
            candidate_json=json.dumps({"memory_id": f"memory-{index}"}),
            decision="create",
            reason="limit test",
        )
    for index in range(3):
        memory_store.upsert_core_memory_section(
            user_id="default",
            section="profile",
            content=f"core version {index}",
            evidence_memory_ids=[f"memory-{index}"],
            confidence=0.8,
        )

    assert len(memory_store.list_decision_logs(user_id="default", limit=-1)) == 1
    assert len(
        memory_store.list_core_memory_section_history(
            user_id="default",
            limit=-1,
        )
    ) == 1
    assert len(memory_store.list_decision_logs(user_id="default", limit=None)) == 3
    assert len(
        memory_store.list_core_memory_section_history(
            user_id="default",
            limit=None,
        )
    ) == 2


def test_recent_context_summary_upsert(memory_store: MemoryStore) -> None:
    memory_store.upsert_recent_context_summary(
        user_id="default",
        conversation_id="conv-1",
        summary="用户：聊咖啡",
    )
    memory_store.upsert_recent_context_summary(
        user_id="default",
        conversation_id="conv-1",
        summary="用户：聊咖啡\n助手：推荐早餐",
    )

    summaries = memory_store.list_recent_context_summaries(user_id="default")
    summary = memory_store.get_recent_context_summary(
        user_id="default",
        conversation_id="conv-1",
    )

    assert len(summaries) == 1
    assert summary is not None
    assert summary.summary == "用户：聊咖啡\n助手：推荐早餐"
    assert summary.compressed_summary == summary.summary
    assert summary.recent_turns == []


def test_recent_context_state_round_trip(memory_store: MemoryStore) -> None:
    saved = memory_store.upsert_recent_context_state(
        user_id="default",
        conversation_id="rolling",
        summary="较早摘要\n\n最近两轮",
        compressed_summary="较早摘要",
        recent_turns=[
            RecentContextTurn(
                user="你猜我现在多少岁",
                assistant="我猜 20 岁",
            ),
            RecentContextTurn(
                user="18",
                assistant="原来是 18 岁",
            ),
        ],
        turn_count=7,
    )

    loaded = memory_store.get_recent_context_summary_for_conversation(
        user_id="default",
        conversation_id="rolling",
    )

    assert loaded == saved
    assert loaded is not None
    assert loaded.turn_count == 7
    assert loaded.recent_turns[1].user == "18"


def test_conversation_branch_nodes_keep_sibling_context_snapshots(
    memory_store: MemoryStore,
) -> None:
    common = {
        "user_id": "default",
        "conversation_id": None,
        "parent_history_fingerprint": "a" * 64,
        "turn_fingerprint": "b" * 64,
        "summary": "用户：同一个问题",
        "compressed_summary": "",
        "turn_count": 1,
    }
    first = memory_store.upsert_conversation_branch_node(
        **common,
        history_fingerprint="c" * 64,
        assistant_digest="d" * 64,
        recent_turns=[
            RecentContextTurn(user="同一个问题", assistant="回答 A"),
        ],
    )
    second = memory_store.upsert_conversation_branch_node(
        **common,
        history_fingerprint="e" * 64,
        assistant_digest="f" * 64,
        recent_turns=[
            RecentContextTurn(user="同一个问题", assistant="回答 B"),
        ],
    )

    loaded_first = memory_store.get_conversation_branch_node(
        user_id="default",
        history_fingerprint="c" * 64,
    )
    loaded_second = memory_store.get_conversation_branch_node(
        user_id="default",
        history_fingerprint="e" * 64,
    )

    assert loaded_first == first
    assert loaded_second == second
    assert loaded_first is not None
    assert loaded_second is not None
    assert loaded_first.recent_turns[0].assistant == "回答 A"
    assert loaded_second.recent_turns[0].assistant == "回答 B"
    assert len(
        memory_store.list_conversation_branch_nodes(user_id="default")
    ) == 2
    assert (
        memory_store.get_conversation_branch_node(
            user_id="other",
            history_fingerprint="c" * 64,
        )
        is None
    )


def test_conversation_branch_subtree_is_archived_and_can_be_recreated(
    memory_store: MemoryStore,
) -> None:
    root = memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint="1" * 64,
        parent_history_fingerprint="",
        turn_fingerprint="2" * 64,
        assistant_digest="3" * 64,
        summary="根节点",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="根问题", assistant="根回答")],
        turn_count=1,
    )
    child = memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint="4" * 64,
        parent_history_fingerprint=root.history_fingerprint,
        turn_fingerprint="5" * 64,
        assistant_digest="6" * 64,
        summary="子节点",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="子问题", assistant="子回答")],
        turn_count=2,
    )
    grandchild = memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint="7" * 64,
        parent_history_fingerprint=child.history_fingerprint,
        turn_fingerprint="8" * 64,
        assistant_digest="9" * 64,
        summary="孙节点",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="孙问题", assistant="孙回答")],
        turn_count=3,
    )
    sibling = memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint="a" * 64,
        parent_history_fingerprint=root.history_fingerprint,
        turn_fingerprint="b" * 64,
        assistant_digest="c" * 64,
        summary="兄弟节点",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="兄弟问题", assistant="兄弟回答")],
        turn_count=2,
    )

    assert memory_store.count_conversation_branch_nodes(user_id="default") == 4
    assert (
        memory_store.archive_conversation_branch_subtree(
            node_id=child.id,
            user_id="default",
        )
        == 2
    )
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
        history_fingerprint=sibling.history_fingerprint,
    ) is not None
    assert memory_store.count_conversation_branch_nodes(user_id="default") == 2
    assert memory_store.count_conversation_branch_nodes(
        user_id="default",
        archived=True,
    ) == 2
    assert {
        node.id
        for node in memory_store.list_conversation_branch_nodes(
            user_id="default",
            archived=True,
        )
    } == {child.id, grandchild.id}

    assert (
        memory_store.restore_conversation_branch_subtree(
            node_id=child.id,
            user_id="default",
        )
        == 2
    )
    assert memory_store.count_conversation_branch_nodes(user_id="default") == 4

    assert (
        memory_store.archive_conversation_branch_subtree(
            node_id=child.id,
            user_id="default",
        )
        == 2
    )

    recreated = memory_store.upsert_conversation_branch_node(
        user_id="default",
        conversation_id=None,
        history_fingerprint=child.history_fingerprint,
        parent_history_fingerprint=root.history_fingerprint,
        turn_fingerprint="d" * 64,
        assistant_digest="e" * 64,
        summary="重新建立的子节点",
        compressed_summary="",
        recent_turns=[RecentContextTurn(user="新问题", assistant="新回答")],
        turn_count=2,
    )
    assert recreated.id == child.id
    assert recreated.archived == 0
    assert recreated.summary == "重新建立的子节点"


def test_legacy_recent_context_table_gets_rolling_state_columns(tmp_path) -> None:
    store = MemoryStore(str(tmp_path / "legacy-recent.db"))
    with store._connect() as connection:
        connection.execute(
            """
            CREATE TABLE recent_context_summaries (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                conversation_id TEXT,
                summary TEXT,
                created_at TEXT,
                updated_at TEXT,
                archived INTEGER DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO recent_context_summaries (
                id, user_id, conversation_id, summary,
                created_at, updated_at, archived
            )
            VALUES (
                'legacy-recent', 'default', 'legacy-conv', '旧的滚动原文',
                'now', 'now', 0
            )
            """
        )

    store.init_db()

    loaded = store.get_recent_context_summary_for_conversation(
        user_id="default",
        conversation_id="legacy-conv",
    )
    assert loaded is not None
    assert loaded.summary == "旧的滚动原文"
    assert loaded.compressed_summary == ""
    assert loaded.recent_turns == []
    assert loaded.turn_count == 0


def test_recent_context_default_reads_latest_and_exact_global_is_distinct(
    memory_store: MemoryStore,
) -> None:
    global_summary = memory_store.upsert_recent_context_summary(
        user_id="default",
        conversation_id=None,
        summary="用户：全局摘要",
    )
    conversation_summary = memory_store.upsert_recent_context_summary(
        user_id="default",
        conversation_id="conv-1",
        summary="用户：会话摘要",
    )

    latest = memory_store.get_recent_context_summary(user_id="default")
    exact_global = memory_store.get_recent_context_summary_for_conversation(
        user_id="default",
        conversation_id=None,
    )

    assert latest is not None
    assert latest.id == conversation_summary.id
    assert exact_global is not None
    assert exact_global.id == global_summary.id


def test_legacy_person_and_relationship_memory_types_migrate_to_semantic(memory_store: MemoryStore) -> None:
    person = memory_store.create_memory(
        user_id="default",
        content="用户的朋友小林正在准备考研。",
        type="person",
    )
    relationship = memory_store.create_memory(
        user_id="default",
        content="小林是用户的高中朋友。",
        type="relationship",
    )

    assert person.type == "semantic"
    assert relationship.type == "semantic"


def test_memory_is_scoped_by_user(memory_store: MemoryStore) -> None:
    memory_store.create_memory(
        user_id="user-a",
        content="用户喜欢茶。",
        type="emotional",
    )

    assert len(memory_store.list_memories(user_id="user-a")) == 1
    assert memory_store.list_memories(user_id="user-b") == []


def test_decision_logs_are_scoped_by_user(memory_store: MemoryStore) -> None:
    memory_store.create_decision_log(
        user_id="user-a",
        conversation_id="shared-conversation",
        candidate_json="{}",
        decision="ignore",
        reason="user-a log",
    )
    memory_store.create_decision_log(
        user_id="user-b",
        conversation_id="shared-conversation",
        candidate_json="{}",
        decision="ignore",
        reason="user-b log",
    )

    logs = memory_store.list_decision_logs(
        user_id="user-a",
        conversation_id="shared-conversation",
    )

    assert len(logs) == 1
    assert logs[0].user_id == "user-a"
    assert logs[0].reason == "user-a log"


def test_archived_memory_can_be_listed_and_restored(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee.",
        type="emotional",
    )

    assert memory_store.archive_memory(memory_id=memory.id, user_id="default") is True

    deleted = memory_store.list_archived_memories(user_id="default")
    assert [item.id for item in deleted] == [memory.id]
    assert deleted[0].archived_at is not None
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is None

    restored = memory_store.restore_memory(memory_id=memory.id, user_id="default")

    assert restored is not None
    assert restored.id == memory.id
    assert restored.archived_at is None
    assert memory_store.list_archived_memories(user_id="default") == []


def test_archived_memory_can_be_purged_with_audit(memory_store: MemoryStore) -> None:
    space = memory_store.upsert_memory_space(user_id="default", name="Private")
    memory = memory_store.create_memory(
        user_id="default",
        content="User private deletion target is SECRET-123.",
        type="semantic",
        sensitivity="private",
        source_message="The source also says SECRET-123.",
        space_ids=[space.id],
    )
    assert memory_store.archive_memory(memory_id=memory.id, user_id="default") is True

    result = memory_store.purge_archived_memory(
        memory_id=memory.id,
        user_id="default",
        affected_core_sections=[
            {
                "id": "core-1",
                "section": "profile",
                "content": "Do not copy this into purge audit.",
                "evidence_memory_ids": [memory.id],
                "version": 3,
            }
        ],
        call_source="test",
    )

    assert result is not None
    purged, log = result
    assert purged.id == memory.id
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is None
    assert memory_store.list_archived_memories(user_id="default") == []
    assert memory_store.restore_memory(memory_id=memory.id, user_id="default") is None
    with memory_store._connect() as connection:
        link_count = connection.execute(
            "SELECT COUNT(*) FROM memory_space_links WHERE user_id = ? AND memory_id = ?",
            ("default", memory.id),
        ).fetchone()[0]
    assert link_count == 0

    assert log.decision == "purge"
    audit = json.loads(log.candidate_json)
    assert audit["source"] == "permanent_purge"
    assert audit["memory_id"] == memory.id
    assert audit["content_length"] == len(memory.content)
    assert len(audit["content_sha256"]) == 64
    assert audit["call_source"] == "test"
    assert audit["affected_core_sections"] == [
        {"section": "profile", "id": "core-1", "version": 3}
    ]
    assert "SECRET-123" not in log.candidate_json
    assert "Do not copy" not in log.candidate_json


def test_purge_scrubs_logs_linked_by_memory_ids_and_source_conversation(
    memory_store: MemoryStore,
) -> None:
    old_secret = "OLD-SECRET-DO-NOT-RETAIN"
    memory = memory_store.create_memory(
        user_id="default",
        content=old_secret,
        source_message="old source quote",
        source_conversation_id="conversation-with-secret",
    )
    memory_store.create_decision_log(
        user_id="default",
        conversation_id="conversation-with-secret",
        candidate_json=json.dumps({"memory": old_secret}),
        decision="create",
        reason="created before edit",
    )
    memory_store.create_decision_log(
        user_id="default",
        conversation_id="other-conversation",
        candidate_json=json.dumps({"memory_id": memory.id, "note": old_secret}),
        decision="update",
        reason="explicit single reference",
    )
    memory_store.create_decision_log(
        user_id="default",
        conversation_id="other-conversation",
        candidate_json=json.dumps({"memory_ids": [memory.id], "note": old_secret}),
        decision="update",
        reason="explicit list reference",
    )
    updated = memory_store.update_memory(
        memory_id=memory.id,
        user_id="default",
        content="Edited replacement content.",
        type=memory.type,
        importance=memory.importance,
        confidence=memory.confidence,
        valence=memory.valence,
        arousal=memory.arousal,
        source_message="edited source quote",
        source_conversation_id=memory.source_conversation_id,
    )
    assert updated is not None
    assert memory_store.archive_memory(memory_id=memory.id, user_id="default")

    result = memory_store.purge_archived_memory(
        memory_id=memory.id,
        user_id="default",
    )

    assert result is not None
    logs = memory_store.list_decision_logs(user_id="default", limit=10)
    serialized_logs = json.dumps(
        [log.model_dump() for log in logs],
        ensure_ascii=False,
    )
    assert old_secret not in serialized_logs
    purge_log = next(log for log in logs if log.decision == "purge")
    assert json.loads(purge_log.candidate_json)["scrubbed_artifacts"][
        "decision_logs_scrubbed"
    ] == 3


def test_purge_deletes_transitive_cycle_safe_derived_closure_for_current_user(
    memory_store: MemoryStore,
) -> None:
    root = memory_store.create_memory(
        user_id="default",
        content="Root fact that must be permanently removed.",
        sensitivity="private",
    )
    first = memory_store.create_memory(
        user_id="default",
        content="First derived fact.",
        origin="agent_derived",
        evidence_memory_ids=[root.id],
    )
    second = memory_store.create_memory(
        user_id="default",
        content="Second derived fact.",
        origin="agent_derived",
        evidence_memory_ids=[first.id],
    )
    first_with_cycle = memory_store.update_memory(
        memory_id=first.id,
        user_id="default",
        content=first.content,
        type=first.type,
        importance=first.importance,
        confidence=first.confidence,
        valence=first.valence,
        arousal=first.arousal,
        sensitivity=first.sensitivity,
        evidence_memory_ids=[root.id, second.id],
    )
    assert first_with_cycle is not None
    other_user = memory_store.create_memory(
        user_id="other",
        content="Another user's derived fact must remain.",
        origin="agent_derived",
        evidence_memory_ids=[root.id, first.id],
    )
    _, core = memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="Core content derived only through the transitive child.",
        evidence_memory_ids=[second.id],
        confidence=0.9,
    )
    assert memory_store.archive_memory(memory_id=root.id, user_id="default")

    result = memory_store.purge_archived_memory(
        memory_id=root.id,
        user_id="default",
    )

    assert result is not None
    _, purge_log = result
    assert memory_store.get_memory(memory_id=first.id, user_id="default") is None
    assert memory_store.get_memory(memory_id=second.id, user_id="default") is None
    assert memory_store.get_memory(memory_id=other_user.id, user_id="other") is not None
    assert memory_store.list_core_memory_sections(user_id="default") == []
    audit = json.loads(purge_log.candidate_json)
    assert audit["affected_core_sections"] == [
        {
            "section": "profile",
            "id": core.id,
            "version": 1,
        }
    ]
    assert audit["scrubbed_artifacts"]["dependent_memories_deleted"] == 2
    assert audit["scrubbed_artifacts"]["derived_memories_deleted"] == 2
    assert audit["scrubbed_artifacts"]["core_sections_scrubbed"] == 1


def test_purge_deletes_user_asserted_merge_that_retains_purged_evidence(
    memory_store: MemoryStore,
) -> None:
    primary = memory_store.create_memory(
        user_id="default",
        content="SECRET-A",
        sensitivity="private",
    )
    fragment = memory_store.create_memory(
        user_id="default",
        content="SECRET-B",
        sensitivity="private",
    )
    merged = memory_store.merge_memories(
        user_id="default",
        memory_ids=[primary.id, fragment.id],
        content="SECRET-A and SECRET-B",
    )
    assert merged.memory is not None
    assert merged.memory.origin == "user_asserted"

    result = memory_store.purge_archived_memory(
        memory_id=fragment.id,
        user_id="default",
    )

    assert result is not None
    assert memory_store.get_memory(memory_id=primary.id, user_id="default") is None
    assert memory_store.list_memories(user_id="default", status="all") == []
    audit = json.loads(result[1].candidate_json)
    assert audit["scrubbed_artifacts"]["dependent_memories_deleted"] == 1
    assert audit["scrubbed_artifacts"]["derived_memories_deleted"] == 1


def test_purge_preserves_temporal_references_repaired_at_soft_delete(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    middle = memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    latest = memory_store.create_memory(
        user_id="default",
        content="User lives in City C.",
        valid_from="2026-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    assert memory_store.archive_memory(memory_id=middle.id, user_id="default")

    result = memory_store.purge_archived_memory(
        memory_id=middle.id,
        user_id="default",
    )

    assert result is not None
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    latest_after = memory_store.get_memory(memory_id=latest.id, user_id="default")
    assert old_after is not None
    assert latest_after is not None
    assert old_after.superseded_by == latest.id
    assert latest_after.supersedes == old.id
    audit = json.loads(result[1].candidate_json)
    assert audit["scrubbed_artifacts"]["temporal_references_relinked"] == 0


def test_purge_rejects_active_memory(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="User active memory should remain.",
        type="semantic",
    )

    result = memory_store.purge_archived_memory(
        memory_id=memory.id,
        user_id="default",
    )

    assert result is None
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is not None
    assert [
        log for log in memory_store.list_decision_logs(user_id="default") if log.decision == "purge"
    ] == []


def test_update_memory_embedding(memory_store: MemoryStore) -> None:
    """update_memory_embedding 应更新活跃记忆的 embedding 并更新时间戳。"""
    import json, time as _time

    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡",
        type="emotional",
    )
    assert memory.embedding_json is None

    _time.sleep(0.01)
    new_embedding = json.dumps([0.5, 0.5, 0.5])
    result = memory_store.update_memory_embedding(
        memory_id=memory.id,
        user_id="default",
        embedding_json=new_embedding,
        embedding_space_id="test-space",
    )
    assert result is True

    updated = memory_store.get_memory(memory_id=memory.id, user_id="default")
    assert updated is not None
    assert updated.embedding_json == new_embedding
    assert updated.embedding_space_id == "test-space"
    assert updated.updated_at > memory.updated_at


def test_update_memory_embedding_ignores_archived(memory_store: MemoryStore) -> None:
    """对已归档记忆调用 update_memory_embedding 应返回 False。"""
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡",
        type="emotional",
    )
    memory_store.archive_memory(memory_id=memory.id, user_id="default")

    result = memory_store.update_memory_embedding(
        memory_id=memory.id,
        user_id="default",
        embedding_json=json.dumps([0.5, 0.5, 0.5]),
        embedding_space_id="test-space",
    )
    assert result is False


def test_content_change_clears_embedding_and_space(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡",
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="test-space",
    )

    updated = memory_store.update_memory(
        memory_id=memory.id,
        user_id="default",
        content="用户现在喜欢拿铁",
        type=memory.type,
        importance=memory.importance,
        confidence=memory.confidence,
        valence=memory.valence,
        arousal=memory.arousal,
        source_message=memory.source_message,
        source_conversation_id=memory.source_conversation_id,
        embedding_json=memory.embedding_json,
        embedding_space_id=memory.embedding_space_id,
        stability=memory.stability,
        valid_from=memory.valid_from,
        valid_until=memory.valid_until,
        review_after=memory.review_after,
        sensitivity=memory.sensitivity,
        evidence_memory_ids=memory.evidence_memory_ids,
        topics=memory.topics,
        entities=memory.entities,
        temporal_subject=memory.temporal_subject,
        temporal_predicate=memory.temporal_predicate,
    )

    assert updated is not None
    assert updated.embedding_json is None
    assert updated.embedding_space_id is None


def test_get_active_memory_count(memory_store: MemoryStore) -> None:
    """get_active_memory_count 应正确统计活跃记忆数量。"""
    assert memory_store.get_active_memory_count(user_id="default") == 0

    m1 = memory_store.create_memory(
        user_id="default", content="记忆1", type="semantic",
    )
    assert memory_store.get_active_memory_count(user_id="default") == 1

    m2 = memory_store.create_memory(
        user_id="default", content="记忆2", type="semantic",
    )
    assert memory_store.get_active_memory_count(user_id="default") == 2

    memory_store.archive_memory(memory_id=m1.id, user_id="default")
    assert memory_store.get_active_memory_count(user_id="default") == 1

    memory_store.archive_memory(memory_id=m2.id, user_id="default")
    assert memory_store.get_active_memory_count(user_id="default") == 0


def test_archive_expired_memories(memory_store: MemoryStore) -> None:
    """archive_expired_memories 应归档 valid_until 已过期的记忆，保留未过期和无有效期的。"""
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()

    m1 = memory_store.create_memory(
        user_id="default",
        content="expired memory",
        type="semantic",
        valid_until=past,
    )
    m2 = memory_store.create_memory(
        user_id="default",
        content="future expiry",
        type="semantic",
        valid_until=future,
    )
    m3 = memory_store.create_memory(
        user_id="default",
        content="no expiry",
        type="semantic",
    )

    count = memory_store.archive_expired_memories(user_id="default")
    assert count == 1

    # m1 已归档
    assert memory_store.get_memory(memory_id=m1.id, user_id="default") is None
    # m2,m3 仍活跃
    assert memory_store.get_memory(memory_id=m2.id, user_id="default") is not None
    assert memory_store.get_memory(memory_id=m3.id, user_id="default") is not None

    # 再次调用不应重复归档
    count2 = memory_store.archive_expired_memories(user_id="default")
    assert count2 == 0


def test_archive_expired_memories_preserves_temporal_version_history(
    memory_store: MemoryStore,
) -> None:
    old_version = memory_store.create_memory(
        user_id="default",
        content="User lives in City A.",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    memory_store.create_memory(
        user_id="default",
        content="User lives in City B.",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    temporary = memory_store.create_memory(
        user_id="default",
        content="ordinary temporary fact",
        valid_until=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )

    assert memory_store.archive_expired_memories(user_id="default") == 1
    assert memory_store.get_memory(memory_id=old_version.id, user_id="default") is not None
    assert memory_store.get_memory(memory_id=temporary.id, user_id="default") is None


def test_archive_expired_memories_compares_instants_across_timezones(
    memory_store: MemoryStore,
) -> None:
    past_with_positive_offset = (
        datetime.now(UTC) - timedelta(hours=1)
    ).astimezone(timezone(timedelta(hours=14))).isoformat()
    memory = memory_store.create_memory(
        user_id="default",
        content="expired in an offset timezone",
        valid_until=past_with_positive_offset,
    )

    assert memory_store.archive_expired_memories(user_id="default") == 1
    assert memory_store.get_memory(memory_id=memory.id, user_id="default") is None


@pytest.mark.parametrize("field", ["valid_until", "review_after"])
def test_create_memory_rejects_invalid_datetime_fields(
    memory_store: MemoryStore,
    field: str,
) -> None:
    with pytest.raises(ValueError):
        memory_store.create_memory(
            user_id="default",
            content="invalid datetime",
            **{field: "not-a-date"},
        )


def test_archive_expired_memories_empty_store(memory_store: MemoryStore) -> None:
    """空 store 调用返回 0。"""
    assert memory_store.archive_expired_memories(user_id="default") == 0


def test_archive_expired_memories_user_scoped(memory_store: MemoryStore) -> None:
    """archive_expired 仅作用于指定用户。"""
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    memory_store.create_memory(
        user_id="user-a", content="a expired", type="semantic", valid_until=past,
    )
    memory_store.create_memory(
        user_id="user-b", content="b expired", type="semantic", valid_until=past,
    )

    assert memory_store.archive_expired_memories(user_id="user-a") == 1
    assert memory_store.archive_expired_memories(user_id="user-b") == 1
    assert memory_store.archive_expired_memories(user_id="user-a") == 0


def _set_memory_status(memory_store: MemoryStore, memory_id: str, status: str) -> None:
    now = datetime.now(UTC).isoformat()
    with memory_store._connect() as connection:
        connection.execute(
            """
            UPDATE memories
            SET status = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (status, now, memory_id, "default"),
        )
