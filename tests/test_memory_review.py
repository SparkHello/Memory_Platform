from app.memory.review import MemoryReviewer
from app.memory.store import MemoryStore


def test_review_duplicate_recommends_same_with_newer_content(memory_store: MemoryStore) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee",
        type="preference",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee!",
        type="preference",
        importance=7,
    )
    _set_updated_at(memory_store, older.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(memory_store, newer.id, "2026-01-02T00:00:00+00:00")

    recommendation = _only_recommendation(memory_store)

    assert recommendation.action == "merge"
    assert recommendation.relation == "same"
    assert recommendation.memory_ids == [older.id, newer.id]
    assert recommendation.suggested_content == newer.content


def test_review_contained_memory_recommends_supplement(memory_store: MemoryStore) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="User likes coffee",
        type="preference",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="User likes coffee with oat milk",
        type="preference",
        importance=7,
    )
    _set_updated_at(memory_store, older.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(memory_store, newer.id, "2026-01-02T00:00:00+00:00")

    recommendation = _only_recommendation(memory_store)

    assert recommendation.action == "merge"
    assert recommendation.relation == "supplement"
    assert recommendation.memory_ids == [older.id, newer.id]
    assert recommendation.suggested_content == newer.content


def test_review_high_similarity_recommends_supersede(memory_store: MemoryStore) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee",
        type="preference",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="User likes dark coffee",
        type="preference",
        importance=7,
    )
    _set_updated_at(memory_store, older.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(memory_store, newer.id, "2026-01-02T00:00:00+00:00")

    recommendation = _only_recommendation(memory_store)

    assert recommendation.action == "review"
    assert recommendation.relation == "supersede"
    assert recommendation.memory_ids == [older.id, newer.id]
    assert recommendation.suggested_content == newer.content


def test_review_negation_difference_recommends_conflict(memory_store: MemoryStore) -> None:
    older = memory_store.create_memory(
        user_id="default",
        content="User likes black coffee",
        type="preference",
        importance=7,
    )
    newer = memory_store.create_memory(
        user_id="default",
        content="User does not like black coffee",
        type="preference",
        importance=7,
    )
    _set_updated_at(memory_store, older.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(memory_store, newer.id, "2026-01-02T00:00:00+00:00")

    recommendation = _only_recommendation(memory_store)

    assert recommendation.action == "review"
    assert recommendation.relation == "conflict"
    assert recommendation.memory_ids == [older.id, newer.id]
    assert recommendation.suggested_content == newer.content


def _only_recommendation(memory_store: MemoryStore):
    result = MemoryReviewer(store=memory_store).review(user_id="default")

    assert len(result.recommendations) == 1
    return result.recommendations[0]


def _set_updated_at(memory_store: MemoryStore, memory_id: str, updated_at: str) -> None:
    with memory_store._connect() as connection:
        connection.execute(
            """
            UPDATE memories
            SET updated_at = ?
            WHERE id = ?
            """,
            (updated_at, memory_id),
        )
