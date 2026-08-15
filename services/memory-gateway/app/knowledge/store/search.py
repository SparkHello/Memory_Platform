"""FTS, substring and embedding retrieval plus chunk embedding maintenance."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
import sqlite3
from typing import Any

from app.knowledge.models import KnowledgeChunk, KnowledgeSearchHit
from app.knowledge.store.constants import _SEARCH_MAX_RESULTS
from app.knowledge.store.errors import (
    KnowledgeNotFoundError,
    KnowledgeValidationError,
)
from app.knowledge.store.helpers import (
    _ConnectableStore,
    _chunk_from_row,
    _chunk_id,
    _document_ids,
    _get_version_row,
    _search_hit_from_row,
    _version_id,
)
from app.knowledge.store.utils import (
    _bounded_int,
    _fts_query,
    _json_dump,
    _optional_embedding_space_id,
    _optional_text,
    _required_embedding_space_id,
    _required_text,
    _utc_now,
    _validated_vector,
)
from app.vector_util import try_cosine_similarity


def search_chunks(
    store: _ConnectableStore,
    user_id: str,
    query: str,
    limit: int = 5,
    document_refs: Sequence[str] | None = None,
    include_sensitive: bool = False,
) -> list[KnowledgeSearchHit]:
    user_id = _required_text(user_id, "user_id", 256)
    query = _required_text(query, "query", 8000)
    limit = _bounded_int(limit, "limit", minimum=1, maximum=_SEARCH_MAX_RESULTS)
    document_ids = _document_ids(document_refs or [])
    if len(document_ids) > 50:
        raise KnowledgeValidationError("document_refs must not contain more than 50 items")
    if document_ids and not _all_documents_visible(
        store, user_id, document_ids, include_sensitive=include_sensitive
    ):
        return []

    compact_query = "".join(query.split())
    if len(compact_query) < 3:
        rows = _search_with_instr(
            store,
            user_id=user_id,
            query=query,
            limit=limit,
            document_ids=document_ids,
            include_sensitive=include_sensitive,
        )
        signal = "substring"
    else:
        rows = _search_with_fts(
            store,
            user_id=user_id,
            query=query,
            limit=limit,
            document_ids=document_ids,
            include_sensitive=include_sensitive,
        )
        signal = "fts"
        if not rows:
            rows = _search_with_instr(
                store,
                user_id=user_id,
                query=query,
                limit=limit,
                document_ids=document_ids,
                include_sensitive=include_sensitive,
            )
            signal = "substring"
    return [_search_hit_from_row(row, query=query, signal=signal) for row in rows]


def egress_override_confirmed(
    store: _ConnectableStore, user_id: str, version_ref: str
) -> bool:
    """Whether the owner explicitly cleared this version's document for egress.

    Chunk-level sensitivity screening exists to protect documents nobody has
    reviewed.  Once a flagged document has been overridden back to 'normal'
    and confirmed, re-screening every chunk would silently overrule that
    decision and leave the document permanently half-indexed.
    """
    user_id = _required_text(user_id, "user_id", 256)
    version_id = _version_id(version_ref)
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT d.sensitivity, d.sensitivity_override_confirmed
            FROM knowledge_versions v
            JOIN knowledge_documents d
                ON d.id = v.document_id AND d.user_id = v.user_id
            WHERE v.id = ? AND v.user_id = ?
            """,
            (version_id, user_id),
        ).fetchone()
    if row is None:
        return False
    return row["sensitivity"] == "normal" and bool(
        row["sensitivity_override_confirmed"]
    )


def list_chunks_for_embedding(
    store: _ConnectableStore,
    user_id: str,
    version_ref: str,
    *,
    include_sensitive: bool = False,
) -> list[KnowledgeChunk]:
    user_id = _required_text(user_id, "user_id", 256)
    version_id = _version_id(version_ref)
    sensitive_sql = "" if include_sensitive else "AND d.sensitivity = 'normal'"
    with store._connect() as connection:
        rows = connection.execute(
            f"""
            SELECT c.*
            FROM knowledge_chunks c
            JOIN knowledge_documents d
                ON d.id = c.document_id AND d.user_id = c.user_id
            JOIN knowledge_versions v
                ON v.id = c.version_id AND v.user_id = c.user_id
            WHERE c.user_id = ? AND c.version_id = ?
              AND d.status = 'active'
              AND v.index_status = 'ready'
              {sensitive_sql}
            ORDER BY c.ordinal ASC
            """,
            (user_id, version_id),
        ).fetchall()
    return [_chunk_from_row(row) for row in rows]


def set_version_embedding_status(
    store: _ConnectableStore,
    user_id: str,
    version_ref: str,
    *,
    status: str,
    model: str = "",
    embedding_space_id: str = "",
    error: str = "",
) -> None:
    if status not in {
        "pending",
        "indexing",
        "ready",
        "partial",
        "failed",
        "disabled",
    }:
        raise KnowledgeValidationError("invalid knowledge embedding status")
    user_id = _required_text(user_id, "user_id", 256)
    version_id = _version_id(version_ref)
    model = _optional_text(model, "embedding model", 300)
    embedding_space_id = _optional_embedding_space_id(embedding_space_id)
    error = _optional_text(error, "embedding error", 1000)
    embedded_at = _utc_now() if status in {"ready", "partial"} else None
    with store._connect() as connection:
        result = connection.execute(
            """
            UPDATE knowledge_versions
            SET embedding_status = ?, embedding_model = ?,
                embedding_space_id = ?, embedded_at = ?, embedding_error = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                status,
                model,
                embedding_space_id,
                embedded_at,
                error or None,
                version_id,
                user_id,
            ),
        )
        if result.rowcount != 1:
            raise KnowledgeNotFoundError("knowledge version not found")


def replace_chunk_embeddings(
    store: _ConnectableStore,
    user_id: str,
    version_ref: str,
    *,
    model: str,
    embedding_space_id: str,
    vectors: dict[str, list[float]],
    total_chunks: int,
) -> dict[str, int | str]:
    user_id = _required_text(user_id, "user_id", 256)
    version_id = _version_id(version_ref)
    model = _required_text(model, "embedding model", 300)
    embedding_space_id = _required_embedding_space_id(embedding_space_id)
    total_chunks = _bounded_int(
        total_chunks, "total_chunks", minimum=1, maximum=100_000
    )
    prepared: list[tuple[str, list[float]]] = []
    dimensions: int | None = None
    for reference, raw_vector in vectors.items():
        chunk_id = _chunk_id(reference)
        vector = _validated_vector(raw_vector)
        if dimensions is None:
            dimensions = len(vector)
        if len(vector) != dimensions:
            raise KnowledgeValidationError("embedding dimensions must be consistent")
        prepared.append((chunk_id, vector))
    now = _utc_now()
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        version = _get_version_row(
            connection,
            user_id=user_id,
            version_id=version_id,
            active_document=True,
            include_sensitive=True,
        )
        connection.execute(
            "DELETE FROM knowledge_chunk_embeddings "
            "WHERE user_id = ? AND version_id = ?",
            (user_id, version_id),
        )
        stored = 0
        for chunk_id, vector in prepared:
            chunk = connection.execute(
                """
                SELECT id, document_id, content
                FROM knowledge_chunks
                WHERE id = ? AND user_id = ? AND version_id = ?
                """,
                (chunk_id, user_id, version_id),
            ).fetchone()
            if chunk is None:
                continue
            connection.execute(
                """
                INSERT INTO knowledge_chunk_embeddings (
                    chunk_id, document_id, version_id, user_id, model,
                    embedding_space_id,
                    dimensions, vector_json, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    chunk["document_id"],
                    version_id,
                    user_id,
                    model,
                    embedding_space_id,
                    len(vector),
                    _json_dump(vector),
                    hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest(),
                    now,
                ),
            )
            stored += 1
        if stored == total_chunks:
            status = "ready"
            error = None
        elif stored:
            status = "partial"
            error = f"embedded {stored} of {total_chunks} chunks"
        else:
            status = "failed"
            error = "embedding provider returned no vectors"
        connection.execute(
            """
            UPDATE knowledge_versions
            SET embedding_status = ?, embedding_model = ?,
                embedding_space_id = ?, embedded_at = ?, embedding_error = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                status,
                model,
                embedding_space_id,
                now if stored else None,
                error,
                version["id"],
                user_id,
            ),
        )
    return {"status": status, "stored": stored, "total": total_chunks}


def search_chunks_by_embedding(
    store: _ConnectableStore,
    user_id: str,
    query_vector: Sequence[float],
    *,
    embedding_space_id: str,
    query: str = "",
    limit: int = 20,
    document_refs: Sequence[str] | None = None,
    include_sensitive: bool = False,
    min_cosine: float = 0.25,
) -> list[KnowledgeSearchHit]:
    user_id = _required_text(user_id, "user_id", 256)
    vector = _validated_vector(query_vector)
    embedding_space_id = _required_embedding_space_id(embedding_space_id)
    query = _optional_text(query, "query", 8000)
    limit = _bounded_int(limit, "limit", minimum=1, maximum=_SEARCH_MAX_RESULTS)
    document_ids = _document_ids(document_refs or [])
    if len(document_ids) > 50:
        raise KnowledgeValidationError("document_refs must not contain more than 50 items")
    # embedding_space_id is the only vector-space contract.  The stored
    # `model` column is attribution metadata whose meaning differs between
    # runtimes -- an upstream model id in direct mode, a route alias behind
    # the Model Gateway -- so filtering on it would hide vectors that the
    # space id already proves are comparable.
    conditions = [
        "e.user_id = ?",
        "e.embedding_space_id = ?",
        "e.dimensions = ?",
        "d.status = 'active'",
        "d.current_version_id = c.version_id",
        "v.index_status = 'ready'",
        "v.embedding_status IN ('ready', 'partial')",
        "v.embedding_space_id = ?",
    ]
    params: list[Any] = [
        user_id,
        embedding_space_id,
        len(vector),
        embedding_space_id,
    ]
    if not include_sensitive:
        conditions.append("d.sensitivity = 'normal'")
    if document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        conditions.append(f"c.document_id IN ({placeholders})")
        params.extend(document_ids)
    with store._connect() as connection:
        rows = connection.execute(
            f"""
            SELECT
                c.*, d.title, d.source_name, d.content_type, d.sensitivity,
                v.version_number, e.vector_json, 0.0 AS rank
            FROM knowledge_chunk_embeddings e
            JOIN knowledge_chunks c
                ON c.id = e.chunk_id AND c.user_id = e.user_id
            JOIN knowledge_documents d
                ON d.id = c.document_id AND d.user_id = c.user_id
            JOIN knowledge_versions v
                ON v.id = c.version_id AND v.user_id = c.user_id
            WHERE {' AND '.join(conditions)}
            LIMIT 10000
            """,
            params,
        ).fetchall()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        try:
            candidate = _validated_vector(json.loads(row["vector_json"]))
        except (TypeError, json.JSONDecodeError, KnowledgeValidationError):
            continue
        cosine = try_cosine_similarity(vector, candidate)
        if cosine is None or cosine < min_cosine:
            continue
        payload = dict(row)
        payload["rank"] = cosine
        scored.append((cosine, payload))
    scored.sort(key=lambda item: (-item[0], item[1]["ordinal"]))
    return [
        _search_hit_from_row(row, query=query, signal="embedding")
        for _, row in scored[:limit]
    ]


def get_chunks_by_refs(
    store: _ConnectableStore,
    user_id: str,
    chunk_refs: Sequence[str],
    include_sensitive: bool = False,
) -> list[KnowledgeSearchHit]:
    user_id = _required_text(user_id, "user_id", 256)
    if len(chunk_refs) > 20:
        raise KnowledgeValidationError("chunk_refs must not contain more than 20 items")
    chunk_ids = [_chunk_id(ref) for ref in chunk_refs]
    if not chunk_ids:
        return []
    unique_ids = list(dict.fromkeys(chunk_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    sensitive_sql = "" if include_sensitive else "AND d.sensitivity = 'normal'"
    with store._connect() as connection:
        rows = connection.execute(
            f"""
            SELECT
                c.*,
                d.title,
                d.source_name,
                d.content_type,
                d.sensitivity,
                d.status AS document_status,
                v.version_number,
                0.0 AS rank
            FROM knowledge_chunks c
            JOIN knowledge_documents d
                ON d.id = c.document_id AND d.user_id = c.user_id
            JOIN knowledge_versions v
                ON v.id = c.version_id AND v.user_id = c.user_id
            WHERE c.user_id = ?
              AND c.id IN ({placeholders})
              AND d.status = 'active'
              AND v.index_status = 'ready'
              {sensitive_sql}
            """,
            [user_id, *unique_ids],
        ).fetchall()
    by_id = {row["id"]: row for row in rows}
    result: list[KnowledgeSearchHit] = []
    for chunk_id in chunk_ids:
        row = by_id.get(chunk_id)
        if row is not None:
            result.append(_search_hit_from_row(row, query="", signal="reference"))
    return result


def _search_with_fts(
    store: _ConnectableStore,
    *,
    user_id: str,
    query: str,
    limit: int,
    document_ids: list[str],
    include_sensitive: bool,
) -> list[sqlite3.Row]:
    fts_query = _fts_query(query)
    conditions = [
        "knowledge_chunks_fts MATCH ?",
        "c.user_id = ?",
        "d.status = 'active'",
        "d.current_version_id = c.version_id",
        "v.index_status = 'ready'",
    ]
    params: list[Any] = [fts_query, user_id]
    if not include_sensitive:
        conditions.append("d.sensitivity = 'normal'")
    if document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        conditions.append(f"c.document_id IN ({placeholders})")
        params.extend(document_ids)
    params.append(limit)
    try:
        with store._connect() as connection:
            return connection.execute(
                f"""
                SELECT
                    c.*,
                    d.title,
                    d.source_name,
                    d.content_type,
                    d.sensitivity,
                    v.version_number,
                    bm25(knowledge_chunks_fts) AS rank
                FROM knowledge_chunks_fts
                JOIN knowledge_chunks c
                    ON c.id = knowledge_chunks_fts.chunk_id
                JOIN knowledge_documents d
                    ON d.id = c.document_id AND d.user_id = c.user_id
                JOIN knowledge_versions v
                    ON v.id = c.version_id AND v.user_id = c.user_id
                WHERE {' AND '.join(conditions)}
                ORDER BY rank ASC, c.ordinal ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
    except sqlite3.OperationalError:
        return []


def _search_with_instr(
    store: _ConnectableStore,
    *,
    user_id: str,
    query: str,
    limit: int,
    document_ids: list[str],
    include_sensitive: bool,
) -> list[sqlite3.Row]:
    conditions = [
        "c.user_id = ?",
        "d.status = 'active'",
        "d.current_version_id = c.version_id",
        "v.index_status = 'ready'",
        "(instr(lower(c.content), lower(?)) > 0 OR "
        "instr(lower(c.title_path_json), lower(?)) > 0 OR "
        "instr(lower(d.title), lower(?)) > 0)",
    ]
    params: list[Any] = [user_id, query, query, query]
    if not include_sensitive:
        conditions.append("d.sensitivity = 'normal'")
    if document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        conditions.append(f"c.document_id IN ({placeholders})")
        params.extend(document_ids)
    params.append(limit)
    with store._connect() as connection:
        return connection.execute(
            f"""
            SELECT
                c.*,
                d.title,
                d.source_name,
                d.content_type,
                d.sensitivity,
                v.version_number,
                CASE
                    WHEN instr(lower(c.content), lower(?)) > 0 THEN 0.0
                    WHEN instr(lower(c.title_path_json), lower(?)) > 0 THEN 0.5
                    ELSE 1.0
                END AS rank
            FROM knowledge_chunks c
            JOIN knowledge_documents d
                ON d.id = c.document_id AND d.user_id = c.user_id
            JOIN knowledge_versions v
                ON v.id = c.version_id AND v.user_id = c.user_id
            WHERE {' AND '.join(conditions)}
            ORDER BY rank ASC, c.ordinal ASC
            LIMIT ?
            """,
            [query, query, *params],
        ).fetchall()


def _all_documents_visible(
    store: _ConnectableStore,
    user_id: str,
    document_ids: list[str],
    *,
    include_sensitive: bool,
) -> bool:
    unique_ids = list(dict.fromkeys(document_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    sensitivity_sql = "" if include_sensitive else "AND sensitivity = 'normal'"
    with store._connect() as connection:
        count = int(
            connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM knowledge_documents
                WHERE user_id = ? AND status = 'active'
                  AND id IN ({placeholders}) {sensitivity_sql}
                """,
                [user_id, *unique_ids],
            ).fetchone()["count"]
        )
    return count == len(unique_ids)
