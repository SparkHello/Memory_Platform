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
            "用户使用一台枪神笔记本",
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
@pytest.mark.parametrize(
    ("old_content", "new_content"),
    [
        (
            "用户在 Acme 工作多年，担任资深工程师。",
            "用户申请了 Acme 的岗位。",
        ),
        (
            "User lives in Paris and works remotely.",
            "User visited Paris during a holiday.",
        ),
    ],
)
async def test_semantic_cover_does_not_suppress_different_relation(
    memory_store: MemoryStore,
    old_content: str,
    new_content: str,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content=old_content,
        type="semantic",
        importance=7,
        topics=["经历"],
        entities=["Acme" if "Acme" in old_content else "Paris"],
        embedding_json=json.dumps([1.0, 0.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([1.0, 0.0]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(
            new_content,
            type="semantic",
            topics=["经历"],
            entities=["Acme" if "Acme" in new_content else "Paris"],
        ),
    )

    assert result.action == "create"
    assert result.memory is not None
    assert result.memory.id != old.id
    assert len(memory_store.list_memories(user_id="default")) == 2


@pytest.mark.asyncio
async def test_semantic_cover_still_suppresses_same_relation_in_english(
    memory_store: MemoryStore,
) -> None:
    existing = memory_store.create_memory(
        user_id="default",
        content="User works at Acme as a senior engineer.",
        type="semantic",
        importance=7,
        topics=["employment"],
        entities=["Acme"],
        embedding_json=json.dumps([1.0, 0.0]),
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([1.0, 0.0]),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(
            "User works at Acme.",
            type="semantic",
            topics=["employment"],
            entities=["Acme"],
        ),
    )

    assert result.action == "ignore"
    assert result.memory == existing
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


@pytest.mark.asyncio
async def test_resolver_allows_temporal_value_to_return_after_intermediate_change(
    memory_store: MemoryStore,
) -> None:
    old_a = memory_store.create_memory(
        user_id="default",
        content="User works at Company A.",
        type="semantic",
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )
    company_b = memory_store.create_memory(
        user_id="default",
        content="User works at Company B.",
        type="semantic",
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient(None),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(
            "User works at Company A.",
            type="semantic",
            valid_from="2026-01-01",
            temporal_subject="user",
            temporal_predicate="current_employer",
        ),
    )

    assert result.action == "create"
    assert result.memory is not None
    assert result.memory.id != old_a.id
    company_b_after = memory_store.get_memory(
        memory_id=company_b.id,
        user_id="default",
    )
    assert company_b_after is not None
    assert company_b_after.superseded_by == result.memory.id


@pytest.mark.asyncio
async def test_agent_derived_text_cannot_suppress_direct_user_assertion(
    memory_store: MemoryStore,
) -> None:
    derived = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        origin="agent_derived",
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient(None),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate("用户喜欢黑咖啡。", type="emotional"),
    )

    assert result.action == "create"
    assert result.memory is not None
    assert result.memory.id != derived.id


@pytest.mark.asyncio
async def test_expired_dynamic_memory_cannot_suppress_new_assertion(
    memory_store: MemoryStore,
) -> None:
    expired = memory_store.create_memory(
        user_id="default",
        content="用户住在旧地址。",
        importance=10,
        valid_from="2024-01-01",
        valid_until="2025-01-01",
    )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient(None),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate("用户住在旧地址。", type="semantic"),
    )

    assert result.action == "create"
    assert result.memory is not None
    assert result.memory.id != expired.id


@pytest.mark.asyncio
async def test_resolver_finds_exact_duplicate_below_former_top_200_cutoff(
    memory_store: MemoryStore,
) -> None:
    exact = memory_store.create_memory(
        user_id="default",
        content="用户最早记录但仍有效的精确偏好。",
        importance=1,
    )
    for index in range(200):
        memory_store.create_memory(
            user_id="default",
            content=f"高重要度噪声记忆 {index}",
            importance=10,
        )
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient(None),
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=_candidate(exact.content, type=exact.type),
    )

    assert result.action == "ignore"
    assert result.memory == exact


@pytest.mark.asyncio
async def test_auto_classification_uses_candidate_quote_not_unrelated_turn(
    memory_store: MemoryStore,
) -> None:
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient(None),
    )
    candidate = _candidate(
        "用户喜欢茶。",
        type="emotional",
        source_quote="我喜欢茶。",
    )

    result = await resolver.resolve(
        user_id="default",
        candidate=candidate,
        source_message="我喜欢茶，而且我在 Acme 做项目开发。",
    )

    assert result.memory is not None
    spaces = {
        space.id: space.name
        for space in memory_store.list_memory_spaces(user_id="default")
    }
    memory_space_names = {spaces[space_id] for space_id in result.memory.space_ids}
    assert "个人偏好" in memory_space_names
    assert "工作与项目" not in memory_space_names


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
