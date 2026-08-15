"""Bootstrap DDL and legacy-column ensure helpers for knowledge.db."""

from __future__ import annotations

import sqlite3

from app.schema_migrations import _ensure_columns

_KNOWLEDGE_TABLES_DDL = """
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT 'text/markdown',
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    detected_sensitivity TEXT NOT NULL DEFAULT 'normal',
                    sensitivity_override_confirmed INTEGER NOT NULL DEFAULT 0,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    current_version_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    CHECK (content_type IN ('text/plain', 'text/markdown')),
                    CHECK (sensitivity IN ('normal', 'private', 'sensitive')),
                    CHECK (detected_sensitivity IN ('normal', 'private', 'sensitive')),
                    CHECK (sensitivity_override_confirmed IN (0, 1)),
                    CHECK (status IN ('active', 'deleted'))
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_user_status
                    ON knowledge_documents(user_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    character_count INTEGER NOT NULL,
                    index_status TEXT NOT NULL DEFAULT 'pending',
                    index_error TEXT,
                    created_at TEXT NOT NULL,
                    indexed_at TEXT,
                    embedding_status TEXT NOT NULL DEFAULT 'pending',
                    embedding_model TEXT NOT NULL DEFAULT '',
                    embedding_space_id TEXT NOT NULL DEFAULT '',
                    embedded_at TEXT,
                    embedding_error TEXT,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    UNIQUE(document_id, version_number),
                    CHECK (version_number >= 1),
                    CHECK (byte_size >= 0),
                    CHECK (character_count >= 0),
                    CHECK (index_status IN ('pending', 'indexing', 'ready', 'failed')),
                    CHECK (embedding_status IN (
                        'pending', 'indexing', 'ready', 'partial', 'failed', 'disabled'
                    ))
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_versions_user_document
                    ON knowledge_versions(user_id, document_id, version_number DESC);
                CREATE INDEX IF NOT EXISTS idx_knowledge_versions_user_index_status
                    ON knowledge_versions(user_id, index_status, created_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    title_path_json TEXT NOT NULL DEFAULT '[]',
                    char_start INTEGER NOT NULL,
                    char_end INTEGER NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(version_id) REFERENCES knowledge_versions(id) ON DELETE CASCADE,
                    UNIQUE(version_id, ordinal),
                    CHECK (ordinal >= 0),
                    CHECK (char_start >= 0 AND char_end >= char_start),
                    CHECK (line_start >= 1 AND line_end >= line_start)
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_user_version
                    ON knowledge_chunks(user_id, version_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_user_document
                    ON knowledge_chunks(user_id, document_id, version_id);

                CREATE TABLE IF NOT EXISTS knowledge_upload_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    replace_document_id TEXT,
                    expected_current_version_id TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    committed_document_ref TEXT NOT NULL DEFAULT '',
                    committed_version_ref TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(replace_document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    CHECK (status IN ('open', 'committing', 'committed', 'failed', 'expired'))
                );

                CREATE TABLE IF NOT EXISTS knowledge_chunk_embeddings (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    embedding_space_id TEXT NOT NULL DEFAULT '',
                    dimensions INTEGER NOT NULL,
                    vector_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(chunk_id) REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(version_id) REFERENCES knowledge_versions(id) ON DELETE CASCADE,
                    CHECK (dimensions > 0)
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_user_version
                    ON knowledge_chunk_embeddings(user_id, version_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_user_document
                    ON knowledge_chunk_embeddings(user_id, document_id, version_id);

                CREATE INDEX IF NOT EXISTS idx_knowledge_upload_sessions_user_status
                    ON knowledge_upload_sessions(user_id, status, expires_at);

                CREATE TABLE IF NOT EXISTS knowledge_upload_parts (
                    upload_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    character_count INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(upload_id, sequence),
                    FOREIGN KEY(upload_id) REFERENCES knowledge_upload_sessions(id) ON DELETE CASCADE,
                    CHECK (sequence >= 0),
                    CHECK (character_count >= 0),
                    CHECK (byte_size >= 0)
                );
                """

# A contentful FTS table makes reindex and cascading purge explicit
# and reliable.  The canonical text remains knowledge_chunks.
_KNOWLEDGE_FTS_DDL = """
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    user_id UNINDEXED,
                    document_id UNINDEXED,
                    version_id UNINDEXED,
                    content,
                    title_path,
                    tokenize='trigram'
                )
                """


def _ensure_documents_source_document_ref(connection: sqlite3.Connection) -> None:
    _ensure_columns(
        connection,
        "knowledge_documents",
        {"source_document_ref": "TEXT NOT NULL DEFAULT ''"},
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_knowledge_documents_user_source_ref
        ON knowledge_documents(user_id, source_document_ref)
        """
    )


def _ensure_document_metadata_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(
        connection,
        "knowledge_documents",
        {
            "tags_json": "TEXT NOT NULL DEFAULT '[]'",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        },
    )


def _ensure_document_sensitivity_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(
        connection,
        "knowledge_documents",
        {
            "detected_sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
            "sensitivity_override_confirmed": "INTEGER NOT NULL DEFAULT 0",
        },
    )


def _ensure_version_embedding_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(
        connection,
        "knowledge_versions",
        {
            "embedding_status": "TEXT NOT NULL DEFAULT 'pending'",
            "embedding_model": "TEXT NOT NULL DEFAULT ''",
            "embedded_at": "TEXT",
            "embedding_error": "TEXT",
        },
    )


def _ensure_embedding_space_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(
        connection,
        "knowledge_versions",
        {"embedding_space_id": "TEXT NOT NULL DEFAULT ''"},
    )
    # Existing derived vectors deliberately remain in the empty,
    # unknown space. A later index run is the only safe way to bind
    # them to a configured vector space.
    _ensure_columns(
        connection,
        "knowledge_chunk_embeddings",
        {"embedding_space_id": "TEXT NOT NULL DEFAULT ''"},
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_user_space
        ON knowledge_chunk_embeddings(user_id, embedding_space_id, version_id)
        """
    )


def _ensure_upload_metadata_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(
        connection,
        "knowledge_upload_sessions",
        {
            "tags_json": "TEXT NOT NULL DEFAULT '[]'",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        },
    )
