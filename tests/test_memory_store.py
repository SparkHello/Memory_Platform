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


def test_memory_is_scoped_by_user(memory_store: MemoryStore) -> None:
    memory_store.create_memory(
        user_id="user-a",
        content="用户喜欢茶。",
        type="preference",
    )

    assert len(memory_store.list_memories(user_id="user-a")) == 1
    assert memory_store.list_memories(user_id="user-b") == []

