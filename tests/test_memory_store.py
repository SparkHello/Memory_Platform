from app.memory.store import MemoryStore


def test_create_list_and_archive_memory(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="preference",
        importance=3,
        confidence=0.8,
    )

    memories = memory_store.list_memories(user_id="default")
    assert len(memories) == 1
    assert memories[0].id == memory.id
    assert memories[0].content == "用户喜欢黑咖啡。"

    assert memory_store.archive_memory(memory_id=memory.id, user_id="default") is True
    assert memory_store.list_memories(user_id="default") == []


def test_create_memory_default_type_is_valid(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户正在测试默认记忆类型。",
    )

    assert memory.type == "fact"
    assert memory.stability == "stable"
    assert memory.valid_until is None
    assert memory.sensitivity == "normal"


def test_create_memory_with_validity_and_sensitivity(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户这个月在减少咖啡摄入。",
        type="fact",
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


def test_create_memory_with_review_after_and_evidence(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户最近在准备旅行。",
        type="fact",
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
        type="preference",
        importance=7,
    )
    second = memory_store.create_memory(
        user_id="default",
        content="用户喜欢浅烘咖啡豆。",
        type="preference",
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


def test_memory_source_explanation_marks_core_evidence(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢直接、实用的回答。",
        type="style",
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


def test_person_and_relationship_memory_types_are_valid(memory_store: MemoryStore) -> None:
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

    assert person.type == "person"
    assert relationship.type == "relationship"


def test_memory_is_scoped_by_user(memory_store: MemoryStore) -> None:
    memory_store.create_memory(
        user_id="user-a",
        content="用户喜欢茶。",
        type="preference",
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
        type="preference",
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
