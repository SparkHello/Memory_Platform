from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
import hashlib
import json
import math
import sqlite3
import threading

from pydantic import ValidationError

from app.memory.models import (
    ConversationBranchNode,
    CoreMemorySection,
    CoreMemorySectionHistory,
    CoreMemorySectionName,
    DecisionLog,
    DecisionLogAction,
    MemoryAction,
    MemoryMergeResult,
    MemoryOrigin,
    MemoryRecord,
    MemorySensitivity,
    MemorySourceExplanation,
    MemorySpace,
    MemoryStability,
    MemoryType,
    RecentContextSummary,
    RecentContextTurn,
    normalize_iso_text,
    normalize_memory_type,
    normalize_optional_text,
    new_memory_id,
    utc_now_iso,
)
from app.memory.classification import (
    normalize_classification_name,
    normalize_classification_names,
)
from app.memory.redaction import detect_text_sensitivity
from app.memory.purge_preview import purge_memory_ids_digest
from app.memory.utils import _parse_iso_datetime
from app.schema_migrations import (
    apply_schema_migrations,
    enable_wal_with_retry,
    validated_schema_version,
)
from app.memory.store import schema as _schema
from app.memory.store import temporal as _temporal
from app.memory.store import export_import as _export_import
from app.memory.store import crud as _crud
from app.memory.store import fts as _fts
from app.memory.store import merge as _merge
from app.memory.store import core_memory as _core_memory
from app.memory.store import conversation as _conversation
from app.memory.store import spaces as _spaces
from app.memory.store import digest as _digest
from app.memory.store import decision_logs as _decision_logs
from app.memory.store import lifecycle_purge as _lifecycle_purge
from app.memory.store import schema_ensure as _schema_ensure
from app.memory.store import migrations as _migrations
from app.memory.store.helpers import (
    _sensitivity_with_floor,
    _average_float,
    _bounded_float,
    _casefold_set,
    _coerce_float,
    _coerce_float_or_none,
    _coerce_int,
    _coerce_string_list,
    _core_section_audit_summaries,
    _earliest_datetime_text,
    _join_memory_contents,
    _json_like_safe,
    _json_string_list,
    _like_escape,
    _merge_core_section_audit_summaries,
    _merged_sensitivity,
    _merged_stability,
    _merged_type,
    _ordered_unique,
    _shared_value,
    _time_ripple_anchor,
    _time_ripple_profiles,
)
from app.memory.store.purge_ops import (
    PurgePreviewConflictError,
    _BatchPurgeSnapshot,
    _apply_batch_purge_snapshot,
    _build_batch_purge_snapshot,
    _decision_log_references_memory_ids,
    _derived_memory_dependency_closure,
    _insert_batch_purge_audit,
    _repair_purged_temporal_references,
    _scrub_purged_memory_artifacts,
)



from app.memory.store.constants import (
    _CONVERSATION_BRANCH_NODE_RETENTION_LIMIT,
    _DECISION_LOG_RETENTION_LIMIT,
    _MEMORY_DB_INIT_LOCK,
    _SENSITIVITY_RANK,
    _TIME_RIPPLE_MAX_CANDIDATES,
    _UNSET,
)


from app.memory.store.errors import RevisionConflictError
from app.memory.store.purge_ops import PurgePreviewConflictError  # re-export






def _serialize_memory_init(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with _MEMORY_DB_INIT_LOCK:
            return method(*args, **kwargs)

    return wrapped


class ClosingSQLiteConnection(sqlite3.Connection):
    """sqlite3 的 context manager 只负责 commit/rollback，不关闭连接。

    本项目的所有访问都写成 `with self._connect() as connection:`，
    因此在退出 with 块时兜底 close，避免连接句柄依赖 GC 回收。
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()




class MemoryStore:
    def __init__(self, database_path: str):
        self.database_path = database_path

    @_serialize_memory_init
    def init_db(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            enable_wal_with_retry(connection)
            # The thread lock above prevents duplicate work inside one process;
            # SQLite's write lock also serializes schema migration across
            # multiple workers/processes sharing the same database.
            connection.execute("BEGIN IMMEDIATE")
            validated_schema_version(
                connection,
                _migrations._MEMORY_SCHEMA_MIGRATIONS,
                schema_name="memory database",
            )
            self._create_tables(connection)
            self._run_migrations(connection)
            self._create_indexes(connection)
            self._rebuild_all_active_temporal_chains(connection=connection)

    def claim_chat_side_effect(
        self,
        *,
        kind: str,
        key: str,
        user_id: str,
        ttl_seconds: float,
    ) -> bool:
        """Atomically claim a retry-sensitive chat side effect.

        Only a hash of the turn key is persisted.  The unique constraint makes
        the guard effective across workers and process restarts; expired claims
        are removed while holding the same SQLite write lock used for insert.
        """

        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in {"activate", "recent_context", "ingest"}:
            raise ValueError("unknown chat side-effect kind")
        normalized_user = str(user_id or "default").strip() or "default"
        if not key:
            raise ValueError("chat side-effect key must not be empty")
        now = datetime.now(UTC)
        expires_at = now + timedelta(
            seconds=max(30.0, min(float(ttl_seconds), 86400.0))
        )
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM chat_side_effect_claims WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO chat_side_effect_claims (
                    kind, key_hash, user_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_kind,
                    key_hash,
                    normalized_user[:200],
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def release_chat_side_effect_claim(
        self,
        *,
        kind: str,
        key: str,
        user_id: str,
    ) -> None:
        normalized_kind = str(kind).strip().lower()
        normalized_user = str(user_id or "default").strip() or "default"
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM chat_side_effect_claims
                WHERE kind = ? AND key_hash = ? AND user_id = ?
                """,
                (normalized_kind, key_hash, normalized_user[:200]),
            )

    def enqueue_chat_finalize_job(
        self,
        *,
        job_id: str,
        user_id: str,
        kind: str,
        claim_key: str,
        payload: dict,
    ) -> bool:
        """Persist finalize intent before background work. Returns True if new."""
        import json as _json

        now = datetime.now(UTC).isoformat()
        normalized_user = str(user_id or "default").strip() or "default"
        normalized_kind = str(kind).strip().lower() or "ingest"
        body = _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(body) > 512_000:
            raise ValueError("chat finalize payload too large")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO chat_finalize_jobs (
                    id, user_id, kind, claim_key, payload_json, status,
                    attempts, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                """,
                (
                    job_id,
                    normalized_user[:200],
                    normalized_kind,
                    claim_key[:500],
                    body,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def mark_chat_finalize_job(
        self,
        *,
        job_id: str,
        status: str,
        last_error: str | None = None,
        bump_attempts: bool = False,
    ) -> bool:
        """Transition a finalize job. ``done`` is terminal: a late duplicate
        delivery can never flip a completed job back and trigger re-ingest.
        Completed jobs also drop their payload copy of the conversation turn.
        Returns True when a row actually changed."""
        if status not in {"pending", "running", "done", "failed"}:
            raise ValueError("invalid finalize job status")
        now = datetime.now(UTC).isoformat()
        attempts_sql = ", attempts = attempts + 1" if bump_attempts else ""
        payload_sql = ", payload_json = ''" if status == "done" else ""
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE chat_finalize_jobs
                SET status = ?, last_error = ?, updated_at = ?
                    {attempts_sql}{payload_sql}
                WHERE id = ? AND status != 'done'
                """,
                (status, (last_error or "")[:500] or None, now, job_id),
            )
            return cursor.rowcount == 1

    def prune_chat_finalize_jobs(self, *, keep_per_user: int = 5000) -> int:
        """Cap terminal (done/failed) outbox rows per user, newest first."""
        bounded = max(1, int(keep_per_user))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM chat_finalize_jobs
                WHERE status IN ('done', 'failed')
                  AND id NOT IN (
                    SELECT id FROM chat_finalize_jobs AS newer
                    WHERE newer.user_id = chat_finalize_jobs.user_id
                      AND newer.status IN ('done', 'failed')
                    ORDER BY newer.updated_at DESC, newer.id DESC
                    LIMIT ?
                  )
                """,
                (bounded,),
            )
            return int(cursor.rowcount or 0)

    def list_recoverable_chat_finalize_jobs(
        self,
        *,
        limit: int = 20,
        stale_running_seconds: float = 120.0,
    ) -> list[dict[str, object]]:
        """Return pending jobs and running jobs stuck past the stale window."""
        import json as _json
        from datetime import timedelta as _td

        now = datetime.now(UTC)
        stale_before = (now - _td(seconds=max(30.0, stale_running_seconds))).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, kind, claim_key, payload_json, status,
                       attempts, last_error, created_at, updated_at
                FROM chat_finalize_jobs
                WHERE status = 'pending'
                   OR (status = 'running' AND updated_at <= ?)
                ORDER BY created_at
                LIMIT ?
                """,
                (stale_before, max(1, min(int(limit), 100))),
            ).fetchall()
        jobs: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = _json.loads(str(row["payload_json"]))
            except _json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            jobs.append(
                {
                    "id": str(row["id"]),
                    "user_id": str(row["user_id"]),
                    "kind": str(row["kind"]),
                    "claim_key": str(row["claim_key"]),
                    "payload": payload,
                    "status": str(row["status"]),
                    "attempts": int(row["attempts"] or 0),
                    "last_error": row["last_error"],
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
        return jobs

    @staticmethod
    def _create_tables(connection: sqlite3.Connection) -> None:
        """幂等建表。老库已存在的表会被跳过；新列由 _run_migrations 补齐。"""
        _schema.create_tables(connection)

    @staticmethod
    def _create_indexes(connection: sqlite3.Connection) -> None:
        """幂等建索引。必须在 _run_migrations 之后执行。"""
        _schema.create_indexes(connection)

    @staticmethod
    def _run_migrations(connection: sqlite3.Connection) -> None:
        return _schema_ensure._run_migrations(connection)


    def create_memory(
        self,
        *,
        user_id: str,
        content: str,
        type: MemoryType = "semantic",
        importance: int = 1,
        confidence: float = 0.7,
        valence: float = 0.5,
        arousal: float = 0.3,
        source_message: str | None = None,
        source_conversation_id: str | None = None,
        origin: MemoryOrigin = "user_asserted",
        embedding_json: str | None = None,
        embedding_space_id: str | None = None,
        stability: MemoryStability = "stable",
        valid_from: str | None = None,
        valid_until: str | None = None,
        review_after: str | None = None,
        sensitivity: MemorySensitivity = "normal",
        evidence_memory_ids: list[str] | None = None,
        topics: list[str] | None = None,
        entities: list[str] | None = None,
        temporal_subject: str | None = None,
        temporal_predicate: str | None = None,
        space_ids: list[str] | None = None,
        decay_lambda: float | None = None,
        final_matcher: Callable[[list[MemoryRecord]], MemoryRecord | None] | None = None,
    ) -> MemoryRecord:
        return _crud.create_memory(self, user_id=user_id, content=content, type=type, importance=importance, confidence=confidence, valence=valence, arousal=arousal, source_message=source_message, source_conversation_id=source_conversation_id, origin=origin, embedding_json=embedding_json, embedding_space_id=embedding_space_id, stability=stability, valid_from=valid_from, valid_until=valid_until, review_after=review_after, sensitivity=sensitivity, evidence_memory_ids=evidence_memory_ids, topics=topics, entities=entities, temporal_subject=temporal_subject, temporal_predicate=temporal_predicate, space_ids=space_ids, decay_lambda=decay_lambda, final_matcher=final_matcher)


    def update_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
        content: str,
        type: MemoryType,
        importance: int,
        confidence: float,
        valence: float,
        arousal: float,
        source_message: str | None = None,
        source_conversation_id: str | None = None,
        embedding_json: str | None = None,
        embedding_space_id: object = _UNSET,
        stability: MemoryStability = "stable",
        valid_from: object = _UNSET,
        valid_until: object = _UNSET,
        review_after: str | None = None,
        sensitivity: MemorySensitivity = "normal",
        evidence_memory_ids: list[str] | None = None,
        topics: list[str] | None = None,
        entities: list[str] | None = None,
        temporal_subject: object = _UNSET,
        temporal_predicate: object = _UNSET,
        status: str | None = None,
        decay_lambda: object = _UNSET,
        expected_revision: int | None = None,
        replacement_space_ids: list[str] | None = None,
        replacement_space_names: list[str] | None = None,
    ) -> MemoryRecord | None:
        return _crud.update_memory(self, memory_id=memory_id, user_id=user_id, content=content, type=type, importance=importance, confidence=confidence, valence=valence, arousal=arousal, source_message=source_message, source_conversation_id=source_conversation_id, embedding_json=embedding_json, embedding_space_id=embedding_space_id, stability=stability, valid_from=valid_from, valid_until=valid_until, review_after=review_after, sensitivity=sensitivity, evidence_memory_ids=evidence_memory_ids, topics=topics, entities=entities, temporal_subject=temporal_subject, temporal_predicate=temporal_predicate, status=status, decay_lambda=decay_lambda, expected_revision=expected_revision, replacement_space_ids=replacement_space_ids, replacement_space_names=replacement_space_names)


    def get_memory(self, *, memory_id: str, user_id: str) -> MemoryRecord | None:
        return _crud.get_memory(self, memory_id=memory_id, user_id=user_id)


    def list_memory_timeline(
        self,
        *,
        user_id: str,
        subject: str,
        predicate: str | None = None,
        include_archived: bool = False,
    ) -> list[MemoryRecord]:
        return _crud.list_memory_timeline(self, user_id=user_id, subject=subject, predicate=predicate, include_archived=include_archived)


    def restore_temporal_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
    ) -> MemoryRecord | None:
        return _temporal.restore_temporal_memory(self, memory_id=memory_id, user_id=user_id)


    def list_memories(
        self,
        *,
        user_id: str,
        limit: int = 200,
        status: str | None = None,
        include_lifecycle_archived: bool = False,
    ) -> list[MemoryRecord]:
        return _crud.list_memories(self, user_id=user_id, limit=limit, status=status, include_lifecycle_archived=include_lifecycle_archived)


    def list_memories_for_resolution(self, *, user_id: str) -> list[MemoryRecord]:
        return _crud.list_memories_for_resolution(self, user_id=user_id)


    def memory_recall_snapshot(
        self,
        *,
        user_id: str,
        page_size: int = 500,
    ) -> Iterator[Callable[[], Iterator[list[MemoryRecord]]]]:
        return _crud.memory_recall_snapshot(self, user_id=user_id, page_size=page_size)


    def keyword_candidate_memories(
        self,
        *,
        user_id: str,
        terms: list[str],
    ) -> list[MemoryRecord] | None:
        """大库时用 FTS5 索引生成关键词候选；返回 None 表示走全表扫描。"""
        return _fts.keyword_candidate_memories(self, user_id=user_id, terms=terms)


    def list_all_memories_for_export(
        self,
        *,
        user_id: str,
        archived: bool,
        page_size: int = 500,
    ) -> list[MemoryRecord]:
        return _export_import.list_all_memories_for_export(self, user_id=user_id, archived=archived, page_size=page_size)


    def read_memory_export_snapshot(
        self,
        *,
        user_id: str,
        include_deleted: bool = True,
        page_size: int = 500,
    ) -> dict[str, list[object]]:
        return _export_import.read_memory_export_snapshot(self, user_id=user_id, include_deleted=include_deleted, page_size=page_size)


    def read_memory_selection_export_snapshot(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> dict[str, list[object] | list[str]]:
        return _export_import.read_memory_selection_export_snapshot(self, user_id=user_id, memory_ids=memory_ids)


    def get_memories_max_updated_at(self, *, user_id: str) -> str | None:
        return _crud.get_memories_max_updated_at(self, user_id=user_id)


    def get_active_memory_count(self, *, user_id: str) -> int:
        return _crud.get_active_memory_count(self, user_id=user_id)


    def get_next_temporal_boundary(
        self,
        *,
        user_id: str,
        after: datetime,
    ) -> datetime | None:
        return _temporal.get_next_temporal_boundary(self, user_id=user_id, after=after)


    def list_archived_memories(
        self,
        *,
        user_id: str,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        return _crud.list_archived_memories(self, user_id=user_id, limit=limit)


    def list_core_memory_sections(
        self,
        *,
        user_id: str,
    ) -> list[CoreMemorySection]:
        return _core_memory.list_core_memory_sections(self, user_id=user_id)


    def get_core_memory_section(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName,
    ) -> CoreMemorySection | None:
        return _core_memory.get_core_memory_section(self, user_id=user_id, section=section)


    def upsert_core_memory_section(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName,
        content: str,
        evidence_memory_ids: list[str],
        confidence: float,
        expected_revision: int | None = None,
    ) -> tuple[MemoryAction, CoreMemorySection]:
        return _core_memory.upsert_core_memory_section(self, user_id=user_id, section=section, content=content, evidence_memory_ids=evidence_memory_ids, confidence=confidence, expected_revision=expected_revision)


    def archive_core_memory_section(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName,
        expected_revision: int | None = None,
    ) -> bool:
        return _core_memory.archive_core_memory_section(self, user_id=user_id, section=section, expected_revision=expected_revision)


    def list_core_memory_section_history(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName | None = None,
        limit: int | None = 50,
    ) -> list[CoreMemorySectionHistory]:
        return _core_memory.list_core_memory_section_history(self, user_id=user_id, section=section, limit=limit)


    def explain_memory_source(
        self,
        *,
        memory_id: str,
        user_id: str,
    ) -> MemorySourceExplanation | None:
        return _crud.explain_memory_source(self, memory_id=memory_id, user_id=user_id)


    def merge_memories(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
        content: str | None = None,
    ) -> MemoryMergeResult:
        return _merge.merge_memories(self, user_id=user_id, memory_ids=memory_ids, content=content)


    def get_recent_context_summary(
        self,
        *,
        user_id: str,
        conversation_id: str | None = None,
    ) -> RecentContextSummary | None:
        return _conversation.get_recent_context_summary(self, user_id=user_id, conversation_id=conversation_id)


    def get_recent_context_summary_for_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
    ) -> RecentContextSummary | None:
        return _conversation.get_recent_context_summary_for_conversation(self, user_id=user_id, conversation_id=conversation_id)


    def list_recent_context_summaries(
        self,
        *,
        user_id: str,
        limit: int | None = 20,
    ) -> list[RecentContextSummary]:
        return _conversation.list_recent_context_summaries(self, user_id=user_id, limit=limit)


    def upsert_recent_context_summary(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        summary: str,
    ) -> RecentContextSummary:
        return _conversation.upsert_recent_context_summary(self, user_id=user_id, conversation_id=conversation_id, summary=summary)


    def upsert_recent_context_state(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        summary: str,
        compressed_summary: str,
        recent_turns: list[RecentContextTurn],
        turn_count: int,
    ) -> RecentContextSummary:
        return _conversation.upsert_recent_context_state(self, user_id=user_id, conversation_id=conversation_id, summary=summary, compressed_summary=compressed_summary, recent_turns=recent_turns, turn_count=turn_count)


    def get_conversation_branch_node(
        self,
        *,
        user_id: str,
        history_fingerprint: str,
    ) -> ConversationBranchNode | None:
        return _conversation.get_conversation_branch_node(self, user_id=user_id, history_fingerprint=history_fingerprint)


    def list_conversation_branch_nodes(
        self,
        *,
        user_id: str,
        limit: int = 5000,
        archived: bool = False,
    ) -> list[ConversationBranchNode]:
        return _conversation.list_conversation_branch_nodes(self, user_id=user_id, limit=limit, archived=archived)


    def count_conversation_branch_nodes(
        self,
        *,
        user_id: str,
        archived: bool = False,
    ) -> int:
        return _conversation.count_conversation_branch_nodes(self, user_id=user_id, archived=archived)


    def archive_conversation_branch_subtree(
        self,
        *,
        node_id: str,
        user_id: str,
    ) -> int:
        return _conversation.archive_conversation_branch_subtree(self, node_id=node_id, user_id=user_id)


    def restore_conversation_branch_subtree(
        self,
        *,
        node_id: str,
        user_id: str,
    ) -> int:
        return _conversation.restore_conversation_branch_subtree(self, node_id=node_id, user_id=user_id)


    def upsert_conversation_branch_node(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        history_fingerprint: str,
        parent_history_fingerprint: str,
        turn_fingerprint: str,
        assistant_digest: str,
        summary: str,
        compressed_summary: str,
        recent_turns: list[RecentContextTurn],
        turn_count: int,
    ) -> ConversationBranchNode:
        return _conversation.upsert_conversation_branch_node(self, user_id=user_id, conversation_id=conversation_id, history_fingerprint=history_fingerprint, parent_history_fingerprint=parent_history_fingerprint, turn_fingerprint=turn_fingerprint, assistant_digest=assistant_digest, summary=summary, compressed_summary=compressed_summary, recent_turns=recent_turns, turn_count=turn_count)


    def archive_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
        expected_revision: int | None = None,
        return_revision: bool = False,
    ) -> bool | int:
        return _crud.archive_memory(self, memory_id=memory_id, user_id=user_id, expected_revision=expected_revision, return_revision=return_revision)


    def restore_memory(self, *, memory_id: str, user_id: str) -> MemoryRecord | None:
        return _crud.restore_memory(self, memory_id=memory_id, user_id=user_id)


    def update_memory_embedding(
        self,
        *,
        memory_id: str,
        user_id: str,
        embedding_json: str,
        embedding_space_id: str,
    ) -> bool:
        return _crud.update_memory_embedding(self, memory_id=memory_id, user_id=user_id, embedding_json=embedding_json, embedding_space_id=embedding_space_id)


    def archive_expired_memories(self, *, user_id: str) -> int:
        return _crud.archive_expired_memories(self, user_id=user_id)


    def preview_archived_memory_purge(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
    ) -> dict[str, object]:
        return _lifecycle_purge.preview_archived_memory_purge(self, memory_ids=memory_ids, user_id=user_id)


    def commit_archived_memory_purge(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
        expected_purge_memory_ids_digest: str,
        expected_purge_memory_count: int,
        expected_fingerprint: str,
        call_source: str = "rest_api",
    ) -> tuple[dict[str, object], DecisionLog]:
        return _lifecycle_purge.commit_archived_memory_purge(self, memory_ids=memory_ids, user_id=user_id, expected_purge_memory_ids_digest=expected_purge_memory_ids_digest, expected_purge_memory_count=expected_purge_memory_count, expected_fingerprint=expected_fingerprint, call_source=call_source)


    def purge_archived_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
        affected_core_sections: list[dict] | None = None,
        call_source: str = "rest_api",
    ) -> tuple[MemoryRecord, DecisionLog] | None:
        return _lifecycle_purge.purge_archived_memory(self, memory_id=memory_id, user_id=user_id, affected_core_sections=affected_core_sections, call_source=call_source)


    def list_purge_affected_core_sections(
        self,
        *,
        memory_id: str,
        user_id: str,
    ) -> list[CoreMemorySection]:
        return _lifecycle_purge.list_purge_affected_core_sections(self, memory_id=memory_id, user_id=user_id)


    def upsert_memory_space(self, *, user_id: str, name: str) -> MemorySpace:
        return _spaces.upsert_memory_space(self, user_id=user_id, name=name)


    def _upsert_memory_space_on_connection(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        display_name: str,
    ) -> MemorySpace:
        return _spaces._upsert_memory_space_on_connection(self, connection=connection, user_id=user_id, display_name=display_name)


    def prepare_memory_space_import(
        self,
        *,
        data: dict,
    ) -> dict[str, object] | None:
        return _export_import.prepare_memory_space_import(self, data=data)


    def import_memory_space(
        self,
        *,
        user_id: str,
        data: dict,
        overwrite: bool = False,
    ) -> tuple[str, MemorySpace | None, str | None]:
        return _export_import.import_memory_space(self, user_id=user_id, data=data, overwrite=overwrite)


    def list_memory_spaces(
        self,
        *,
        user_id: str,
        include_archived: bool = False,
    ) -> list[MemorySpace]:
        return _spaces.list_memory_spaces(self, user_id=user_id, include_archived=include_archived)


    def list_memory_space_summaries(self, *, user_id: str) -> list[dict]:
        return _spaces.list_memory_space_summaries(self, user_id=user_id)


    def get_memory_space(self, *, user_id: str, space_id: str) -> MemorySpace | None:
        return _spaces.get_memory_space(self, user_id=user_id, space_id=space_id)


    def list_memories_for_space(
        self,
        *,
        user_id: str,
        space_id: str,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        return _spaces.list_memories_for_space(self, user_id=user_id, space_id=space_id, limit=limit)


    def replace_memory_spaces(
        self,
        *,
        memory_id: str,
        user_id: str,
        space_ids: list[str],
        create_space_names: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> MemoryRecord | None:
        return _spaces.replace_memory_spaces(self, memory_id=memory_id, user_id=user_id, space_ids=space_ids, create_space_names=create_space_names, expected_revision=expected_revision)


    def plan_memory_import_ids(
        self,
        *,
        user_id: str,
        source_ids: list[str],
        rebind_all: bool = False,
    ) -> dict[str, str]:
        return _export_import.plan_memory_import_ids(self, user_id=user_id, source_ids=source_ids, rebind_all=rebind_all)


    @staticmethod
    def _plan_memory_import_ids_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        source_ids: list[str],
        rebind_all: bool,
    ) -> dict[str, str]:
        return _export_import._plan_memory_import_ids_on_connection(connection=connection, user_id=user_id, source_ids=source_ids, rebind_all=rebind_all)


    def filter_existing_memory_ids(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> set[str]:
        return _export_import.filter_existing_memory_ids(self, user_id=user_id, memory_ids=memory_ids)


    @staticmethod
    def _filter_existing_memory_ids_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory_ids: list[str],
    ) -> set[str]:
        return _export_import._filter_existing_memory_ids_on_connection(connection=connection, user_id=user_id, memory_ids=memory_ids)


    def prune_dangling_memory_references(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> int:
        return _export_import.prune_dangling_memory_references(self, user_id=user_id, memory_ids=memory_ids)


    @staticmethod
    def _prune_dangling_memory_references_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory_ids: list[str],
    ) -> int:
        return _export_import._prune_dangling_memory_references_on_connection(connection=connection, user_id=user_id, memory_ids=memory_ids)


    def restore_prepared_export(
        self,
        *,
        user_id: str,
        prepared_spaces: list[dict[str, object]],
        prepared_memories: list[tuple[str, MemoryRecord]],
        source_memory_ids: list[str],
        referenced_source_ids: list[str],
        recent_contexts: list[dict[str, object]],
        branch_nodes: list[dict[str, object]],
        exported_user_id: str,
        overwrite: bool,
        dry_run: bool = False,
    ) -> dict[str, object]:
        return _export_import.restore_prepared_export(self, user_id=user_id, prepared_spaces=prepared_spaces, prepared_memories=prepared_memories, source_memory_ids=source_memory_ids, referenced_source_ids=referenced_source_ids, recent_contexts=recent_contexts, branch_nodes=branch_nodes, exported_user_id=exported_user_id, overwrite=overwrite, dry_run=dry_run)


    def _plan_memory_space_imports_on_connection(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        prepared_spaces: list[dict[str, object]],
        overwrite: bool,
    ) -> tuple[list[dict[str, object]], dict[str, str]]:
        return _export_import._plan_memory_space_imports_on_connection(self, connection=connection, user_id=user_id, prepared_spaces=prepared_spaces, overwrite=overwrite)


    @staticmethod
    def _apply_memory_space_import_plan_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        plan: dict[str, object],
    ) -> None:
        return _export_import._apply_memory_space_import_plan_on_connection(connection=connection, user_id=user_id, plan=plan)


    @staticmethod
    def _restore_recent_context_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        prepared: dict[str, object],
        overwrite: bool,
    ) -> str:
        return _export_import._restore_recent_context_on_connection(connection=connection, user_id=user_id, prepared=prepared, overwrite=overwrite)


    @staticmethod
    def _restore_branch_node_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        prepared: dict[str, object],
        overwrite: bool,
    ) -> str:
        return _export_import._restore_branch_node_on_connection(connection=connection, user_id=user_id, prepared=prepared, overwrite=overwrite)


    def prepare_memory_import_record(
        self,
        *,
        user_id: str,
        data: dict,
        archived: int | None = None,
        space_id_map: dict[str, str] | None = None,
    ) -> MemoryRecord | None:
        return _export_import.prepare_memory_import_record(self, user_id=user_id, data=data, archived=archived, space_id_map=space_id_map)


    def import_memory_record(
        self,
        *,
        user_id: str,
        data: dict,
        overwrite: bool = False,
        archived: int | None = None,
        space_id_map: dict[str, str] | None = None,
        rebind_on_conflict: bool = True,
    ) -> tuple[str, MemoryRecord | None]:
        return _export_import.import_memory_record(self, user_id=user_id, data=data, overwrite=overwrite, archived=archived, space_id_map=space_id_map, rebind_on_conflict=rebind_on_conflict)


    def _import_prepared_memory_record_on_connection(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory: MemoryRecord,
        overwrite: bool,
        rebind_on_conflict: bool,
    ) -> tuple[str, MemoryRecord | None]:
        return _export_import._import_prepared_memory_record_on_connection(self, connection=connection, user_id=user_id, memory=memory, overwrite=overwrite, rebind_on_conflict=rebind_on_conflict)


    def mark_memories_used(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
        time_ripple_delta: float = 0.0,
        time_ripple_window_hours: int = 48,
    ) -> str | None:
        return _crud.mark_memories_used(self, memory_ids=memory_ids, user_id=user_id, time_ripple_delta=time_ripple_delta, time_ripple_window_hours=time_ripple_window_hours)


    def touch_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
        time_ripple_delta: float = 0.0,
        time_ripple_window_hours: int = 48,
    ) -> None:
        return _crud.touch_memory(self, memory_id=memory_id, user_id=user_id, time_ripple_delta=time_ripple_delta, time_ripple_window_hours=time_ripple_window_hours)


    def _apply_time_ripple(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        seed_ids: list[str],
        used_at: str,
        delta: float,
        window_hours: int,
    ) -> None:
        return _temporal._apply_time_ripple(self, connection=connection, user_id=user_id, seed_ids=seed_ids, used_at=used_at, delta=delta, window_hours=window_hours)


    def list_undigested_memories(
        self, *, user_id: str, limit: int = 10, include_sensitive: bool = False
    ) -> list[MemoryRecord]:
        return _digest.list_undigested_memories(self, user_id=user_id, limit=limit, include_sensitive=include_sensitive)


    def get_digest_source_memories(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
        include_sensitive: bool = False,
    ) -> list[MemoryRecord]:
        return _digest.get_digest_source_memories(self, memory_ids=memory_ids, user_id=user_id, include_sensitive=include_sensitive)


    def apply_memory_digest(
        self,
        *,
        user_id: str,
        source_ids: list[str],
        resolved_ids: list[str],
        reflection: str = "",
        reflection_valence: float = 0.5,
        reflection_arousal: float = 0.3,
        feel: str = "",
        feel_valence: float = 0.5,
        feel_arousal: float = 0.4,
        include_sensitive: bool = False,
    ) -> tuple[list[MemoryRecord], int]:
        return _digest.apply_memory_digest(self, user_id=user_id, source_ids=source_ids, resolved_ids=resolved_ids, reflection=reflection, reflection_valence=reflection_valence, reflection_arousal=reflection_arousal, feel=feel, feel_valence=feel_valence, feel_arousal=feel_arousal, include_sensitive=include_sensitive)


    @staticmethod
    def _validated_digest_source_rows(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        source_ids: list[str],
        include_sensitive: bool = False,
    ) -> list[sqlite3.Row]:
        return _digest._validated_digest_source_rows(connection=connection, user_id=user_id, source_ids=source_ids, include_sensitive=include_sensitive)


    def mark_digested(self, *, memory_ids: list[str], user_id: str) -> None:
        return _digest.mark_digested(self, memory_ids=memory_ids, user_id=user_id)


    def update_memory_statuses(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
        status: str,
    ) -> int:
        return _crud.update_memory_statuses(self, memory_ids=memory_ids, user_id=user_id, status=status)


    def _rebuild_temporal_key(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        temporal_subject: str | None,
        temporal_predicate: str | None,
    ) -> int:
        return _temporal._rebuild_temporal_key(self, connection=connection, user_id=user_id, temporal_subject=temporal_subject, temporal_predicate=temporal_predicate)


    def _rebuild_all_active_temporal_chains(
        self,
        *,
        connection: sqlite3.Connection,
    ) -> int:
        return _temporal._rebuild_all_active_temporal_chains(self, connection=connection)


    def _detach_temporal_position(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory: MemoryRecord,
    ) -> None:
        return _temporal._detach_temporal_position(self, connection=connection, user_id=user_id, memory=memory)


    def _apply_temporal_invalidation(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        new_memory: MemoryRecord,
    ) -> list[str]:
        return _temporal._apply_temporal_invalidation(self, connection=connection, user_id=user_id, new_memory=new_memory)


    @staticmethod
    def _temporal_snapshot(row: sqlite3.Row) -> dict:
        return _temporal._temporal_snapshot(row)


    def _insert_decision_log(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str = "default",
        conversation_id: str | None,
        candidate_json: str,
        decision: DecisionLogAction,
        reason: str,
    ) -> DecisionLog:
        return _decision_logs._insert_decision_log(self, connection=connection, user_id=user_id, conversation_id=conversation_id, candidate_json=candidate_json, decision=decision, reason=reason)


    def create_decision_log(
        self,
        *,
        user_id: str = "default",
        conversation_id: str | None,
        candidate_json: str,
        decision: DecisionLogAction,
        reason: str,
    ) -> DecisionLog:
        return _decision_logs.create_decision_log(self, user_id=user_id, conversation_id=conversation_id, candidate_json=candidate_json, decision=decision, reason=reason)


    def list_decision_logs(
        self,
        *,
        user_id: str | None = None,
        conversation_id: str | None = None,
        memory_id: str | None = None,
        limit: int | None = 100,
    ) -> list[DecisionLog]:
        return _decision_logs.list_decision_logs(self, user_id=user_id, conversation_id=conversation_id, memory_id=memory_id, limit=limit)


    def _create_core_memory_section_history(
        self,
        *,
        connection: sqlite3.Connection | None,
        section: CoreMemorySection,
        replaced_at: str,
    ) -> None:
        return _core_memory._create_core_memory_section_history(self, connection=connection, section=section, replaced_at=replaced_at)


    def _insert_memory_row(
        self,
        *,
        connection: sqlite3.Connection,
        memory: MemoryRecord,
    ) -> None:
        return _crud._insert_memory_row(self, connection=connection, memory=memory)


    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _archive_duplicate_recent_context_summaries(connection: sqlite3.Connection) -> None:
        return _conversation._archive_duplicate_recent_context_summaries(connection)


    @staticmethod
    def _ensure_memories_usage_columns(connection: sqlite3.Connection) -> None:
        return _schema_ensure._ensure_memories_usage_columns(connection)


    @staticmethod
    def _ensure_memories_embedding_space_column(connection: sqlite3.Connection) -> None:
        return _schema_ensure._ensure_memories_embedding_space_column(connection)


    @staticmethod
    def _ensure_core_memory_sections_columns(connection: sqlite3.Connection) -> None:
        return _core_memory._ensure_core_memory_sections_columns(connection)


    @staticmethod
    def _ensure_revision_columns(connection: sqlite3.Connection) -> None:
        return _schema_ensure._ensure_revision_columns(connection)


    @staticmethod
    def _merge_duplicate_active_core_sections(connection: sqlite3.Connection) -> None:
        return _core_memory._merge_duplicate_active_core_sections(connection)


    @staticmethod
    def _ensure_recent_context_summary_columns(connection: sqlite3.Connection) -> None:
        return _conversation._ensure_recent_context_summary_columns(connection)


    @staticmethod
    def _ensure_decision_logs_user_id(connection: sqlite3.Connection) -> None:
        return _decision_logs._ensure_decision_logs_user_id(connection)


    def _space_ids_for_memory_ids(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> dict[str, list[str]]:
        return _spaces._space_ids_for_memory_ids(self, user_id=user_id, memory_ids=memory_ids)


    @staticmethod
    def _space_ids_for_memory_ids_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory_ids: list[str],
    ) -> dict[str, list[str]]:
        return _spaces._space_ids_for_memory_ids_on_connection(connection=connection, user_id=user_id, memory_ids=memory_ids)


    @staticmethod
    def _replace_memory_space_links(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory_id: str,
        space_ids: list[str],
        created_at: str,
    ) -> None:
        return _spaces._replace_memory_space_links(connection=connection, user_id=user_id, memory_id=memory_id, space_ids=space_ids, created_at=created_at)


    @staticmethod
    def _filter_existing_space_ids(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        space_ids: list[str],
    ) -> list[str]:
        return _spaces._filter_existing_space_ids(connection=connection, user_id=user_id, space_ids=space_ids)


    @staticmethod
    def _validate_space_ids(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        space_ids: list[str],
    ) -> None:
        return _spaces._validate_space_ids(connection=connection, user_id=user_id, space_ids=space_ids)


    def _rows_to_memories(self, rows: list[sqlite3.Row]) -> list[MemoryRecord]:
        return _crud._rows_to_memories(self, rows)


    def _rows_to_memories_on_connection(
        self,
        *,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> list[MemoryRecord]:
        return _crud._rows_to_memories_on_connection(self, connection=connection, rows=rows)


    def _row_to_memory(
        self,
        row: sqlite3.Row,
        *,
        space_ids: list[str] | None = None,
    ) -> MemoryRecord:
        return _crud._row_to_memory(self, row, space_ids=space_ids)


    @staticmethod
    def _row_to_memory_space(row: sqlite3.Row) -> MemorySpace:
        return _spaces._row_to_memory_space(row)


    @staticmethod
    def _row_to_core_memory_section(row: sqlite3.Row) -> CoreMemorySection:
        return _core_memory._row_to_core_memory_section(row)


    @staticmethod
    def _row_to_core_memory_section_history(row: sqlite3.Row) -> CoreMemorySectionHistory:
        return _core_memory._row_to_core_memory_section_history(row)


    @staticmethod
    def _row_to_recent_context_summary(row: sqlite3.Row) -> RecentContextSummary:
        return _conversation._row_to_recent_context_summary(row)


    @staticmethod
    def _row_to_conversation_branch_node(
        row: sqlite3.Row,
    ) -> ConversationBranchNode:
        return _conversation._row_to_conversation_branch_node(row)




