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
async def test_resolver_ignores_broader_paraphrase_covered_by_old_memory(
    memory_store: MemoryStore,
) -> None:
    existing = memory_store.create_memory(
        user_id="default",
        content="用户使用的笔记本是华硕枪神9 Plus，配备 RTX 5060 显卡和 32GB 内存。",
        type="semantic",
        importance=6,
        confidence=0.9,
        topics=["硬件配置", "设备", "工具"],
        entities=["华硕枪神9 Plus", "RTX 5060", "枪神9plus"],
        embedding_json=json.dumps([1.0, 0.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([0.71, 0.7042]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(
            "用户拥有一台枪神（ROG 枪神）笔记本电脑",
            type="semantic",
            importance=6,
            topics=["设备", "笔记本电脑", "游戏本"],
            entities=["枪神"],
        ),
    )

    assert result.action == "ignore"
    assert result.relation == "same"
    assert result.memory == existing
    assert "语义等价" in result.reason
    assert len(memory_store.list_memories(user_id="default")) == 1


@pytest.mark.asyncio
async def test_resolver_does_not_hide_new_plan_with_same_device_entity(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户使用的笔记本是华硕枪神9 Plus，配备 RTX 5060 显卡和 32GB 内存。",
        type="semantic",
        importance=6,
        topics=["硬件配置", "设备"],
        entities=["华硕枪神9 Plus", "枪神9plus"],
        embedding_json=json.dumps([1.0, 0.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([0.71, 0.7042]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(
            "用户计划购买一台新的枪神笔记本电脑。",
            type="semantic",
            topics=["购买计划", "设备"],
            entities=["枪神"],
        ),
    )

    assert result.action == "create"
    assert result.memory is not None
    assert result.memory.id != old.id
    assert len(memory_store.list_memories(user_id="default")) == 2


@pytest.mark.asyncio
async def test_resolver_does_not_hide_candidate_with_uncovered_entity(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户使用的笔记本是华硕枪神9 Plus，配备 RTX 5060 显卡和 32GB 内存。",
        type="semantic",
        importance=6,
        topics=["硬件配置", "设备"],
        entities=["华硕枪神9 Plus", "枪神9plus"],
        embedding_json=json.dumps([1.0, 0.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([0.71, 0.7042]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(
            "用户拥有枪神笔记本电脑和 iPhone 手机。",
            type="semantic",
            topics=["设备"],
            entities=["枪神", "iPhone"],
        ),
    )

    assert result.action == "create"
    assert result.memory is not None
    assert result.memory.id != old.id
    assert len(memory_store.list_memories(user_id="default")) == 2


@pytest.mark.asyncio
async def test_resolver_does_not_hide_new_structured_device_detail(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户拥有一台枪神笔记本电脑，并经常用它玩游戏。",
        type="semantic",
        importance=6,
        topics=["设备", "游戏"],
        entities=["枪神"],
        embedding_json=json.dumps([1.0, 0.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([0.71, 0.7042]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(
            "用户的枪神笔记本配备 RTX 5090 显卡。",
            type="semantic",
            topics=["设备", "硬件配置"],
            entities=["枪神", "RTX 5090"],
        ),
    )

    assert result.action == "create"
    assert result.memory is not None
    assert result.memory.id != old.id
    assert len(memory_store.list_memories(user_id="default")) == 2


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
