"""Cached unit-vector matrix for memory recall.

Search stores an L2-normalized little-endian float64 blob beside the original
``embedding_json``. Cosine similarity is then a dot product. The decoded
matrix stays in process memory until ``memory_vector_epoch`` changes, so a
query does not parse every row's JSON again.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
import os
import sqlite3
import threading

from app import vector_util
from app.memory.utils import parse_embedding_vector
from app.vector_util import (
    batch_max_cosine,
    pack_unit_vector,
    unit_vector,
    unpack_unit_vector,
)


_CACHE_MAX_SPACES = 4
_CACHE: OrderedDict[tuple, "_SpaceIndex"] = OrderedDict()
_CACHE_LOCK = threading.Lock()


class _DimMatrix:
    def __init__(self, ids: list[str], matrix) -> None:
        self.ids = ids
        self.id_to_pos = {memory_id: index for index, memory_id in enumerate(ids)}
        self.matrix = matrix


class _SpaceIndex:
    def __init__(self, epoch: int, by_dim: dict[int, _DimMatrix]) -> None:
        self.epoch = epoch
        self.by_dim = by_dim


def clear_vector_index_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def embedding_unit_blob(raw_json: str | None) -> bytes | None:
    """Blob written next to ``embedding_json``.

    ``None`` means the row has no vector. ``b""`` means the text was present
    but cannot be normalized, so later reads do not parse it again.
    """
    if not raw_json:
        return None
    vector = parse_embedding_vector(raw_json)
    if vector is None:
        return b""
    packed = pack_unit_vector(vector)
    return packed if packed is not None else b""


def vector_recall_ready(connection: sqlite3.Connection) -> bool:
    try:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'memory_vector_epoch'
            """
        ).fetchone()
        if table is None:
            return False
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(memories)")
        }
    except sqlite3.Error:
        return False
    return "embedding_unit" in columns


def backfill_embedding_units(connection: sqlite3.Connection) -> int:
    """Fill ``embedding_unit`` from existing ``embedding_json`` rows.

    Original JSON is left unchanged. Invalid and zero vectors are marked with
    an empty blob so a later startup does not scan them again.
    """
    updated = 0
    while True:
        rows = connection.execute(
            """
            SELECT id, embedding_json
            FROM memories
            WHERE embedding_json IS NOT NULL AND embedding_unit IS NULL
            LIMIT 500
            """
        ).fetchall()
        if not rows:
            return updated
        payload = [
            (embedding_unit_blob(row["embedding_json"]), row["id"]) for row in rows
        ]
        connection.executemany(
            "UPDATE memories SET embedding_unit = ? WHERE id = ?",
            payload,
        )
        updated += len(payload)


def max_cosines_by_id(
    connection: sqlite3.Connection,
    *,
    database_path: str,
    user_id: str,
    embedding_space_id: str,
    query_embeddings: Sequence[Sequence[float]],
) -> dict[str, float]:
    """Return the max cosine of each comparable memory against the queries."""
    if not embedding_space_id or not query_embeddings:
        return {}
    epoch = _read_epoch(connection, user_id)
    key = (_database_token(database_path), user_id, embedding_space_id)
    index = _cached_index(key, epoch)
    if index is None:
        index = _load_space_index(
            connection,
            user_id=user_id,
            embedding_space_id=embedding_space_id,
            epoch=epoch,
        )
        _store_index(key, index)
    return _score_index(index, query_embeddings)


def _read_epoch(connection: sqlite3.Connection, user_id: str) -> int:
    try:
        row = connection.execute(
            "SELECT epoch FROM memory_vector_epoch WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _database_token(database_path: str) -> tuple:
    try:
        stat = os.stat(database_path)
    except OSError:
        return (database_path,)
    return (database_path, stat.st_dev, stat.st_ino)


def _cached_index(key: tuple, epoch: int) -> _SpaceIndex | None:
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is None or cached.epoch != epoch:
            return None
        _CACHE.move_to_end(key)
        return cached


def _store_index(key: tuple, index: _SpaceIndex) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = index
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX_SPACES:
            _CACHE.popitem(last=False)


def _load_space_index(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    embedding_space_id: str,
    epoch: int,
) -> _SpaceIndex:
    grouped: dict[int, list[tuple[str, list[float]]]] = {}
    # Unit blobs are the hot representation. JSON is read only for rows that
    # have not been backfilled yet, so a rebuilt matrix does not pull every
    # embedding document off disk.
    packed_rows = connection.execute(
        """
        SELECT id, embedding_unit
        FROM memories
        WHERE user_id = ?
          AND archived = 0
          AND (status IS NULL OR status != 'archived')
          AND embedding_space_id = ?
          AND embedding_unit IS NOT NULL
          AND length(embedding_unit) > 0
        """,
        (user_id, embedding_space_id),
    ).fetchall()
    pending_rows = connection.execute(
        """
        SELECT id, embedding_json
        FROM memories
        WHERE user_id = ?
          AND archived = 0
          AND (status IS NULL OR status != 'archived')
          AND embedding_space_id = ?
          AND embedding_unit IS NULL
          AND embedding_json IS NOT NULL
        """,
        (user_id, embedding_space_id),
    ).fetchall()
    for row in packed_rows:
        vector = unpack_unit_vector(row["embedding_unit"])
        if vector is None:
            continue
        grouped.setdefault(len(vector), []).append((str(row["id"]), vector))
    for row in pending_rows:
        parsed = parse_embedding_vector(row["embedding_json"])
        vector = unit_vector(parsed) if parsed is not None else None
        if vector is None:
            continue
        grouped.setdefault(len(vector), []).append((str(row["id"]), vector))
    np = vector_util._numpy()
    by_dim: dict[int, _DimMatrix] = {}
    for dim, items in grouped.items():
        ids = [memory_id for memory_id, _ in items]
        vectors = [vector for _, vector in items]
        matrix = np.asarray(vectors, dtype=np.float64) if np is not None else vectors
        by_dim[dim] = _DimMatrix(ids, matrix)
    return _SpaceIndex(epoch, by_dim)


def _score_index(
    index: _SpaceIndex,
    query_embeddings: Sequence[Sequence[float]],
) -> dict[str, float]:
    queries_by_dim: dict[int, list[Sequence[float]]] = {}
    for query in query_embeddings:
        if not query:
            continue
        queries_by_dim.setdefault(len(query), []).append(query)
    scores: dict[str, float] = {}
    for dim, queries in queries_by_dim.items():
        block = index.by_dim.get(dim)
        if block is None or not block.ids:
            continue
        cosines = batch_max_cosine(queries, block.matrix)
        for memory_id, cosine in zip(block.ids, cosines, strict=True):
            previous = scores.get(memory_id)
            if previous is None or cosine > previous:
                scores[memory_id] = cosine
    return scores
