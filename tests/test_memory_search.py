from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json

import pytest

from app.memory.search import MemorySearchService, NullEmbeddingClient, SEARCH_CACHE
from app.memory.store import MemoryStore
from app.memory.temporal import (
    memory_matches_temporal_mode,
    temporal_query_mode,
    temporal_query_window,
)


class StaticEmbeddingClient:
    def __init__(self, vector: list[float] | None):
        self.vector = vector
        self.call_count = 0
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float] | None:
        self.call_count += 1
        self.texts.append(text)
        return self.vector


@pytest.mark.asyncio
async def test_keyword_search_fallback_returns_relevant_memory(memory_store: MemoryStore) -> None:
    coffee = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡和爵士乐。",
        type="emotional",
        importance=3,
    )
    memory_store.create_memory(
        user_id="default",
        content="用户住在上海。",
        type="semantic",
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
        type="emotional",
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
async def test_search_hits_applies_time_ripple_when_enabled(
    memory_store: MemoryStore,
) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="用户使用 Kelivo 做 AI 客户端。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["kelivo"],
    )
    neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在整理客户端记忆体验。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["kelivo"],
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        time_ripple_delta=0.25,
        time_ripple_window_hours=48,
    )

    hits = await service.search_hits(query="Kelivo", user_id="default", limit=5)

    assert [hit.memory.id for hit in hits] == [seed.id]
    refreshed_seed = memory_store.get_memory(memory_id=seed.id, user_id="default")
    refreshed_neighbor = memory_store.get_memory(memory_id=neighbor.id, user_id="default")
    assert refreshed_seed is not None
    assert refreshed_neighbor is not None
    assert refreshed_seed.usage_count == 1
    assert refreshed_neighbor.usage_count == 0.25
    assert refreshed_neighbor.last_used_at == refreshed_seed.last_used_at


@pytest.mark.asyncio
async def test_search_record_usage_false_skips_time_ripple(
    memory_store: MemoryStore,
) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="用户使用 Kelivo 做 AI 客户端。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["kelivo"],
    )
    neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在整理客户端记忆体验。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["kelivo"],
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        time_ripple_delta=0.25,
        time_ripple_window_hours=48,
    )

    hits = await service.search_hits(
        query="Kelivo",
        user_id="default",
        limit=5,
        record_usage=False,
    )

    assert [hit.memory.id for hit in hits] == [seed.id]
    refreshed_seed = memory_store.get_memory(memory_id=seed.id, user_id="default")
    refreshed_neighbor = memory_store.get_memory(memory_id=neighbor.id, user_id="default")
    assert refreshed_seed is not None
    assert refreshed_neighbor is not None
    assert refreshed_seed.usage_count == 0
    assert refreshed_neighbor.usage_count == 0
    assert refreshed_neighbor.last_used_at is None


@pytest.mark.asyncio
async def test_cached_search_hits_still_apply_time_ripple(
    memory_store: MemoryStore,
) -> None:
    seed = memory_store.create_memory(
        user_id="default",
        content="用户使用 Kelivo 做 AI 客户端。",
        type="semantic",
        importance=8,
        valid_from="2026-06-17T08:00:00+00:00",
        topics=["kelivo"],
    )
    neighbor = memory_store.create_memory(
        user_id="default",
        content="用户在整理客户端记忆体验。",
        type="semantic",
        importance=7,
        valid_from="2026-06-17T09:00:00+00:00",
        topics=["kelivo"],
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        time_ripple_delta=0.25,
        time_ripple_window_hours=48,
    )

    await service.search_hits(query="Kelivo", user_id="default", limit=5)
    await service.search_hits(query="Kelivo", user_id="default", limit=5)

    refreshed_seed = memory_store.get_memory(memory_id=seed.id, user_id="default")
    refreshed_neighbor = memory_store.get_memory(memory_id=neighbor.id, user_id="default")
    assert refreshed_seed is not None
    assert refreshed_neighbor is not None
    assert refreshed_seed.usage_count == 2
    assert refreshed_neighbor.usage_count == 0.5


@pytest.mark.asyncio
async def test_cached_candidates_are_reranked_after_activation_changes(
    memory_store: MemoryStore,
) -> None:
    initially_top = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=8,
    )
    later_active = memory_store.create_memory(
        user_id="default",
        content="用户也喜欢咖啡。",
        type="emotional",
        importance=7,
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
    )

    first = await service.search(
        query="咖啡",
        user_id="default",
        limit=1,
        record_usage=False,
    )
    for _ in range(20):
        memory_store.mark_memories_used(
            memory_ids=[later_active.id],
            user_id="default",
        )
    second = await service.search(
        query="咖啡",
        user_id="default",
        limit=1,
        record_usage=False,
    )

    assert [memory.id for memory in first] == [initially_top.id]
    assert [memory.id for memory in second] == [later_active.id]
    assert service.last_cache_status == "hit"


@pytest.mark.asyncio
async def test_cached_result_rechecks_local_sensitivity_before_returning(
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        importance=8,
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
    )
    assert await service.search(
        query="咖啡",
        user_id="default",
        record_usage=False,
    )

    # Keep the cache generation deliberately unchanged to exercise the
    # defense-in-depth row eligibility check, as in a concurrent update window.
    with memory_store._connect() as connection:
        connection.execute(
            "UPDATE memories SET sensitivity = 'sensitive' WHERE id = ?",
            (memory.id,),
        )

    assert await service.search(
        query="咖啡",
        user_id="default",
        record_usage=False,
    ) == []


def test_concurrent_expired_cache_reads_do_not_raise(memory_store: MemoryStore) -> None:
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
    )
    key = ("default", "咖啡", 8, False)
    SEARCH_CACHE[key] = (0.0, "unused", 0, [])

    def read(_: int):
        return service._cached_search_hits(
            key,
            user_id="default",
            query=str(key[1]),
            now=1.0,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(read, range(100)))

    assert results == [None] * 100


@pytest.mark.asyncio
async def test_search_hits_merges_embedding_and_keyword_channels_once(
    memory_store: MemoryStore,
) -> None:
    coffee = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡和爵士乐。",
        type="emotional",
        importance=8,
        embedding_json=json.dumps([1.0, 0.0]),
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([1.0, 0.0]),
    )

    hits = await service.search_hits(query="咖啡", user_id="default", limit=5)

    assert len([hit for hit in hits if hit.memory.id == coffee.id]) == 1
    coffee_hit = next(hit for hit in hits if hit.memory.id == coffee.id)
    assert coffee_hit.channels == ["embedding", "keyword"]
    assert coffee_hit.relevance > 0
    assert coffee_hit.score_breakdown["semantic_score"] > 0
    assert coffee_hit.score_breakdown["keyword_score"] > 0
    assert coffee_hit.score_breakdown["importance_score"] == 80.0
    assert coffee_hit.score_breakdown["final_score"] == coffee_hit.relevance
    assert coffee_hit.activation_count == 1
    assert coffee_hit.last_active_at is not None

    refreshed = memory_store.get_memory(memory_id=coffee.id, user_id="default")
    assert refreshed is not None
    assert refreshed.usage_count == 1


@pytest.mark.asyncio
async def test_search_hits_combines_embedding_only_and_keyword_only_memories(
    memory_store: MemoryStore,
) -> None:
    semantic = memory_store.create_memory(
        user_id="default",
        content="用户正在准备一次越野跑。",
        type="semantic",
        importance=5,
        embedding_json=json.dumps([1.0, 0.0]),
    )
    keyword = memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        type="emotional",
        importance=5,
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([1.0, 0.0]),
    )

    hits = await service.search_hits(
        query="咖啡",
        user_id="default",
        limit=5,
        record_usage=False,
    )
    by_id = {hit.memory.id: hit for hit in hits}

    assert semantic.id in by_id
    assert keyword.id in by_id
    assert by_id[semantic.id].channels == ["embedding"]
    assert by_id[keyword.id].channels == ["keyword"]


@pytest.mark.asyncio
async def test_search_hit_breakdown_survives_search_cache(
    memory_store: MemoryStore,
) -> None:
    memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡和爵士乐。",
        type="emotional",
        importance=8,
        embedding_json=json.dumps([1.0, 0.0]),
    )
    embedding_client = StaticEmbeddingClient([1.0, 0.0])
    service = MemorySearchService(
        store=memory_store,
        embedding_client=embedding_client,
    )

    first_hits = await service.search_hits(
        query="咖啡",
        user_id="default",
        limit=5,
        record_usage=False,
    )
    cached_hits = await service.search_hits(
        query="咖啡",
        user_id="default",
        limit=5,
        record_usage=False,
    )

    assert embedding_client.call_count == 1
    assert first_hits[0].score_breakdown["semantic_score"] > 0
    assert cached_hits[0].score_breakdown["semantic_score"] > 0
    assert cached_hits[0].score_breakdown["keyword_score"] > 0
    assert cached_hits[0].score_breakdown["final_score"] == cached_hits[0].relevance


def test_surface_memories_empty_store(memory_store: MemoryStore) -> None:
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    assert service.surface_memories(user_id="default") == []


def test_surface_memories_promotes_high_importance_unused(
    memory_store: MemoryStore,
) -> None:
    active = memory_store.create_memory(
        user_id="default",
        content="用户常用 Python。",
        type="semantic",
        importance=6,
    )
    cold = memory_store.create_memory(
        user_id="default",
        content="用户正在开发 My_Memory。",
        type="semantic",
        importance=9,
    )
    memory_store.mark_memories_used(memory_ids=[active.id], user_id="default")
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    surfaced = service.surface_memories(user_id="default", limit=2)

    assert surfaced[0].memory.id == cold.id
    assert surfaced[0].surface_reason == "fresh_high_importance"
    assert surfaced[0].activation_count == 0
    assert surfaced[0].final_score > 0
    assert surfaced[0].surface_score == surfaced[0].final_score
    assert surfaced[0].surface_mode == "balanced"
    assert surfaced[0].surface_reason_text
    assert surfaced[0].life_score > 0
    assert surfaced[0].days_since_last_active >= 0
    assert surfaced[0].review_signals == []
    assert surfaced[0].freshness_bonus >= 1.0


def test_surface_memories_supports_modes(memory_store: MemoryStore) -> None:
    important = memory_store.create_memory(
        user_id="default",
        content="用户非常重视季度产品路线图。",
        type="semantic",
        importance=10,
        confidence=0.95,
        arousal=0.2,
    )
    emotional = memory_store.create_memory(
        user_id="default",
        content="用户对一次演示复盘非常紧张。",
        type="semantic",
        importance=4,
        confidence=0.9,
        arousal=0.95,
        valence=0.2,
    )
    stale = memory_store.create_memory(
        user_id="default",
        content="用户长期关注一个旧研究方向。",
        type="procedural",
        importance=7,
    )
    due = memory_store.create_memory(
        user_id="default",
        content="用户最近可能在试用一个临时工具。",
        type="semantic",
        importance=5,
        review_after=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )
    old_time = (datetime.now(UTC) - timedelta(days=140)).isoformat()
    _set_memory_times(memory_store, stale.id, old_time, old_time)

    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    assert service.surface_memories(user_id="default", mode="important", limit=4)[0].memory.id == important.id
    assert service.surface_memories(user_id="default", mode="emotional", limit=4)[0].memory.id == emotional.id

    stale_hits = service.surface_memories(user_id="default", mode="stale", limit=4)
    assert [hit.memory.id for hit in stale_hits] == [stale.id]
    assert stale_hits[0].surface_reason == "stale_important"
    assert "stale" in stale_hits[0].review_signals

    review_hits = service.surface_memories(user_id="default", mode="review_due", limit=4)
    assert review_hits[0].memory.id == due.id
    assert review_hits[0].surface_mode == "review_due"
    assert "review_due" in review_hits[0].review_signals


def test_surface_memories_sorts_by_final_score(memory_store: MemoryStore) -> None:
    for index in range(4):
        memory_store.create_memory(
            user_id="default",
            content=f"用户的排序测试记忆 {index}",
            type="semantic",
            importance=5 + index,
        )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    surfaced = service.surface_memories(user_id="default", limit=4)

    scores = [hit.final_score for hit in surfaced]
    assert scores == sorted(scores, reverse=True)


def test_surface_memories_excludes_archived_and_caps_limit(
    memory_store: MemoryStore,
) -> None:
    archived = memory_store.create_memory(
        user_id="default",
        content="用户曾经使用旧工具。",
        type="semantic",
        importance=10,
    )
    memory_store.archive_memory(memory_id=archived.id, user_id="default")
    for index in range(25):
        memory_store.create_memory(
            user_id="default",
            content=f"用户的测试记忆 {index}",
            type="semantic",
            importance=5,
        )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    surfaced = service.surface_memories(user_id="default", limit=99)

    assert len(surfaced) == 20
    assert archived.id not in {hit.memory.id for hit in surfaced}


@pytest.mark.asyncio
async def test_old_situational_memory_decays_in_ranking(memory_store: MemoryStore) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户计划练习跑步。",
        type="semantic",
        importance=8,
    )
    recent = memory_store.create_memory(
        user_id="default",
        content="用户正在练习跑步。",
        type="semantic",
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
async def test_default_search_excludes_expired_temporal_memory(
    memory_store: MemoryStore,
) -> None:
    expired = memory_store.create_memory(
        user_id="default",
        content="用户最近在减少咖啡摄入。",
        type="semantic",
        importance=10,
        stability="temporary",
        valid_until=(datetime.now(UTC) - timedelta(days=1)).date().isoformat(),
    )
    stable = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=3,
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        enable_cache=False,
    )

    results = await service.search(
        query="咖啡",
        user_id="default",
        limit=2,
        record_usage=False,
    )

    assert [memory.id for memory in results] == [stable.id]


def test_temporal_intent_prefers_explicit_current_fact_and_supports_relative_windows() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)

    assert temporal_query_mode("之前记住的我现在住哪里", now=now) == "current"
    assert temporal_query_mode("以后回答时依据我现在住哪里", now=now) == "current"
    assert temporal_query_mode("用户以前的当前城市是什么", now=now) == "history"
    assert temporal_query_window("我去年住在哪里", now=now) == (
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert temporal_query_window("我前年住在哪里", now=now) == (
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert temporal_query_window("我明年住在哪里", now=now) == (
        datetime(2027, 1, 1, tzinfo=UTC),
        datetime(2028, 1, 1, tzinfo=UTC),
    )
    assert temporal_query_window("我后年住在哪里", now=now) == (
        datetime(2028, 1, 1, tzinfo=UTC),
        datetime(2029, 1, 1, tzinfo=UTC),
    )
    assert temporal_query_window("上个月参加了什么", now=now) == (
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert temporal_query_window("next month plans", now=now) == (
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
    )

    january = datetime(2026, 1, 15, tzinfo=UTC)
    december = datetime(2026, 12, 15, tzinfo=UTC)
    assert temporal_query_window("last month", now=january) == (
        datetime(2025, 12, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert temporal_query_window("下月", now=december) == (
        datetime(2027, 1, 1, tzinfo=UTC),
        datetime(2027, 2, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "query",
    [
        "我什么时候住在北京？",
        "我住过哪些城市？",
        "Where did I live?",
        "Which cities have I lived in?",
    ],
)
def test_temporal_intent_understands_past_tense_questions(query: str) -> None:
    assert temporal_query_mode(query) == "history"


def test_timestamped_point_event_remains_eligible_in_history_mode(
    memory_store: MemoryStore,
) -> None:
    point = memory_store.create_memory(
        user_id="default",
        content="用户在 2025 年参加了一次马拉松。",
        type="episodic",
        valid_from="2025-06-01T08:00:00+00:00",
    )
    now = datetime(2026, 8, 1, tzinfo=UTC)
    last_year = temporal_query_window("去年参加了什么", now=now)

    assert memory_matches_temporal_mode(point, mode="history", now=now)
    assert memory_matches_temporal_mode(point, mode="current", now=now)
    assert not memory_matches_temporal_mode(point, mode="future", now=now)
    assert last_year is not None
    assert memory_matches_temporal_mode(
        point,
        mode="history",
        now=now,
        query_window=last_year,
    )


@pytest.mark.asyncio
async def test_search_selects_temporal_version_from_explicit_query_intent(
    memory_store: MemoryStore,
) -> None:
    now = datetime.now(UTC)
    old = memory_store.create_memory(
        user_id="default",
        content="用户的当前城市是旧城。",
        type="semantic",
        importance=8,
        valid_from=(now - timedelta(days=730)).isoformat(),
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    current = memory_store.create_memory(
        user_id="default",
        content="用户的当前城市是新城。",
        type="semantic",
        importance=8,
        valid_from=(now - timedelta(days=365)).isoformat(),
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    future = memory_store.create_memory(
        user_id="default",
        content="用户的当前城市将是未来城。",
        type="semantic",
        importance=8,
        valid_from=(now + timedelta(days=30)).isoformat(),
        temporal_subject="user",
        temporal_predicate="current_city",
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        enable_cache=False,
    )

    current_results = await service.search(
        query="用户的当前城市是什么？",
        user_id="default",
        limit=5,
        record_usage=False,
    )
    history_results = await service.search(
        query="用户以前的当前城市是什么？",
        user_id="default",
        limit=5,
        record_usage=False,
    )
    future_results = await service.search(
        query="用户未来的当前城市是什么？",
        user_id="default",
        limit=5,
        record_usage=False,
    )

    assert [memory.id for memory in current_results] == [current.id]
    assert [memory.id for memory in history_results] == [old.id]
    assert [memory.id for memory in future_results] == [future.id]


@pytest.mark.asyncio
async def test_search_uses_explicit_calendar_window_for_temporal_version(
    memory_store: MemoryStore,
) -> None:
    old = memory_store.create_memory(
        user_id="default",
        content="用户在这段时间的雇主是 Acme。",
        importance=8,
        valid_from="2024-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )
    current = memory_store.create_memory(
        user_id="default",
        content="用户在这段时间的雇主是 Beta。",
        importance=8,
        valid_from="2025-01-01",
        temporal_subject="user",
        temporal_predicate="current_employer",
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        enable_cache=False,
    )

    results = await service.search(
        query="用户在 2024 年的雇主是什么？",
        user_id="default",
        limit=5,
        record_usage=False,
    )

    assert [memory.id for memory in results] == [old.id]
    assert current.id not in {memory.id for memory in results}


@pytest.mark.asyncio
async def test_sensitive_memory_requires_explicit_search_opt_in(memory_store: MemoryStore) -> None:
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户的健康记录里提到咖啡因限制。",
        type="semantic",
        importance=10,
        sensitivity="sensitive",
    )
    normal = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        type="emotional",
        importance=4,
    )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    default_results = await service.search(
        query="咖啡",
        user_id="default",
        limit=2,
        record_usage=False,
    )
    opted_in_results = await service.search(
        query="咖啡",
        user_id="default",
        limit=2,
        record_usage=False,
        include_sensitive=True,
    )

    assert [memory.id for memory in default_results] == [normal.id]
    assert [memory.id for memory in opted_in_results] == [normal.id, sensitive.id]


@pytest.mark.asyncio
async def test_legacy_mislabeled_sensitive_text_requires_search_opt_in(
    memory_store: MemoryStore,
) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢咖啡。",
        importance=8,
    )
    with memory_store._connect() as connection:
        connection.execute(
            "UPDATE memories SET content = ?, sensitivity = 'normal' WHERE id = ?",
            ("用户的身份证号是 123456789012345678。", memory.id),
        )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
    )

    default_results = await service.search(
        query="身份证号",
        user_id="default",
        record_usage=False,
    )
    opted_in_results = await service.search(
        query="身份证号",
        user_id="default",
        record_usage=False,
        include_sensitive=True,
    )

    assert default_results == []
    assert [result.id for result in opted_in_results] == [memory.id]


@pytest.mark.asyncio
async def test_unrelated_chinese_query_abstains(memory_store: MemoryStore) -> None:
    memory_store.create_memory(user_id="default", content="用户喜欢黑咖啡和爵士乐。")
    memory_store.create_memory(user_id="default", content="用户住在上海。")
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    results = await service.search(
        query="量子火箭发动机维护",
        user_id="default",
        record_usage=False,
    )

    assert results == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "用户的年龄",
        "用户的旅游经历",
        "用户喜欢吃什么",
        "用户喜欢的运动",
        "用户的拍照设备",
    ],
)
async def test_common_user_query_wrappers_do_not_create_keyword_hits(
    memory_store: MemoryStore,
    query: str,
) -> None:
    memory_store.create_memory(user_id="default", content="用户喜欢用手机拍猫。")
    memory_store.create_memory(user_id="default", content="用户常住湖南省常德市。")
    memory_store.create_memory(user_id="default", content="十一特别爱吃西瓜。")
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    results = await service.search(
        query=query,
        user_id="default",
        record_usage=False,
    )

    assert results == []


@pytest.mark.asyncio
async def test_keyword_search_keeps_meaningful_terms_after_query_normalization(
    memory_store: MemoryStore,
) -> None:
    kelivo = memory_store.create_memory(
        user_id="default",
        content="用户使用 Kelivo 作为 AI 客户端。",
    )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    results = await service.search(
        query="用户的 AI 客户端是什么",
        user_id="default",
        record_usage=False,
    )

    assert [memory.id for memory in results] == [kelivo.id]


@pytest.mark.asyncio
async def test_keyword_search_uses_auditable_category_expansion(
    memory_store: MemoryStore,
) -> None:
    pets = memory_store.create_memory(
        user_id="default",
        content="用户养了两只猫，分别叫糯米和十一。",
        topics=["宠物", "猫"],
    )
    laptop = memory_store.create_memory(
        user_id="default",
        content="用户使用的笔记本是华硕枪神。",
        topics=["设备", "硬件配置"],
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        enable_cache=False,
    )

    pet_results = await service.search(
        query="用户有什么宠物",
        user_id="default",
        record_usage=False,
    )
    computer_results = await service.search(
        query="用户的电脑是什么",
        user_id="default",
        record_usage=False,
    )

    assert [memory.id for memory in pet_results] == [pets.id]
    assert [memory.id for memory in computer_results] == [laptop.id]


@pytest.mark.asyncio
async def test_fielded_metadata_reranks_the_matching_domain(
    memory_store: MemoryStore,
) -> None:
    photography = memory_store.create_memory(
        user_id="default",
        content="用户喜欢用手机拍猫，经常分享猫的照片。",
        topics=["拍照", "偏好"],
    )
    memory_store.create_memory(
        user_id="default",
        content="用户对 AI 模型的信息分析有强烈风格偏好。",
        topics=["AI分析", "偏好"],
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        enable_cache=False,
    )

    results = await service.search(
        query="用户拍照风格",
        user_id="default",
        limit=1,
        record_usage=False,
    )

    assert [memory.id for memory in results] == [photography.id]


@pytest.mark.asyncio
async def test_relation_gates_keep_metadata_from_answering_a_different_question(
    memory_store: MemoryStore,
) -> None:
    memory_store.create_memory(
        user_id="default",
        content="用户喂西瓜很有分寸，吃完后会控制糖分。",
        topics=["宠物", "饮食"],
    )
    memory_store.create_memory(
        user_id="default",
        content="用户喜欢给猫准备西瓜，十一吃得很开心。",
        topics=["宠物", "饮食"],
    )
    memory_store.create_memory(
        user_id="default",
        content="用户喜欢用手机拍猫，经常分享猫的照片。",
        topics=["拍照", "偏好"],
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        enable_cache=False,
    )

    food = await service.search(
        query="用户喜欢吃什么",
        user_id="default",
        record_usage=False,
    )
    equipment = await service.search(
        query="用户的拍照设备",
        user_id="default",
        record_usage=False,
    )

    assert food == []
    assert equipment == []


@pytest.mark.asyncio
async def test_keyword_search_supports_meaningful_single_cjk_character(
    memory_store: MemoryStore,
) -> None:
    cat = memory_store.create_memory(
        user_id="default",
        content="用户养了两只猫。",
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        enable_cache=False,
    )

    results = await service.search(
        query="猫",
        user_id="default",
        record_usage=False,
    )
    low_information = await service.search(
        query="我",
        user_id="default",
        record_usage=False,
    )

    assert [memory.id for memory in results] == [cat.id]
    assert low_information == []


@pytest.mark.asyncio
async def test_embedding_uses_normalized_query_and_rejects_weak_cosine(
    memory_store: MemoryStore,
) -> None:
    memory_store.create_memory(
        user_id="default",
        content="用户喜欢黑咖啡。",
        embedding_json=json.dumps([0.5, 0.8660254]),
    )
    embedding_client = StaticEmbeddingClient([1.0, 0.0])
    service = MemorySearchService(
        store=memory_store,
        embedding_client=embedding_client,
    )

    results = await service.search(
        query="用户的年龄是什么",
        user_id="default",
        record_usage=False,
    )

    assert embedding_client.texts == ["年龄"]
    assert results == []


@pytest.mark.asyncio
async def test_high_confidence_embedding_can_bridge_synonyms(
    memory_store: MemoryStore,
) -> None:
    cats = memory_store.create_memory(
        user_id="default",
        content="用户养了两只猫。",
        embedding_json=json.dumps([1.0, 0.0]),
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([1.0, 0.0]),
    )

    results = await service.search(
        query="用户有什么宠物",
        user_id="default",
        record_usage=False,
    )

    assert [memory.id for memory in results] == [cats.id]


@pytest.mark.asyncio
async def test_user_subject_query_rejects_pet_subject_embedding(
    memory_store: MemoryStore,
) -> None:
    memory_store.create_memory(
        user_id="default",
        content="十一特别爱吃西瓜。",
        embedding_json=json.dumps([1.0, 0.0]),
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=StaticEmbeddingClient([1.0, 0.0]),
    )

    results = await service.search(
        query="用户喜欢吃什么",
        user_id="default",
        record_usage=False,
    )

    assert results == []


def test_surface_memories_excludes_sensitive_and_derived_by_default(
    memory_store: MemoryStore,
) -> None:
    normal = memory_store.create_memory(user_id="default", content="用户喜欢咖啡。")
    sensitive = memory_store.create_memory(
        user_id="default",
        content="用户有一项健康隐私。",
        sensitivity="sensitive",
    )
    memory_store.create_memory(
        user_id="default",
        content="模型推导出的反思。",
        origin="agent_derived",
        evidence_memory_ids=[normal.id],
    )
    service = MemorySearchService(store=memory_store, embedding_client=NullEmbeddingClient())

    default_ids = [hit.memory.id for hit in service.surface_memories(user_id="default")]
    opted_in_ids = [
        hit.memory.id
        for hit in service.surface_memories(user_id="default", include_sensitive=True)
    ]

    assert default_ids == [normal.id]
    assert set(opted_in_ids) == {normal.id, sensitive.id}


def _set_memory_times(
    memory_store: MemoryStore,
    memory_id: str,
    created_at: str,
    updated_at: str,
) -> None:
    with memory_store._connect() as connection:
        connection.execute(
            """
            UPDATE memories
            SET created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (created_at, updated_at, memory_id),
        )
