from datetime import UTC, datetime

import pytest

from app.memory.search import MemorySearchService, NullEmbeddingClient
from app.memory.store import MemoryStore


def _bulk_insert_decoys(
    store: MemoryStore,
    *,
    count: int,
    target_token: str,
) -> str:
    now = datetime.now(UTC).isoformat()
    rows = [
        (
            f"decoy-{index}",
            "default",
            f"Unrelated high importance decoy number {index}.",
            "semantic",
            10,
            0.7,
            0.5,
            0.3,
            "user_asserted",
            0.0,
            "stable",
            "normal",
            "[]",
            "[]",
            "[]",
            "dynamic",
            0,
            now,
            now,
            0,
        )
        for index in range(count)
    ]
    target_id = f"target-after-{count}"
    rows.append(
        (
            target_id,
            "default",
            f"The exact retrieval canary is {target_token}.",
            "semantic",
            1,
            0.7,
            0.5,
            0.3,
            "user_asserted",
            0.0,
            "stable",
            "normal",
            "[]",
            "[]",
            "[]",
            "dynamic",
            0,
            now,
            now,
            0,
        )
    )
    with store._connect() as connection:
        connection.executemany(
            """
            INSERT INTO memories (
                id, user_id, content, type, importance, confidence,
                valence, arousal, origin, usage_count, stability, sensitivity,
                evidence_memory_ids_json, topics_json, entities_json,
                status, digested, created_at, updated_at, archived
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return target_id


@pytest.mark.asyncio
@pytest.mark.parametrize("decoy_count", [10_000, 50_000])
async def test_recall_finds_low_importance_exact_match_beyond_legacy_pool(
    memory_store: MemoryStore,
    decoy_count: int,
) -> None:
    target_token = f"needle{decoy_count}xyz"
    target_id = _bulk_insert_decoys(
        memory_store,
        count=decoy_count,
        target_token=target_token,
    )
    service = MemorySearchService(
        store=memory_store,
        embedding_client=NullEmbeddingClient(),
        enable_cache=False,
    )

    hits = await service.search_hits(
        query=target_token,
        user_id="default",
        limit=1,
        record_usage=False,
    )

    assert [hit.memory.id for hit in hits] == [target_id]
