"""Status and count reporting for the knowledge database."""

from __future__ import annotations

import sqlite3
from typing import Any

from app.knowledge.store.helpers import ConnectionProvider
from app.knowledge.store.utils import _required_text, _utc_now

# Explicit status → count-key mappings.  Unknown enum values no longer create
# silent dynamic keys; they only still roll up into the totals below.
_DOCUMENT_STATUS_KEYS = {
    "active": "active_documents",
    "deleted": "deleted_documents",
}
_INDEX_STATUS_KEYS = {
    "pending": "index_pending",
    "indexing": "index_indexing",
    "ready": "index_ready",
    "failed": "index_failed",
}
_EMBEDDING_STATUS_KEYS = {
    "pending": "embedding_pending",
    "indexing": "embedding_indexing",
    "ready": "embedding_ready",
    "partial": "embedding_partial",
    "failed": "embedding_failed",
    "disabled": "embedding_disabled",
}


def counts(store: ConnectionProvider, user_id: str) -> dict[str, int]:
    user_id = _required_text(user_id, "user_id", 256)
    with store._connect() as connection:
        document_rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count FROM knowledge_documents
            WHERE user_id = ? GROUP BY status
            """,
            (user_id,),
        ).fetchall()
        version_rows = connection.execute(
            """
            SELECT index_status, COUNT(*) AS count FROM knowledge_versions
            WHERE user_id = ? GROUP BY index_status
            """,
            (user_id,),
        ).fetchall()
        embedding_rows = connection.execute(
            """
            SELECT embedding_status, COUNT(*) AS count
            FROM knowledge_versions
            WHERE user_id = ? GROUP BY embedding_status
            """,
            (user_id,),
        ).fetchall()
        chunk_count = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM knowledge_chunks WHERE user_id = ?",
                (user_id,),
            ).fetchone()["count"]
        )
        embedded_chunk_count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM knowledge_chunk_embeddings WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()["count"]
        )
        open_uploads = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count FROM knowledge_upload_sessions
                WHERE user_id = ? AND status = 'open' AND expires_at > ?
                """,
                (user_id, _utc_now()),
            ).fetchone()["count"]
        )
    result = {
        "documents": 0,
        "active_documents": 0,
        "deleted_documents": 0,
        "versions": 0,
        "chunks": chunk_count,
        "embedded_chunks": embedded_chunk_count,
        "index_pending": 0,
        "index_indexing": 0,
        "index_ready": 0,
        "index_failed": 0,
        "open_uploads": open_uploads,
        "embedding_pending": 0,
        "embedding_indexing": 0,
        "embedding_ready": 0,
        "embedding_partial": 0,
        "embedding_failed": 0,
        "embedding_disabled": 0,
    }
    for row in document_rows:
        count = int(row["count"])
        result["documents"] += count
        key = _DOCUMENT_STATUS_KEYS.get(row["status"])
        if key is not None:
            result[key] = count
    for row in version_rows:
        count = int(row["count"])
        result["versions"] += count
        key = _INDEX_STATUS_KEYS.get(row["index_status"])
        if key is not None:
            result[key] = count
    for row in embedding_rows:
        key = _EMBEDDING_STATUS_KEYS.get(row["embedding_status"])
        if key is not None:
            result[key] = int(row["count"])
    return result


def status(store: ConnectionProvider, user_id: str) -> dict[str, Any]:
    try:
        counts_result = counts(store, user_id)
    except sqlite3.Error as exc:
        return {
            "available": False,
            "fts5": False,
            "tokenizer": "trigram",
            "error": str(exc),
            "counts": {},
        }
    return {
        "available": True,
        "fts5": True,
        "tokenizer": "trigram",
        "error": "",
        "counts": counts_result,
    }
