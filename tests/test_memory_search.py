from datetime import UTC, datetime, timedelta

import pytest

from app.memory.search import MemorySearchService, NullEmbeddingClient
from app.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_keyword_search_fallback_returns_relevant_memory(memory_store: MemoryStore) -> None:
    coffee = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡和爵士乐。",
        type="preference",
        importance=3,
    )
    memory_store.create_memory(
        user_id="default",
        content="用户住在上海。",
        type="fact",
        importance=2,
    )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    results = await service.search(query="咖啡", user_id="default", limit=1)

    assert [memory.id for memory in results] == [coffee.id]
    assert results[0].usage_count == 1
    assert results[0].last_used_at is not None

    refreshed = memory_store.get_memory(memory_id=coffee.id, user_id="default")
    assert refreshed is not None
    assert refreshed.usage_count == 1
    assert refreshed.last_used_at == results[0].last_used_at


@pytest.mark.asyncio
async def test_search_can_skip_usage_tracking(memory_store: MemoryStore) -> None:
    coffee = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡和爵士乐。",
        type="preference",
        importance=3,
    )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    results = await service.search(
        query="咖啡",
        user_id="default",
        limit=1,
        record_usage=False,
    )

    assert [memory.id for memory in results] == [coffee.id]
    refreshed = memory_store.get_memory(memory_id=coffee.id, user_id="default")
    assert refreshed is not None
    assert refreshed.usage_count == 0
    assert refreshed.last_used_at is None


@pytest.mark.asyncio
async def test_old_situational_memory_decays_in_ranking(memory_store: MemoryStore) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户计划练习跑步。",
        type="project",
        importance=8,
    )
    recent = memory_store.create_memory(
        user_id="default",
        content="用户正在练习跑步。",
        type="fact",
        importance=3,
    )
    old_time = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with memory_store._connect() as connection:
        connection.execute(
            """
            UPDATE memories
            SET created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (old_time, old_time, old.id),
        )

    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    results = await service.search(
        query="跑步",
        user_id="default",
        limit=2,
        record_usage=False,
    )

    assert [memory.id for memory in results] == [recent.id, old.id]


@pytest.mark.asyncio
async def test_expired_temporary_memory_is_downranked(memory_store: MemoryStore) -> None:
    expired = memory_store.create_memory(
        user_id="default",
        content="用户最近在减少咖啡摄入。",
        type="fact",
        importance=10,
        stability="temporary",
        valid_until=(datetime.now(UTC) - timedelta(days=1)).date().isoformat(),
    )
    stable = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="preference",
        importance=3,
    )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    results = await service.search(
        query="咖啡",
        user_id="default",
        limit=2,
        record_usage=False,
    )

    assert [memory.id for memory in results] == [stable.id, expired.id]


@pytest.mark.asyncio
async def test_sensitive_memory_is_downranked(memory_store: MemoryStore) -> None:
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户的健康记录里提到咖啡因限制。",
        type="fact",
        importance=10,
        sensitivity="sensitive",
    )
    normal = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="preference",
        importance=4,
    )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    results = await service.search(
        query="咖啡",
        user_id="default",
        limit=2,
        record_usage=False,
    )

    assert [memory.id for memory in results] == [normal.id, sensitive.id]
