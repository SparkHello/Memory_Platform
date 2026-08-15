"""KnowledgeStore orchestration shell.

The public API lives here as thin delegators; implementations are split
across the focused modules under app.knowledge.store and only depend on the
``_ConnectableStore`` Protocol from helpers.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import wraps
from pathlib import Path
import sqlite3
from typing import Any

from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeCommitResult,
    KnowledgeDocument,
    KnowledgeSearchHit,
    KnowledgeSensitivity,
    KnowledgeUploadPart,
    KnowledgeUploadSession,
    KnowledgeVersion,
)
from app.knowledge.store import documents as _documents
from app.knowledge.store import export_import as _export_import
from app.knowledge.store import references as _references
from app.knowledge.store import schema as _schema
from app.knowledge.store import search as _search
from app.knowledge.store import status as _status
from app.knowledge.store import uploads as _uploads
from app.knowledge.store import helpers as _helpers
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


class KnowledgeStore:
    """SQLite store for versioned long-form knowledge.

    This class owns no memory-store object and never reads or writes the memory
    database.  Every public record operation is scoped by ``user_id``.
    """

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
            # executescript commits implicitly, so acquire the cross-process
            # migration lock only after the idempotent bootstrap DDL finishes.
            connection.execute("BEGIN IMMEDIATE")
            self._run_migrations(connection)

    @staticmethod
    def _schema_migrations():
        # Resolve through the migrations module so tests can monkeypatch the list.
        from app.knowledge.store import migrations as migrations_mod

        return migrations_mod._KNOWLEDGE_SCHEMA_MIGRATIONS

    @staticmethod
    def _run_migrations(connection: sqlite3.Connection) -> None:
        """按 PRAGMA user_version 顺序执行一次性的 schema/数据迁移。"""
        apply_schema_migrations(
            connection,
            KnowledgeStore._schema_migrations(),
            schema_name="knowledge database",
        )

    # ------------------------------------------------------------------
    # Upload lifecycle

    def begin_upload(
        self,
        user_id: str,
        title: str,
        *,
        content_type: str = "text/markdown",
        source_name: str = "",
        replace_document_ref: str = "",
        sensitivity: KnowledgeSensitivity = "normal",
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeUploadSession:
        return _uploads.begin_upload(
            self,
            user_id,
            title,
            content_type=content_type,
            source_name=source_name,
            replace_document_ref=replace_document_ref,
            sensitivity=sensitivity,
            tags=tags,
            metadata=metadata,
        )

    def append_upload(
        self,
        user_id: str,
        upload_id: str,
        sequence: int,
        text: str,
    ) -> KnowledgeUploadPart:
        return _uploads.append_upload(self, user_id, upload_id, sequence, text)

    def commit_upload(
        self,
        user_id: str,
        upload_id: str,
        expected_parts: int,
        expected_sha256: str = "",
        confirm_sensitivity_override: bool = False,
    ) -> KnowledgeCommitResult:
        return _uploads.commit_upload(
            self,
            user_id,
            upload_id,
            expected_parts,
            expected_sha256,
            confirm_sensitivity_override,
        )

    def cancel_upload(self, user_id: str, upload_id: str) -> bool:
        return _uploads.cancel_upload(self, user_id, upload_id)

    # ------------------------------------------------------------------
    # Document and version management

    def list_documents(
        self,
        user_id: str,
        query: str = "",
        status: str = "active",
        limit: int = 50,
        include_sensitive: bool = False,
    ) -> list[KnowledgeDocument]:
        return _documents.list_documents(
            self, user_id, query, status, limit, include_sensitive
        )

    def resolve_document_refs(
        self,
        user_id: str,
        *,
        document_refs: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        metadata_filter: dict[str, Any] | None = None,
        include_sensitive: bool = False,
        limit: int = 50,
    ) -> list[str]:
        return _documents.resolve_document_refs(
            self,
            user_id,
            document_refs=document_refs,
            tags=tags,
            metadata_filter=metadata_filter,
            include_sensitive=include_sensitive,
            limit=limit,
        )

    def get_document_detail(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        include_content: bool = False,
        include_sensitive: bool = True,
    ) -> dict[str, Any]:
        return _documents.get_document_detail(
            self,
            user_id,
            document_id,
            document_ref=document_ref,
            include_content=include_content,
            include_sensitive=include_sensitive,
        )

    def get_version(
        self,
        user_id: str,
        version_id: str,
        *,
        include_content: bool = False,
        include_sensitive: bool = True,
    ) -> KnowledgeVersion:
        return _documents.get_version(
            self,
            user_id,
            version_id,
            include_content=include_content,
            include_sensitive=include_sensitive,
        )

    def update_document(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        title: str | None = None,
        source_name: str | None = None,
        sensitivity: KnowledgeSensitivity | None = None,
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeDocument:
        return _documents.update_document(
            self,
            user_id,
            document_id,
            document_ref=document_ref,
            title=title,
            source_name=source_name,
            sensitivity=sensitivity,
            tags=tags,
            metadata=metadata,
        )

    def soft_delete_document(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        confirm_document_ref: str = "",
    ) -> KnowledgeDocument:
        return _documents.soft_delete_document(
            self,
            user_id,
            document_id,
            document_ref=document_ref,
            confirm_document_ref=confirm_document_ref,
        )

    def restore_document(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
    ) -> KnowledgeDocument:
        return _documents.restore_document(
            self, user_id, document_id, document_ref=document_ref
        )

    def purge_document(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        confirm_document_ref: str = "",
        confirm_document_id: str = "",
    ) -> bool:
        return _documents.purge_document(
            self,
            user_id,
            document_id,
            document_ref=document_ref,
            confirm_document_ref=confirm_document_ref,
            confirm_document_id=confirm_document_id,
        )

    def restore_version(
        self,
        user_id: str,
        document_id: str = "",
        version_id: str = "",
        *,
        document_ref: str = "",
        version_ref: str = "",
    ) -> KnowledgeCommitResult:
        return _documents.restore_version(
            self,
            user_id,
            document_id,
            version_id,
            document_ref=document_ref,
            version_ref=version_ref,
        )

    def reindex_version(
        self,
        user_id: str,
        version_id: str = "",
        *,
        document_id: str = "",
        document_ref: str = "",
        version_ref: str = "",
    ) -> KnowledgeCommitResult:
        return _documents.reindex_version(
            self,
            user_id,
            version_id,
            document_id=document_id,
            document_ref=document_ref,
            version_ref=version_ref,
        )

    # ------------------------------------------------------------------
    # Retrieval

    def search_chunks(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        document_refs: Sequence[str] | None = None,
        include_sensitive: bool = False,
    ) -> list[KnowledgeSearchHit]:
        return _search.search_chunks(
            self, user_id, query, limit, document_refs, include_sensitive
        )

    def egress_override_confirmed(self, user_id: str, version_ref: str) -> bool:
        return _search.egress_override_confirmed(self, user_id, version_ref)

    def list_chunks_for_embedding(
        self,
        user_id: str,
        version_ref: str,
        *,
        include_sensitive: bool = False,
    ) -> list[KnowledgeChunk]:
        return _search.list_chunks_for_embedding(
            self, user_id, version_ref, include_sensitive=include_sensitive
        )

    def set_version_embedding_status(
        self,
        user_id: str,
        version_ref: str,
        *,
        status: str,
        model: str = "",
        embedding_space_id: str = "",
        error: str = "",
    ) -> None:
        return _search.set_version_embedding_status(
            self,
            user_id,
            version_ref,
            status=status,
            model=model,
            embedding_space_id=embedding_space_id,
            error=error,
        )

    def replace_chunk_embeddings(
        self,
        user_id: str,
        version_ref: str,
        *,
        model: str,
        embedding_space_id: str,
        vectors: dict[str, list[float]],
        total_chunks: int,
    ) -> dict[str, int | str]:
        return _search.replace_chunk_embeddings(
            self,
            user_id,
            version_ref,
            model=model,
            embedding_space_id=embedding_space_id,
            vectors=vectors,
            total_chunks=total_chunks,
        )

    def search_chunks_by_embedding(
        self,
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
        return _search.search_chunks_by_embedding(
            self,
            user_id,
            query_vector,
            embedding_space_id=embedding_space_id,
            query=query,
            limit=limit,
            document_refs=document_refs,
            include_sensitive=include_sensitive,
            min_cosine=min_cosine,
        )

    def get_chunks_by_refs(
        self,
        user_id: str,
        chunk_refs: Sequence[str],
        include_sensitive: bool = False,
    ) -> list[KnowledgeSearchHit]:
        return _search.get_chunks_by_refs(
            self, user_id, chunk_refs, include_sensitive
        )

    def read_reference(
        self,
        user_id: str,
        reference: str,
        cursor: str = "",
        max_chars: int = 12_000,
        include_sensitive: bool = False,
        signing_key: str | bytes = "",
    ) -> dict[str, Any]:
        return _references.read_reference(
            self,
            user_id,
            reference,
            cursor,
            max_chars,
            include_sensitive,
            signing_key,
        )

    # ------------------------------------------------------------------
    # Independent backup and restore

    def list_versions(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        include_content: bool = False,
    ) -> list[KnowledgeVersion]:
        return _export_import.list_versions(
            self,
            user_id,
            document_id,
            document_ref=document_ref,
            include_content=include_content,
        )

    def export_user(self, user_id: str) -> dict[str, Any]:
        return _export_import.export_user(self, user_id)

    def restore_export(self, user_id: str, export_data: dict[str, Any]) -> dict[str, Any]:
        return _export_import.restore_export(self, user_id, export_data)

    # ------------------------------------------------------------------
    # Status and counts

    def counts(self, user_id: str) -> dict[str, int]:
        return _status.counts(self, user_id)

    def status(self, user_id: str) -> dict[str, Any]:
        return _status.status(self, user_id)

    # ------------------------------------------------------------------
    # Internal SQL and mapping helpers

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

    def _index_version_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        document_id: str,
        version_id: str,
        make_current: bool,
    ) -> None:
        return _helpers._index_version_in_connection(
            connection,
            user_id=user_id,
            document_id=document_id,
            version_id=version_id,
            make_current=make_current,
        )
