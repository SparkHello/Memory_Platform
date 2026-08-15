from collections.abc import Callable, Iterator
from datetime import datetime
from functools import wraps
from pathlib import Path
import sqlite3

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
)
from app.schema_migrations import (
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
from app.memory.store import chat_finalize as _chat_finalize
from app.memory.store import lifecycle_purge as _lifecycle_purge
from app.memory.store import schema_ensure as _schema_ensure
from app.memory.store import migrations as _migrations
from app.memory.store.constants import (
    _MEMORY_DB_INIT_LOCK,
    _UNSET,
)
from app.sqlite_util import ClosingSQLiteConnection


def _serialize_memory_init(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with _MEMORY_DB_INIT_LOCK:
            return method(*args, **kwargs)

    return wrapped


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
            _temporal._rebuild_all_active_temporal_chains(connection=connection)

    def claim_chat_side_effect(
        self,
        *,
        kind: str,
        key: str,
        user_id: str,
        ttl_seconds: float,
    ) -> bool:
        return _chat_finalize.claim_chat_side_effect(
            self, kind=kind, key=key, user_id=user_id, ttl_seconds=ttl_seconds
        )

    def release_chat_side_effect_claim(
        self,
        *,
        kind: str,
        key: str,
        user_id: str,
    ) -> None:
        return _chat_finalize.release_chat_side_effect_claim(
            self, kind=kind, key=key, user_id=user_id
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
        return _chat_finalize.enqueue_chat_finalize_job(
            self,
            job_id=job_id,
            user_id=user_id,
            kind=kind,
            claim_key=claim_key,
            payload=payload,
        )

    def mark_chat_finalize_job(
        self,
        *,
        job_id: str,
        status: str,
        last_error: str | None = None,
        bump_attempts: bool = False,
    ) -> bool:
        return _chat_finalize.mark_chat_finalize_job(
            self,
            job_id=job_id,
            status=status,
            last_error=last_error,
            bump_attempts=bump_attempts,
        )

    def prune_chat_finalize_jobs(self, *, keep_per_user: int = 5000) -> int:
        return _chat_finalize.prune_chat_finalize_jobs(
            self, keep_per_user=keep_per_user
        )

    def list_recoverable_chat_finalize_jobs(
        self,
        *,
        limit: int = 20,
        stale_running_seconds: float = 120.0,
    ) -> list[dict[str, object]]:
        return _chat_finalize.list_recoverable_chat_finalize_jobs(
            self, limit=limit, stale_running_seconds=stale_running_seconds
        )

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


    def list_memory_space_summaries(
        self,
        *,
        user_id: str,
        include_archived: bool = False,
    ) -> list[dict]:
        return _spaces.list_memory_space_summaries(
            self, user_id=user_id, include_archived=include_archived
        )


    def get_memory_space(
        self,
        *,
        user_id: str,
        space_id: str,
        include_archived: bool = False,
    ) -> MemorySpace | None:
        return _spaces.get_memory_space(
            self,
            user_id=user_id,
            space_id=space_id,
            include_archived=include_archived,
        )

    def create_memory_space(
        self,
        *,
        user_id: str,
        name: str,
        color: str | None = None,
        description: str | None = None,
        sort_order: int | None = None,
    ) -> MemorySpace:
        return _spaces.create_memory_space(
            self,
            user_id=user_id,
            name=name,
            color=color,
            description=description,
            sort_order=sort_order,
        )

    def update_memory_space(
        self,
        *,
        user_id: str,
        space_id: str,
        name: str | None = None,
        color: str | None = None,
        description: str | None = None,
        sort_order: int | None = None,
        update_name: bool = False,
        update_color: bool = False,
        update_description: bool = False,
        update_sort_order: bool = False,
    ) -> MemorySpace | None:
        return _spaces.update_memory_space(
            self,
            user_id=user_id,
            space_id=space_id,
            name=name,
            color=color,
            description=description,
            sort_order=sort_order,
            update_name=update_name,
            update_color=update_color,
            update_description=update_description,
            update_sort_order=update_sort_order,
        )

    def set_memory_space_archived(
        self,
        *,
        user_id: str,
        space_id: str,
        archived: bool,
    ) -> MemorySpace | None:
        return _spaces.set_memory_space_archived(
            self, user_id=user_id, space_id=space_id, archived=archived
        )

    def delete_memory_space(self, *, user_id: str, space_id: str) -> str:
        return _spaces.delete_memory_space(self, user_id=user_id, space_id=space_id)


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


    def filter_existing_memory_ids(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> set[str]:
        return _export_import.filter_existing_memory_ids(self, user_id=user_id, memory_ids=memory_ids)


    def prune_dangling_memory_references(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> int:
        return _export_import.prune_dangling_memory_references(self, user_id=user_id, memory_ids=memory_ids)


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


    def mark_memories_used(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
    ) -> str | None:
        return _crud.mark_memories_used(self, memory_ids=memory_ids, user_id=user_id)


    def touch_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
    ) -> None:
        return _crud.touch_memory(self, memory_id=memory_id, user_id=user_id)


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


    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection
