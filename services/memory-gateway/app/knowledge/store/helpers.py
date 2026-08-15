"""Shared row mapping and connection-level primitives for the knowledge store."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
import json
import sqlite3
from typing import Protocol

from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSearchHit,
    KnowledgeUploadPart,
    KnowledgeUploadSession,
    KnowledgeVersion,
)
from app.knowledge.store.constants import (
    _CHUNK_PREFIX,
    _DOCUMENT_PREFIX,
    _ID_RE,
    _SEARCH_EXCERPT_CHARS,
    _VERSION_PREFIX,
)
from app.knowledge.store.errors import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgeValidationError,
)
from app.knowledge.store.utils import (
    _chunk_ref,
    _document_ref,
    _excerpt,
    _json_metadata,
    _json_string_list,
    _parse_utc,
    _safe_error,
    _utc_now,
    _version_ref,
)


class ConnectionProvider(Protocol):
    """Knowledge repository dependency that can open SQLite connections.

    The structural contract avoids importing the composed ``KnowledgeStore``
    from domain modules and creating a type-level cycle.
    """

    def _connect(self) -> sqlite3.Connection: ...


class DocumentSizeProvider(ConnectionProvider, Protocol):
    """Connection provider carrying the configured document size boundary."""

    max_document_bytes: int


class VersionIndexProvider(ConnectionProvider, Protocol):
    """Connection provider that can rebuild one knowledge version index."""

    def _index_version_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        document_id: str,
        version_id: str,
        make_current: bool,
    ) -> None: ...


class KnowledgeWriteProvider(
    DocumentSizeProvider,
    VersionIndexProvider,
    Protocol,
):
    """Explicit dependency for writes that enforce size and rebuild indexes."""


def _plain_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise KnowledgeValidationError(f"invalid {label} id")
    return value


def _reference_id(value: str, prefix: str, label: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeValidationError(f"invalid {label} reference")
    raw = value[len(prefix) :] if value.startswith(prefix) else value
    if not _ID_RE.fullmatch(raw):
        raise KnowledgeValidationError(f"invalid {label} reference")
    if value.startswith("knowledge://") and not value.startswith(prefix):
        raise KnowledgeValidationError(f"invalid {label} reference")
    return raw


def _document_id(value: str) -> str:
    return _reference_id(value, _DOCUMENT_PREFIX, "document")


def _version_id(value: str) -> str:
    return _reference_id(value, _VERSION_PREFIX, "version")


def _chunk_id(value: str) -> str:
    return _reference_id(value, _CHUNK_PREFIX, "chunk")


def _document_ids(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        document_id = _document_id(value)
        if document_id not in seen:
            seen.add(document_id)
            result.append(document_id)
    return result


def _require_open_upload(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    upload_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM knowledge_upload_sessions
        WHERE id = ? AND user_id = ?
        """,
        (upload_id, user_id),
    ).fetchone()
    if row is None:
        raise KnowledgeNotFoundError("upload session not found")
    if row["status"] != "open":
        raise KnowledgeConflictError("upload session is not open")
    if _parse_utc(row["expires_at"]) <= datetime.now(UTC):
        connection.execute(
            """
            UPDATE knowledge_upload_sessions
            SET status = 'expired', updated_at = ? WHERE id = ? AND user_id = ?
            """,
            (_utc_now(), upload_id, user_id),
        )
        raise KnowledgeConflictError("upload session has expired")
    return row


def _index_version_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    document_id: str,
    version_id: str,
    make_current: bool,
) -> None:
    row = connection.execute(
        """
        SELECT * FROM knowledge_versions
        WHERE id = ? AND document_id = ? AND user_id = ?
        """,
        (version_id, document_id, user_id),
    ).fetchone()
    if row is None:
        raise KnowledgeNotFoundError("knowledge version not found")
    connection.execute(
        """
        UPDATE knowledge_versions
        SET index_status = 'indexing', index_error = NULL, indexed_at = NULL,
            embedding_status = 'pending', embedding_model = '',
            embedding_space_id = '', embedded_at = NULL,
            embedding_error = NULL
        WHERE id = ? AND user_id = ?
        """,
        (version_id, user_id),
    )
    connection.execute(
        "DELETE FROM knowledge_chunk_embeddings WHERE user_id = ? AND version_id = ?",
        (user_id, version_id),
    )
    connection.execute(
        "DELETE FROM knowledge_chunks_fts WHERE user_id = ? AND version_id = ?",
        (user_id, version_id),
    )
    connection.execute(
        "DELETE FROM knowledge_chunks WHERE user_id = ? AND version_id = ?",
        (user_id, version_id),
    )
    try:
        # Resolve through the package namespace so tests can monkeypatch the
        # chunker on app.knowledge.store.
        from app.knowledge import store as store_package

        drafts = store_package.chunk_knowledge_text(row["content"])
        if not drafts:
            raise ValueError("document content produced no indexable chunks")
        now = _utc_now()
        for draft in drafts:
            chunk_id = f"{version_id}_{draft.ordinal}"
            title_path_json = json.dumps(
                list(draft.title_path), ensure_ascii=False, separators=(",", ":")
            )
            connection.execute(
                """
                INSERT INTO knowledge_chunks (
                    id, document_id, version_id, user_id, ordinal,
                    title_path_json, char_start, char_end, line_start,
                    line_end, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    document_id,
                    version_id,
                    user_id,
                    draft.ordinal,
                    title_path_json,
                    draft.char_start,
                    draft.char_end,
                    draft.line_start,
                    draft.line_end,
                    draft.content,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_chunks_fts (
                    chunk_id, user_id, document_id, version_id, content, title_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    user_id,
                    document_id,
                    version_id,
                    draft.content,
                    " / ".join(draft.title_path),
                ),
            )
        indexed_at = _utc_now()
        connection.execute(
            """
            UPDATE knowledge_versions
            SET index_status = 'ready', index_error = NULL, indexed_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (indexed_at, version_id, user_id),
        )
        if make_current:
            connection.execute(
                """
                UPDATE knowledge_documents
                SET current_version_id = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (version_id, indexed_at, document_id, user_id),
            )
    except Exception as exc:
        connection.execute(
            "DELETE FROM knowledge_chunks_fts WHERE user_id = ? AND version_id = ?",
            (user_id, version_id),
        )
        connection.execute(
            "DELETE FROM knowledge_chunks WHERE user_id = ? AND version_id = ?",
            (user_id, version_id),
        )
        connection.execute(
            """
            UPDATE knowledge_versions
            SET index_status = 'failed', index_error = ?, indexed_at = NULL
            WHERE id = ? AND user_id = ?
            """,
            (_safe_error(exc), version_id, user_id),
        )


def _get_document_row(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    document_id: str,
    include_deleted: bool,
) -> sqlite3.Row:
    status_sql = "" if include_deleted else "AND status = 'active'"
    row = connection.execute(
        f"""
        SELECT * FROM knowledge_documents
        WHERE id = ? AND user_id = ? {status_sql}
        """,
        (document_id, user_id),
    ).fetchone()
    if row is None:
        raise KnowledgeNotFoundError("knowledge document not found")
    return row


def _get_version_row(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    version_id: str,
    active_document: bool,
    include_sensitive: bool,
) -> sqlite3.Row:
    status_sql = "AND d.status = 'active'" if active_document else ""
    sensitivity_sql = "" if include_sensitive else "AND d.sensitivity = 'normal'"
    row = connection.execute(
        f"""
        SELECT v.*
        FROM knowledge_versions v
        JOIN knowledge_documents d
            ON d.id = v.document_id AND d.user_id = v.user_id
        WHERE v.id = ? AND v.user_id = ? {status_sql} {sensitivity_sql}
        """,
        (version_id, user_id),
    ).fetchone()
    if row is None:
        raise KnowledgeNotFoundError("knowledge version not found")
    return row


def _get_chunk_row(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    chunk_id: str,
    include_sensitive: bool,
) -> sqlite3.Row:
    sensitivity_sql = "" if include_sensitive else "AND d.sensitivity = 'normal'"
    row = connection.execute(
        f"""
        SELECT c.*, d.title, d.source_name, d.content_type, d.sensitivity
        FROM knowledge_chunks c
        JOIN knowledge_documents d
            ON d.id = c.document_id AND d.user_id = c.user_id
        JOIN knowledge_versions v
            ON v.id = c.version_id AND v.user_id = c.user_id
        WHERE c.id = ? AND c.user_id = ?
          AND d.status = 'active' AND v.index_status = 'ready'
          {sensitivity_sql}
        """,
        (chunk_id, user_id),
    ).fetchone()
    if row is None:
        raise KnowledgeNotFoundError("knowledge reference not found")
    return row


def _document_select_sql() -> str:
    return """
        SELECT
            d.*,
            cv.version_number AS current_version_number,
            cv.index_status AS current_index_status,
            COALESCE(
                cv.byte_size,
                (SELECT lv.byte_size FROM knowledge_versions lv
                 WHERE lv.document_id = d.id AND lv.user_id = d.user_id
                 ORDER BY lv.version_number DESC LIMIT 1),
                0
            ) AS current_byte_size,
            COALESCE(
                cv.character_count,
                (SELECT lv.character_count FROM knowledge_versions lv
                 WHERE lv.document_id = d.id AND lv.user_id = d.user_id
                 ORDER BY lv.version_number DESC LIMIT 1),
                0
            ) AS current_character_count,
            COALESCE(
                cv.index_status,
                (SELECT lv.index_status FROM knowledge_versions lv
                 WHERE lv.document_id = d.id AND lv.user_id = d.user_id
                 ORDER BY lv.version_number DESC LIMIT 1)
            ) AS display_index_status
        FROM knowledge_documents d
        LEFT JOIN knowledge_versions cv
            ON cv.id = d.current_version_id AND cv.user_id = d.user_id
    """


def _load_document_model(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    document_id: str,
) -> KnowledgeDocument:
    row = connection.execute(
        f"""
        {_document_select_sql()}
        WHERE d.id = ? AND d.user_id = ?
        """,
        (document_id, user_id),
    ).fetchone()
    if row is None:
        raise KnowledgeNotFoundError("knowledge document not found")
    return _document_from_row(row)


def _document_from_row(row: sqlite3.Row) -> KnowledgeDocument:
    version_id = row["current_version_id"]
    return KnowledgeDocument(
        id=row["id"],
        ref=_document_ref(row["id"]),
        user_id=row["user_id"],
        title=row["title"],
        source_name=row["source_name"],
        content_type=row["content_type"],
        sensitivity=row["sensitivity"],
        detected_sensitivity=row["detected_sensitivity"],
        sensitivity_override_confirmed=bool(
            row["sensitivity_override_confirmed"]
        ),
        status=row["status"],
        current_version_id=version_id,
        current_version_ref=_version_ref(version_id) if version_id else "",
        current_version_number=row["current_version_number"],
        index_status=row["display_index_status"],
        byte_size=int(row["current_byte_size"] or 0),
        character_count=int(row["current_character_count"] or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        tags=_json_string_list(row["tags_json"]),
        metadata=_json_metadata(row["metadata_json"]),
    )


def _version_from_row(
    row: sqlite3.Row,
    *,
    include_content: bool = False,
) -> KnowledgeVersion:
    return KnowledgeVersion(
        id=row["id"],
        ref=_version_ref(row["id"]),
        document_id=row["document_id"],
        document_ref=_document_ref(row["document_id"]),
        user_id=row["user_id"],
        version_number=int(row["version_number"]),
        content_sha256=row["content_sha256"],
        byte_size=int(row["byte_size"]),
        character_count=int(row["character_count"]),
        index_status=row["index_status"],
        index_error=row["index_error"],
        created_at=row["created_at"],
        indexed_at=row["indexed_at"],
        embedding_status=row["embedding_status"],
        embedding_model=row["embedding_model"],
        embedding_space_id=row["embedding_space_id"],
        embedded_at=row["embedded_at"],
        embedding_error=row["embedding_error"],
        content=row["content"] if include_content else None,
    )


def _chunk_from_row(row: sqlite3.Row) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=row["id"],
        ref=_chunk_ref(row["id"]),
        document_id=row["document_id"],
        document_ref=_document_ref(row["document_id"]),
        version_id=row["version_id"],
        version_ref=_version_ref(row["version_id"]),
        user_id=row["user_id"],
        ordinal=int(row["ordinal"]),
        title_path=_json_string_list(row["title_path_json"]),
        char_start=int(row["char_start"]),
        char_end=int(row["char_end"]),
        line_start=int(row["line_start"]),
        line_end=int(row["line_end"]),
        content=row["content"],
        created_at=row["created_at"],
    )


def _search_hit_from_row(
    row: sqlite3.Row,
    *,
    query: str,
    signal: str,
) -> KnowledgeSearchHit:
    content = row["content"]
    if query:
        excerpt, local_start, local_end = _excerpt(content, query, _SEARCH_EXCERPT_CHARS)
    else:
        excerpt, local_start, local_end = content, 0, len(content)
    absolute_start = int(row["char_start"]) + local_start
    absolute_end = int(row["char_start"]) + local_end
    line_start = int(row["line_start"]) + content.count("\n", 0, local_start)
    line_end = line_start + max(0, excerpt.count("\n") - (1 if excerpt.endswith("\n") else 0))
    rank = float(row["rank"] or 0.0)
    signals = [signal]
    if signal == "fts":
        signals.append("trigram")
    if query and query.casefold() in content.casefold():
        signals.append("exact_phrase")
    title_path = _json_string_list(row["title_path_json"])
    if query and query.casefold() in " / ".join(title_path).casefold():
        signals.append("heading")
    if signal == "reference":
        score = 1.0
    elif signal == "embedding":
        score = max(-1.0, min(1.0, rank))
        signals.append("cosine")
    elif signal == "fts":
        # FTS5 bm25 is ordered ascending and normally returns negative
        # values; negate it so a stronger match also has a larger score.
        score = max(0.0, -rank)
    else:
        score = 1.0 / (1.0 + max(0.0, rank))
    return KnowledgeSearchHit(
        document_ref=_document_ref(row["document_id"]),
        version_ref=_version_ref(row["version_id"]),
        chunk_ref=_chunk_ref(row["id"]),
        title=row["title"],
        source_name=row["source_name"],
        content_type=row["content_type"],
        sensitivity=row["sensitivity"],
        title_path=title_path,
        ordinal=int(row["ordinal"]),
        char_start=absolute_start,
        char_end=absolute_end,
        line_start=line_start,
        line_end=max(line_start, line_end),
        excerpt=excerpt,
        score=score,
        match_signals=signals,
        channels=[signal],
    )


def _upload_session_from_row(row: sqlite3.Row) -> KnowledgeUploadSession:
    replace_id = row["replace_document_id"]
    expected_id = row["expected_current_version_id"]
    return KnowledgeUploadSession(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        content_type=row["content_type"],
        source_name=row["source_name"],
        sensitivity=row["sensitivity"],
        tags=_json_string_list(row["tags_json"]),
        metadata=_json_metadata(row["metadata_json"]),
        replace_document_id=replace_id,
        replace_document_ref=_document_ref(replace_id) if replace_id else "",
        expected_current_version_id=expected_id,
        expected_current_version_ref=_version_ref(expected_id) if expected_id else "",
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
        committed_document_ref=row["committed_document_ref"],
        committed_version_ref=row["committed_version_ref"],
    )


def _upload_part_from_row(
    row: sqlite3.Row,
    *,
    duplicate: bool = False,
) -> KnowledgeUploadPart:
    return KnowledgeUploadPart(
        upload_id=row["upload_id"],
        sequence=int(row["sequence"]),
        character_count=int(row["character_count"]),
        byte_size=int(row["byte_size"]),
        content_sha256=row["content_sha256"],
        created_at=row["created_at"],
        duplicate=duplicate,
    )
