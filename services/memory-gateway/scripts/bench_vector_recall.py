"""Compare legacy per-row cosine with the cached unit-vector dot product.

The legacy loop is the previous recall math: parse every embedding JSON and
compute cosine similarity in pure Python. The new path scores one NumPy
matrix of stored unit vectors and reuses it while the vector epoch is unchanged.

Example:
    python scripts/bench_vector_recall.py --counts 10000,50000 --dim 384
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import random
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.memory.store import MemoryStore  # noqa: E402
from app.memory.vector_index import (  # noqa: E402
    clear_vector_index_cache,
    max_cosines_by_id,
)
from app.memory.store.schema import ensure_vector_epoch_triggers  # noqa: E402
from app.vector_util import cosine_similarity  # noqa: E402


def _legacy_sqlite_scores(store: MemoryStore, query: list[float]) -> int:
    """Previous recall cost: load every embedding JSON and cosine it in Python."""
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT embedding_json
            FROM memories
            WHERE user_id = ?
              AND archived = 0
              AND (status IS NULL OR status != 'archived')
              AND embedding_space_id = ?
              AND embedding_json IS NOT NULL
            """,
            ("default", "bench-space"),
        ).fetchall()
    kept = 0
    for row in rows:
        if cosine_similarity(query, json.loads(row[0])) >= 0.55:
            kept += 1
    return kept


def _build_database_from_vectors(
    path: Path,
    *,
    vectors: list[list[float]],
) -> MemoryStore:
    store = MemoryStore(str(path))
    store.init_db()
    now = datetime.now(UTC).isoformat()
    from app.memory.vector_index import backfill_embedding_units

    with store._connect() as connection:
        for name in (
            "memories_vector_epoch_insert",
            "memories_vector_epoch_delete",
            "memories_vector_epoch_update",
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
        connection.executemany(
            """
            INSERT INTO memories (
                id, user_id, content, type, importance, confidence,
                origin, embedding_json, embedding_space_id,
                created_at, updated_at, archived, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"m{index}",
                    "default",
                    f"memory {index}",
                    "semantic",
                    3,
                    0.7,
                    "user_asserted",
                    json.dumps(vector),
                    "bench-space",
                    now,
                    now,
                    0,
                    "dynamic",
                )
                for index, vector in enumerate(vectors)
            ],
        )
        backfill_embedding_units(connection)
        ensure_vector_epoch_triggers(connection)
        connection.execute(
            """
            INSERT INTO memory_vector_epoch(user_id, epoch)
            VALUES ('default', 1)
            ON CONFLICT(user_id) DO UPDATE SET epoch = epoch + 1
            """
        )
    return store


def _time_call(fn) -> tuple[float, object]:
    started = time.perf_counter()
    value = fn()
    return time.perf_counter() - started, value


def benchmark_count(count: int, dim: int, rng: random.Random) -> dict[str, float | int]:
    raw = [[rng.uniform(-1.0, 1.0) for _ in range(dim)] for _ in range(count)]
    # One stored vector is the query, so both paths have a comparable hit
    # instead of an empty high-dimensional result set.
    query = list(raw[0])
    with tempfile.TemporaryDirectory() as directory:
        store = _build_database_from_vectors(
            Path(directory) / "bench.db",
            vectors=raw,
        )
        legacy_seconds, legacy_kept = _time_call(
            lambda: _legacy_sqlite_scores(store, query)
        )
        clear_vector_index_cache()

        def score() -> int:
            with store._connect() as connection:
                connection.execute("BEGIN")
                scores = max_cosines_by_id(
                    connection,
                    database_path=store.database_path,
                    user_id="default",
                    embedding_space_id="bench-space",
                    query_embeddings=[query],
                )
            return sum(1 for value in scores.values() if value >= 0.55)

        cold_seconds, cold_kept = _time_call(score)
        warm_seconds, warm_kept = _time_call(score)
    return {
        "count": count,
        "dim": dim,
        "legacy_json_cosine_seconds": legacy_seconds,
        "new_cold_seconds": cold_seconds,
        "new_warm_seconds": warm_seconds,
        "legacy_hits": int(legacy_kept),
        "cold_hits": int(cold_kept),
        "warm_hits": int(warm_kept),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", default="10000,50000")
    parser.add_argument("--dim", type=int, default=384)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    counts = [int(item) for item in str(args.counts).split(",") if item.strip()]
    results = [benchmark_count(count, args.dim, rng) for count in counts]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
