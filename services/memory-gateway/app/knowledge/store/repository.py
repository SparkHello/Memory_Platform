"""Composed KnowledgeStore backed directly by focused repository functions."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
import sqlite3

from app.knowledge.store import documents as _documents
from app.knowledge.store import export_import as _export_import
from app.knowledge.store import helpers as _helpers
from app.knowledge.store import references as _references
from app.knowledge.store import schema as _schema
from app.knowledge.store import search as _search
from app.knowledge.store import status as _status
from app.knowledge.store import uploads as _uploads
from app.knowledge.store.constants import _KNOWLEDGE_DB_INIT_LOCK
from app.knowledge.store.errors import KnowledgeValidationError
from app.schema_migrations import (
    apply_schema_migrations,
    enable_wal_with_retry,
    validated_schema_version,
)
from app.sqlite_util import ClosingSQLiteConnection as _ClosingSQLiteConnection


def _serialize_knowledge_init(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with _KNOWLEDGE_DB_INIT_LOCK:
            return method(*args, **kwargs)

    return wrapped


class KnowledgeUploadRepository:
    begin_upload = _uploads.begin_upload
    append_upload = _uploads.append_upload
    commit_upload = _uploads.commit_upload
    cancel_upload = _uploads.cancel_upload


class KnowledgeDocumentRepository:
    list_documents = _documents.list_documents
    resolve_document_refs = _documents.resolve_document_refs
    get_document_detail = _documents.get_document_detail
    get_version = _documents.get_version
    update_document = _documents.update_document
    soft_delete_document = _documents.soft_delete_document
    restore_document = _documents.restore_document
    purge_document = _documents.purge_document
    restore_version = _documents.restore_version
    reindex_version = _documents.reindex_version


class KnowledgeSearchRepository:
    search_chunks = _search.search_chunks
    egress_override_confirmed = _search.egress_override_confirmed
    list_chunks_for_embedding = _search.list_chunks_for_embedding
    set_version_embedding_status = _search.set_version_embedding_status
    replace_chunk_embeddings = _search.replace_chunk_embeddings
    search_chunks_by_embedding = _search.search_chunks_by_embedding
    get_chunks_by_refs = _search.get_chunks_by_refs


class KnowledgeReferenceRepository:
    read_reference = _references.read_reference


class KnowledgeExportRepository:
    list_versions = _export_import.list_versions
    export_user = _export_import.export_user
    restore_export = _export_import.restore_export


class KnowledgeStatusRepository:
    counts = _status.counts
    status = _status.status


class KnowledgeStore(
    KnowledgeUploadRepository,
    KnowledgeDocumentRepository,
    KnowledgeSearchRepository,
    KnowledgeReferenceRepository,
    KnowledgeExportRepository,
    KnowledgeStatusRepository,
):
    """SQLite store for user-scoped, versioned long-form knowledge."""

    _index_version_in_connection = staticmethod(
        _helpers._index_version_in_connection
    )

    def __init__(
        self,
        database_path: str,
        max_document_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        if not database_path or not str(database_path).strip():
            raise KnowledgeValidationError("database_path must not be blank")
        if max_document_bytes <= 0:
            raise KnowledgeValidationError("max_document_bytes must be positive")
        self.database_path = str(database_path)
        self.max_document_bytes = int(max_document_bytes)

    @_serialize_knowledge_init
    def init_db(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            enable_wal_with_retry(connection)
            validated_schema_version(
                connection,
                self._schema_migrations(),
                schema_name="knowledge database",
            )
            connection.executescript(_schema._KNOWLEDGE_TABLES_DDL)
            connection.execute(_schema._KNOWLEDGE_FTS_DDL)
            connection.execute("BEGIN IMMEDIATE")
            self._run_migrations(connection)

    @staticmethod
    def _schema_migrations():
        from app.knowledge.store import migrations as migrations_mod

        return migrations_mod._KNOWLEDGE_SCHEMA_MIGRATIONS

    @staticmethod
    def _run_migrations(connection: sqlite3.Connection) -> None:
        apply_schema_migrations(
            connection,
            KnowledgeStore._schema_migrations(),
            schema_name="knowledge database",
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            factory=_ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
