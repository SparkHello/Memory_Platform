import json

import pytest

from app.memory.models import CandidateMemory
from app.memory.resolver import MemoryResolver
from app.memory.store import MemoryStore


class StaticEmbeddingClient:
    def __init__(self, vector: list[float] | None):
        self.vector = vector

    async def embed(self, text: str) -> list[float] | None:
        return self.vector


@pytest.mark.asyncio
async def test_resolver_ignores_exact_duplicate(memory_store: MemoryStore) -> None:
    existing = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=7,
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient(None),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate("用户喜欢黑咖啡。", type="emotional"),
    )

    assert result.action == "ignore"
    assert result.memory == existing
    assert len(memory_store.list_memories(user_id="default")) == 1


@pytest.mark.asyncio
async def test_resolver_ignores_when_existing_is_more_complete(
    memory_store: MemoryStore,
) -> None:
    existing = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡，不加糖不加奶。",
        type="emotional",
        importance=8,
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient(None),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate("用户喜欢黑咖啡。", type="emotional"),
    )

    assert result.action == "ignore"
    assert result.memory == existing
    assert len(memory_store.list_memories(user_id="default")) == 1


@pytest.mark.asyncio
async def test_resolver_creates_related_memory_without_overwriting_old_timeline(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户曾经在腾讯实习。",
        type="semantic",
        importance=7,
        embedding_json=json.dumps([1.0, 0.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([1.0, 0.0]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate("用户现在在字节跳动做后端开发。", type="semantic"),
    )

    assert result.action == "create"
    assert result.relation in {"supersede", "conflict"}
    assert "暂不自动合并" in result.reason
    assert result.memory is not None
    assert result.memory.id != old.id

    original = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert original is not None
    assert original.content == "用户曾经在腾讯实习。"
    assert len(memory_store.list_memories(user_id="default")) == 2


@pytest.mark.asyncio
async def test_resolver_creates_high_similarity_non_contained_memory(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=7,
        embedding_json=json.dumps([0.0, 1.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([0.0, 1.0]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate("用户喜欢浅烘咖啡豆。", type="emotional"),
    )

    assert result.action == "create"
    assert "暂不自动合并" in result.reason
    assert result.memory is not None
    assert result.memory.id != old.id
    assert len(memory_store.list_memories(user_id="default")) == 2


@pytest.mark.asyncio
async def test_resolver_temporal_candidate_closes_old_fact(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="User works at Company A.",
        type="semantic",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
        embedding_json=json.dumps([1.0, 0.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([1.0, 0.0]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(
            "User works at Company B.",
            type="semantic",
            valid_from="2026-01-01",
            temporal_subject="user",
            temporal_predicate="current_employer",
        ),
    )

    assert result.action == "create"
    assert result.memory is not None
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None
    assert old_after.valid_until == "2026-01-01"
    assert old_after.status == "resolved"
    assert old_after.superseded_by == result.memory.id


def _candidate(content: str, *, type: str = "fact", **overrides) -> CandidateMemory:
    payload = {
        "action": "create",
        "memory": content,
        "type": type,
        "importance": 8,
        "confidence": 0.9,
        "source_quote": content,
    }
    payload.update(overrides)
    return CandidateMemory(**payload)

