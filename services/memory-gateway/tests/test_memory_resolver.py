import asyncio
import json

import pytest

from app.memory.models import CandidateMemory
from app.memory.resolver import MemoryResolver
from app.memory.store import MemoryStore


class StaticEmbeddingClient:
    def __init__(self, vector: list[float] | None):
        self.vector = vector
        self.embedding_space_id = "test-space"

    async def embed(self, text: str) -> list[float] | None:
        return self.vector


class CoordinatedEmbeddingClient:
    def __init__(self) -> None:
        self.embedding_space_id = ""
        self.calls = 0
        self.both_started = asyncio.Event()

    async def embed(self, text: str) -> list[float] | None:
        self.calls += 1
        if self.calls == 2:
            self.both_started.set()
        await self.both_started.wait()
        return None


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
async def test_concurrent_resolvers_atomically_recheck_before_create(
    memory_store: MemoryStore,
) -> None:
    embedding_client = CoordinatedEmbeddingClient()
    resolver = MemoryResolver(
        store=memory_store,
        embedding_client=embedding_client,
    )

    async def resolve_once():
        return await resolver.resolve(
            user_id="default",
            candidate=_candidate("用户偏好并发写入只保存一次。", type="semantic"),
            auto_classify=False,
        )

    first, second = await asyncio.gather(resolve_once(), resolve_once())

    assert sorted([first.action, second.action]) == ["create", "ignore"]
    memories = memory_store.list_memories(user_id="default")
    assert [memory.content for memory in memories] == ["用户偏好并发写入只保存一次。"]


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
        embedding_space_id="test-space",
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
        embedding_space_id="test-space",
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
        embedding_space_id="test-space",
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
        embedding_space_id="test-space",
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
        embedding_space_id="test-space",
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
        embedding_space_id="test-space",
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
        embedding_space_id="test-space",
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
        embedding_space_id="test-space",
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
        embedding_space_id="test-space",
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


# ---------------------------------------------------------------------------
# Automatic supersede: a committed transition closes the matching live fact.
# ---------------------------------------------------------------------------

import sqlite3

from app.memory.temporal import is_current_temporal_memory


def _old_fact(memory_store: MemoryStore, content: str, **overrides):
    payload = {
        "user_id": "default",
        "content": content,
        "type": "semantic",
        "importance": 8,
        "embedding_json": json.dumps([1.0, 0.0]),
        "embedding_space_id": "test-space",
    }
    payload.update(overrides)
    return memory_store.create_memory(**payload)


def _resolver(memory_store: MemoryStore, *, auto_supersede: bool = True, vector=(1.0, 0.0)):
    return MemoryResolver(
        store=memory_store,
        embedding_client=StaticEmbeddingClient(list(vector) if vector is not None else None),
        auto_supersede=auto_supersede,
    )


def _auto_supersede_logs(memory_store: MemoryStore, user_id: str = "default") -> list[dict]:
    return [
        json.loads(log.candidate_json)
        for log in memory_store.list_decision_logs(user_id=user_id)
        if '"auto_supersede"' in log.candidate_json
    ]


@pytest.mark.asyncio
async def test_resolver_auto_supersedes_same_attribute_with_transition_marker(
    memory_store: MemoryStore,
) -> None:
    old = _old_fact(memory_store, "用户平时用 iPhone 手机。")

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户现在改用安卓手机。", type="semantic"),
    )

    assert result.action == "update"
    assert result.relation == "supersede"
    assert result.superseded_memory_id == old.id
    assert result.memory is not None
    assert result.memory.supersedes == old.id
    assert "已自动替换" in result.reason

    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None
    assert old_after.status == "resolved"
    assert old_after.superseded_by == result.memory.id
    assert old_after.valid_until == result.memory.created_at
    assert is_current_temporal_memory(old_after) is False
    assert is_current_temporal_memory(result.memory) is True
    assert len(memory_store.list_memories(user_id="default")) == 2

    logs = _auto_supersede_logs(memory_store)
    assert len(logs) == 1
    assert logs[0]["source"] == "auto_supersede"
    assert logs[0]["target_memory_id"] == old.id
    assert logs[0]["memory_id"] == result.memory.id
    assert logs[0]["relation"] == "supersede"
    assert logs[0]["after"]["status"] == "resolved"


@pytest.mark.asyncio
async def test_resolver_auto_supersede_disabled_keeps_review_path(memory_store: MemoryStore) -> None:
    old = _old_fact(memory_store, "用户平时用 iPhone 手机。")

    result = await _resolver(memory_store, auto_supersede=False).resolve(
        user_id="default",
        candidate=_candidate("用户现在改用安卓手机。", type="semantic"),
    )

    assert result.action == "create"
    assert "暂不自动合并" in result.reason
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None
    assert old_after.superseded_by is None
    assert old_after.status == "dynamic"
    assert _auto_supersede_logs(memory_store) == []


@pytest.mark.asyncio
async def test_resolver_does_not_auto_supersede_without_transition_marker(
    memory_store: MemoryStore,
) -> None:
    _old_fact(memory_store, "用户平时用 iPhone 手机。")

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户平时用安卓手机。", type="semantic"),
    )

    assert result.action == "create"
    assert result.superseded_memory_id is None


@pytest.mark.asyncio
async def test_resolver_does_not_auto_supersede_pure_polarity_flip(memory_store: MemoryStore) -> None:
    old = _old_fact(memory_store, "用户喜欢喝咖啡。", type="emotional")

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户不喜欢喝咖啡。", type="emotional"),
    )

    assert result.action == "create"
    assert result.relation == "conflict"
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None and old_after.superseded_by is None


@pytest.mark.asyncio
async def test_resolver_auto_supersedes_conflict_with_transition_marker(
    memory_store: MemoryStore,
) -> None:
    old = _old_fact(memory_store, "用户喜欢喝咖啡。")

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户现在不再喝咖啡了。", type="semantic"),
    )

    assert result.action == "update"
    assert result.relation == "conflict"
    assert result.superseded_memory_id == old.id


@pytest.mark.asyncio
async def test_resolver_does_not_auto_supersede_additive_preference_without_conflict(
    memory_store: MemoryStore,
) -> None:
    """Liking tea now does not end liking coffee."""
    old = _old_fact(memory_store, "用户喜欢黑咖啡。", topics=["饮食偏好"])

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户现在更喜欢喝茶。", type="semantic", topics=["饮食偏好"]),
    )

    assert result.action == "create"
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None and old_after.superseded_by is None


@pytest.mark.asyncio
async def test_resolver_auto_supersedes_with_shared_topic_when_no_relation_family(
    memory_store: MemoryStore,
) -> None:
    old = _old_fact(memory_store, "用户的猫叫小白。", topics=["宠物"])

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户的猫现在改名叫小黑。", type="semantic", topics=["宠物"]),
        auto_classify=False,
    )

    assert result.action == "update"
    assert result.superseded_memory_id == old.id


@pytest.mark.parametrize(
    "variant",
    ["pinned", "resolved", "agent_derived", "past_marker", "other_type", "other_sensitivity", "episodic"],
)
@pytest.mark.asyncio
async def test_resolver_never_auto_supersedes_ineligible_target(
    memory_store: MemoryStore,
    variant: str,
) -> None:
    content = "用户平时用 iPhone 手机。"
    candidate_type = "semantic"
    overrides: dict = {}
    if variant == "agent_derived":
        overrides["origin"] = "agent_derived"
    elif variant == "past_marker":
        content = "用户曾经平时用 iPhone 手机。"
    elif variant == "other_type":
        overrides["type"] = "emotional"
    elif variant == "other_sensitivity":
        overrides["sensitivity"] = "private"
    elif variant == "episodic":
        overrides["type"] = "episodic"
        candidate_type = "episodic"
    old = _old_fact(memory_store, content, **overrides)
    if variant in {"pinned", "resolved"}:
        with sqlite3.connect(memory_store.database_path) as connection:
            connection.execute(
                "UPDATE memories SET status = ? WHERE id = ?", (variant, old.id)
            )

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户现在改用安卓手机。", type=candidate_type),
    )

    assert result.action == "create", variant
    assert result.superseded_memory_id is None
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None and old_after.superseded_by is None


@pytest.mark.asyncio
async def test_resolver_auto_supersede_requires_embedding_vector(memory_store: MemoryStore) -> None:
    _old_fact(memory_store, "用户平时用 iPhone 手机。")

    result = await _resolver(memory_store, vector=None).resolve(
        user_id="default",
        candidate=_candidate("用户现在改用安卓手机。", type="semantic"),
    )

    assert result.action == "create"
    assert result.superseded_memory_id is None


@pytest.mark.asyncio
async def test_resolver_does_not_auto_supersede_third_party_subject(memory_store: MemoryStore) -> None:
    _old_fact(memory_store, "用户的朋友用 iPhone 手机。")

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户现在改用安卓手机。", type="semantic"),
    )

    assert result.action == "create"


@pytest.mark.asyncio
async def test_resolver_does_not_auto_supersede_future_dated_or_uncommitted_candidate(
    memory_store: MemoryStore,
) -> None:
    old = _old_fact(memory_store, "用户平时用 iPhone 手机。")

    future = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户现在改用安卓手机。", type="semantic", valid_from="2999-01-01"),
    )
    uncommitted = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户可能现在改用安卓手机。", type="semantic"),
    )

    assert future.action == "create"
    assert uncommitted.action == "create"
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None and old_after.superseded_by is None


@pytest.mark.asyncio
async def test_resolver_auto_supersede_english_word_boundaries(memory_store: MemoryStore) -> None:
    cat = _old_fact(memory_store, "User's cat is named Tom.")
    # A different vector keeps the two live facts distinguishable by cosine.
    python = _old_fact(memory_store, "User uses Python.", embedding_json=json.dumps([0.0, 1.0]))

    renamed = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("User's cat is now named Jerry.", type="semantic"),
    )
    knows = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("User knows Python well.", type="semantic"),
    )

    assert renamed.action == "update"
    assert renamed.superseded_memory_id == cat.id
    # "knows" must not fire the "now" marker; and "knows" is not a transition.
    assert knows.action == "create"
    python_after = memory_store.get_memory(memory_id=python.id, user_id="default")
    assert python_after is not None and python_after.superseded_by is None


@pytest.mark.asyncio
async def test_auto_supersede_does_not_cross_users(memory_store: MemoryStore) -> None:
    alice_fact = _old_fact(memory_store, "用户平时用 iPhone 手机。", user_id="alice")

    result = await _resolver(memory_store).resolve(
        user_id="default",
        candidate=_candidate("用户现在改用安卓手机。", type="semantic"),
    )

    assert result.action == "create"
    alice_after = memory_store.get_memory(memory_id=alice_fact.id, user_id="alice")
    assert alice_after is not None and alice_after.superseded_by is None


@pytest.mark.asyncio
async def test_resolver_allows_keyless_value_to_return_after_auto_supersede(
    memory_store: MemoryStore,
) -> None:
    first = _old_fact(memory_store, "用户平时用 iPhone 手机。")
    resolver = _resolver(memory_store)

    second = await resolver.resolve(
        user_id="default",
        candidate=_candidate("用户现在改用安卓手机。", type="semantic"),
    )
    third = await resolver.resolve(
        user_id="default",
        candidate=_candidate("用户现在改用 iPhone 手机。", type="semantic"),
    )

    assert second.action == "update" and second.superseded_memory_id == first.id
    assert third.action == "update"
    assert second.memory is not None and third.memory is not None
    # The historical first value is never re-selected; the live second one is.
    assert third.superseded_memory_id == second.memory.id
    first_after = memory_store.get_memory(memory_id=first.id, user_id="default")
    second_after = memory_store.get_memory(memory_id=second.memory.id, user_id="default")
    assert first_after is not None and first_after.superseded_by == second.memory.id
    assert second_after is not None and second_after.superseded_by == third.memory.id
    current = [
        memory
        for memory in memory_store.list_memories(user_id="default")
        if is_current_temporal_memory(memory)
    ]
    assert [memory.id for memory in current] == [third.memory.id]


class _CoordinatedStaticEmbeddingClient:
    """Two concurrent resolvers both finish embedding before either writes."""

    def __init__(self) -> None:
        self.embedding_space_id = "test-space"
        self.calls = 0
        self.both_started = asyncio.Event()

    async def embed(self, text: str) -> list[float] | None:
        self.calls += 1
        if self.calls == 2:
            self.both_started.set()
        await self.both_started.wait()
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_concurrent_superseding_candidates_close_target_once(memory_store: MemoryStore) -> None:
    old = _old_fact(memory_store, "用户平时用 iPhone 手机。")
    client = _CoordinatedStaticEmbeddingClient()
    left = MemoryResolver(store=memory_store, embedding_client=client)
    right = MemoryResolver(store=memory_store, embedding_client=client)

    # auto_classify=False keeps the pre-existing space-upsert race out of
    # this test; the write lock under test is create_memory's BEGIN IMMEDIATE.
    results = await asyncio.gather(
        left.resolve(
            user_id="default",
            candidate=_candidate("用户现在改用安卓手机。", type="semantic"),
            auto_classify=False,
        ),
        right.resolve(
            user_id="default",
            candidate=_candidate("用户现在改用安卓手机。", type="semantic"),
            auto_classify=False,
        ),
    )

    actions = sorted(result.action for result in results)
    assert actions == ["ignore", "update"]
    assert len(_auto_supersede_logs(memory_store)) == 1
    old_after = memory_store.get_memory(memory_id=old.id, user_id="default")
    assert old_after is not None and old_after.superseded_by is not None
    assert len(memory_store.list_memories(user_id="default")) == 2
