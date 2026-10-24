"""Vector recall matches the historical cosine ranking and survives migration."""

from __future__ import annotations

import json
import math
import random
import sqlite3

import pytest

from app.memory.search import EMBEDDING_MIN_COSINE, MemorySearchService
from app.memory.store import MemoryStore
from app.memory.utils import parse_embedding_vector
from app.memory.vector_index import clear_vector_index_cache, max_cosines_by_id
from app.vector_util import (
    batch_max_cosine,
    try_cosine_similarity,
    unit_vector,
    unpack_unit_vector,
)


class _VectorClient:
    def __init__(self, vector: list[float], space_id: str = "space-a") -> None:
        self.vector = vector
        self.embedding_space_id = space_id

    async def embed(self, text: str) -> list[float]:
        return list(self.vector)


def _legacy_max_cosine(
    raw_json: str | None,
    queries: list[list[float]],
) -> float | None:
    vector = parse_embedding_vector(raw_json)
    if vector is None or not queries:
        return None
    scores = [try_cosine_similarity(query, vector) for query in queries]
    comparable = [score for score in scores if score is not None]
    if not comparable:
        return None
    return max(comparable)


def test_unit_dot_matches_legacy_cosine_within_float_tolerance() -> None:
    rng = random.Random(7)
    queries = [[rng.uniform(-2.0, 2.0) for _ in range(32)] for _ in range(3)]
    rows = [[rng.uniform(-5.0, 5.0) for _ in range(32)] for _ in range(40)]
    rows.append([0.0] * 32)
    matrix = []
    expected: list[float] = []
    for row in rows:
        unit = unit_vector(row)
        if unit is None:
            continue
        matrix.append(unit)
        expected.append(
            max(
                score
                for score in (try_cosine_similarity(query, row) for query in queries)
                if score is not None
            )
        )
    numpy = pytest.importorskip("numpy")
    actual = batch_max_cosine(queries, numpy.asarray(matrix, dtype="float64"))
    assert len(actual) == len(expected)
    for left, right in zip(actual, expected, strict=True):
        assert left == pytest.approx(right, abs=1e-9)


def test_batch_dot_matches_legacy_without_numpy(monkeypatch) -> None:
    monkeypatch.setattr("app.vector_util._numpy", lambda: None)
    query = [2.0, 0.0]
    matrix = [[1.0, 0.0], [0.0, 1.0], unit_vector([3.0, 4.0])]
    actual = batch_max_cosine([query], matrix)
    assert actual[0] == pytest.approx(1.0)
    assert actual[1] == pytest.approx(0.0)
    assert actual[2] == pytest.approx(0.6)


def test_create_and_reembed_store_unit_blob_without_rewriting_json(
    memory_store: MemoryStore,
) -> None:
    raw = json.dumps([3.0, 4.0])
    memory = memory_store.create_memory(
        user_id="default",
        content="用户养了一只猫。",
        embedding_json=raw,
        embedding_space_id="space-a",
    )
    assert memory.embedding_json == raw
    with memory_store._connect() as connection:
        row = connection.execute(
            "SELECT embedding_json, embedding_unit FROM memories WHERE id = ?",
            (memory.id,),
        ).fetchone()
    assert row["embedding_json"] == raw
    assert unpack_unit_vector(row["embedding_unit"]) == pytest.approx([0.6, 0.8])

    updated = json.dumps([0.0, 5.0])
    assert memory_store.update_memory_embedding(
        memory_id=memory.id,
        user_id="default",
        embedding_json=updated,
        embedding_space_id="space-a",
    )
    with memory_store._connect() as connection:
        row = connection.execute(
            "SELECT embedding_json, embedding_unit FROM memories WHERE id = ?",
            (memory.id,),
        ).fetchone()
        epoch = connection.execute(
            "SELECT epoch FROM memory_vector_epoch WHERE user_id = 'default'"
        ).fetchone()[0]
    assert row["embedding_json"] == updated
    assert unpack_unit_vector(row["embedding_unit"]) == pytest.approx([0.0, 1.0])
    assert int(epoch) >= 2

    before = int(epoch)
    memory_store.mark_memories_used(memory_ids=[memory.id], user_id="default")
    with memory_store._connect() as connection:
        after = connection.execute(
            "SELECT epoch FROM memory_vector_epoch WHERE user_id = 'default'"
        ).fetchone()[0]
    assert int(after) == before


def test_v7_database_backfills_unit_vectors(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    store = MemoryStore(str(db_path))
    store.init_db()
    with store._connect() as connection:
        connection.execute("DROP TRIGGER IF EXISTS memories_vector_epoch_insert")
        connection.execute("DROP TRIGGER IF EXISTS memories_vector_epoch_delete")
        connection.execute("DROP TRIGGER IF EXISTS memories_vector_epoch_update")
        connection.execute("ALTER TABLE memories DROP COLUMN embedding_unit")
        connection.execute("DROP TABLE memory_vector_epoch")
        connection.execute(
            """
            INSERT INTO memories (
                id, user_id, content, type, importance, confidence,
                origin, embedding_json, embedding_space_id,
                created_at, updated_at, archived, status
            ) VALUES (
                'legacy', 'default', '旧向量', 'semantic', 3, 0.7,
                'user_asserted', ?, 'space-a',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 0, 'dynamic'
            )
            """,
            (json.dumps([3.0, 4.0]),),
        )
        connection.execute("PRAGMA user_version = 7")

    store.init_db()
    with store._connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        row = connection.execute(
            "SELECT embedding_json, embedding_unit FROM memories WHERE id = 'legacy'"
        ).fetchone()
        triggers = {
            str(item[0])
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    assert int(version) == 8
    assert row["embedding_json"] == json.dumps([3.0, 4.0])
    assert unpack_unit_vector(row["embedding_unit"]) == pytest.approx([0.6, 0.8])
    assert {
        "memories_vector_epoch_insert",
        "memories_vector_epoch_delete",
        "memories_vector_epoch_update",
    } <= triggers


@pytest.mark.asyncio
async def test_search_matches_legacy_cosine_order_and_filters(
    memory_store: MemoryStore,
) -> None:
    rng = random.Random(11)
    query = [1.0, 0.0, 0.0]
    created: list[tuple[str, str, str]] = []
    specs: list[tuple[str, list[float], dict]] = [
        ("near", [0.98, 0.1, 0.0], {"user_id": "default", "embedding_space_id": "space-a"}),
        ("mid", [0.75, 0.4, 0.0], {"user_id": "default", "embedding_space_id": "space-a"}),
        ("low", [0.62, 0.5, 0.0], {"user_id": "default", "embedding_space_id": "space-a"}),
        ("far", [0.0, 1.0, 0.0], {"user_id": "default", "embedding_space_id": "space-a"}),
        ("other-space", [1.0, 0.0, 0.0], {"user_id": "default", "embedding_space_id": "space-b"}),
        ("other-user", [1.0, 0.0, 0.0], {"user_id": "someone-else", "embedding_space_id": "space-a"}),
        (
            "derived",
            [1.0, 0.0, 0.0],
            {
                "user_id": "default",
                "embedding_space_id": "space-a",
                "origin": "agent_derived",
            },
        ),
        (
            "sensitive",
            [1.0, 0.0, 0.0],
            {
                "user_id": "default",
                "embedding_space_id": "space-a",
                "sensitivity": "sensitive",
            },
        ),
    ]
    for name, vector, extra in specs:
        # A little noise keeps non-tie scores separated by more than float error.
        jitter = [component + rng.uniform(-0.01, 0.01) for component in vector]
        raw = json.dumps(jitter)
        memory = memory_store.create_memory(
            content=f"memory {name} has no shared query terms",
            embedding_json=raw,
            **extra,
        )
        created.append((name, memory.id, raw))
    archived = memory_store.create_memory(
        user_id="default",
        content="memory archived has no shared query terms",
        embedding_json=json.dumps([1.0, 0.0, 0.0]),
        embedding_space_id="space-a",
    )
    memory_store.archive_memory(memory_id=archived.id, user_id="default")

    clear_vector_index_cache()
    service = MemorySearchService(
        store=memory_store,
        embedding_client=_VectorClient(query),
        enable_cache=False,
    )
    hits = await service.search_hits(
        query="qqqq-unshared-token",
        user_id="default",
        limit=8,
        record_usage=False,
    )
    legacy = []
    for name, memory_id, raw in created:
        if name in {"other-space", "other-user", "derived", "sensitive"}:
            continue
        cosine = _legacy_max_cosine(raw, [query])
        assert cosine is not None
        if cosine >= EMBEDDING_MIN_COSINE:
            legacy.append((cosine * 100.0, memory_id))
    legacy.sort(reverse=True)
    assert [hit.memory.id for hit in hits] == [memory_id for _, memory_id in legacy]
    assert archived.id not in {hit.memory.id for hit in hits}
    for hit, (score, _) in zip(hits, legacy, strict=True):
        assert hit.score_breakdown["semantic_score"] == pytest.approx(score, abs=1e-6)
        stored = memory_store.get_memory(memory_id=hit.memory.id, user_id="default")
        assert stored is not None
        assert hit.memory.embedding_json == stored.embedding_json
        assert hit.memory.embedding_space_id == "space-a"

    # A second search must reuse the matrix and still ignore a replaced vector
    # only after the epoch moves.
    kept = hits[0].memory.id
    memory_store.update_memory_embedding(
        memory_id=kept,
        user_id="default",
        embedding_json=json.dumps([0.0, 1.0, 0.0]),
        embedding_space_id="space-a",
    )
    refreshed = await service.search_hits(
        query="qqqq-unshared-token",
        user_id="default",
        limit=8,
        record_usage=False,
    )
    assert kept not in {hit.memory.id for hit in refreshed}


def test_cached_matrix_is_scoped_to_user_and_space(memory_store: MemoryStore) -> None:
    memory_store.create_memory(
        user_id="alice",
        content="alice",
        embedding_json=json.dumps([1.0, 0.0]),
        embedding_space_id="space-a",
    )
    memory_store.create_memory(
        user_id="bob",
        content="bob",
        embedding_json=json.dumps([0.0, 1.0]),
        embedding_space_id="space-a",
    )
    with memory_store._connect() as connection:
        connection.execute("BEGIN")
        alice = max_cosines_by_id(
            connection,
            database_path=memory_store.database_path,
            user_id="alice",
            embedding_space_id="space-a",
            query_embeddings=[[1.0, 0.0]],
        )
        bob = max_cosines_by_id(
            connection,
            database_path=memory_store.database_path,
            user_id="bob",
            embedding_space_id="space-a",
            query_embeddings=[[1.0, 0.0]],
        )
        other_space = max_cosines_by_id(
            connection,
            database_path=memory_store.database_path,
            user_id="alice",
            embedding_space_id="space-b",
            query_embeddings=[[1.0, 0.0]],
        )
    assert list(alice.values()) == pytest.approx([1.0])
    assert list(bob.values()) == pytest.approx([0.0])
    assert other_space == {}
    assert math.isclose(next(iter(alice.values())), 1.0)


def test_missing_unit_blob_scores_from_embedding_json(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="尚未回填",
        embedding_json=json.dumps([5.0, 0.0]),
        embedding_space_id="space-a",
    )
    with memory_store._connect() as connection:
        connection.execute(
            "UPDATE memories SET embedding_unit = NULL WHERE id = ?",
            (memory.id,),
        )
        scores = max_cosines_by_id(
            connection,
            database_path=memory_store.database_path,
            user_id="default",
            embedding_space_id="space-a",
            query_embeddings=[[1.0, 0.0]],
        )
    assert list(scores.values()) == pytest.approx([1.0])


def test_invalid_embedding_is_not_reparsed_as_a_vector(memory_store: MemoryStore) -> None:
    memory = memory_store.create_memory(
        user_id="default",
        content="坏向量",
        embedding_json="not-json",
        embedding_space_id="space-a",
    )
    with memory_store._connect() as connection:
        blob = connection.execute(
            "SELECT embedding_unit FROM memories WHERE id = ?",
            (memory.id,),
        ).fetchone()[0]
        assert blob == b""
        scores = max_cosines_by_id(
            connection,
            database_path=memory_store.database_path,
            user_id="default",
            embedding_space_id="space-a",
            query_embeddings=[[1.0, 0.0]],
        )
    assert scores == {}


def test_recall_snapshot_omits_embedding_json_until_hydrate(memory_store: MemoryStore) -> None:
    raw = json.dumps([1.0, 0.0])
    memory = memory_store.create_memory(
        user_id="default",
        content="用户喜欢红茶。",
        embedding_json=raw,
        embedding_space_id="space-a",
    )
    with memory_store.memory_recall_snapshot(
        user_id="default",
        omit_embedding_json=True,
    ) as read_pages:
        page = next(read_pages())
        assert page[0].id == memory.id
        assert page[0].embedding_json == ""
        assert page[0].embedding_space_id == "space-a"
        read_pages.hydrate_embeddings(page)
        assert page[0].embedding_json == raw
    with sqlite3.connect(memory_store.database_path) as connection:
        stored = connection.execute(
            "SELECT embedding_json FROM memories WHERE id = ?",
            (memory.id,),
        ).fetchone()[0]
    assert stored == raw
