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

