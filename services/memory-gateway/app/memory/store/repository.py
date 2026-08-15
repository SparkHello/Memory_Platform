"""Composed MemoryStore backed directly by focused repository functions.

Each mixin binds the domain function itself as a method.  This keeps one
callable/signature per operation while preserving the long-standing
``MemoryStore`` construction and call surface.
"""

from __future__ import annotations

from functools import wraps
from pathlib import Path
import sqlite3

from app.memory.store import chat_finalize as _chat_finalize
from app.memory.store import conversation as _conversation
from app.memory.store import core_memory as _core_memory
from app.memory.store import crud as _crud
from app.memory.store import decision_logs as _decision_logs
from app.memory.store import digest as _digest
from app.memory.store import export_import as _export_import
from app.memory.store import fts as _fts
from app.memory.store import lifecycle_purge as _lifecycle_purge
from app.memory.store import merge as _merge
from app.memory.store import migrations as _migrations
from app.memory.store import schema as _schema
from app.memory.store import schema_ensure as _schema_ensure
from app.memory.store import spaces as _spaces
from app.memory.store import temporal as _temporal
from app.memory.store.constants import _MEMORY_DB_INIT_LOCK
from app.schema_migrations import enable_wal_with_retry, validated_schema_version
from app.sqlite_util import ClosingSQLiteConnection


def _serialize_memory_init(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with _MEMORY_DB_INIT_LOCK:
            return method(*args, **kwargs)

    return wrapped


class ChatFinalizeRepository:
    claim_chat_side_effect = _chat_finalize.claim_chat_side_effect
    release_chat_side_effect_claim = _chat_finalize.release_chat_side_effect_claim
    enqueue_chat_finalize_job = _chat_finalize.enqueue_chat_finalize_job
    mark_chat_finalize_job = _chat_finalize.mark_chat_finalize_job
    claim_chat_finalize_job = _chat_finalize.claim_chat_finalize_job
    prune_chat_finalize_jobs = _chat_finalize.prune_chat_finalize_jobs


class MemoryCrudRepository:
    create_memory = _crud.create_memory
    update_memory = _crud.update_memory
    get_memory = _crud.get_memory
    list_memory_timeline = _crud.list_memory_timeline
    list_memories = _crud.list_memories
    list_memories_for_resolution = _crud.list_memories_for_resolution
    memory_recall_snapshot = _crud.memory_recall_snapshot
    get_memories_max_updated_at = _crud.get_memories_max_updated_at
    get_active_memory_count = _crud.get_active_memory_count
    list_archived_memories = _crud.list_archived_memories
    explain_memory_source = _crud.explain_memory_source
    archive_memory = _crud.archive_memory
    restore_memory = _crud.restore_memory
    update_memory_embedding = _crud.update_memory_embedding
    archive_expired_memories = _crud.archive_expired_memories
    mark_memories_used = _crud.mark_memories_used
    update_memory_statuses = _crud.update_memory_statuses


class MemoryTemporalRepository:
    restore_temporal_memory = _temporal.restore_temporal_memory
    get_next_temporal_boundary = _temporal.get_next_temporal_boundary


class MemoryFtsRepository:
    keyword_candidate_memories = _fts.keyword_candidate_memories


class MemoryExportRepository:
    list_all_memories_for_export = _export_import.list_all_memories_for_export
    read_memory_export_snapshot = _export_import.read_memory_export_snapshot
    read_memory_selection_export_snapshot = (
        _export_import.read_memory_selection_export_snapshot
    )
    prepare_memory_space_import = _export_import.prepare_memory_space_import
    import_memory_space = _export_import.import_memory_space
    plan_memory_import_ids = _export_import.plan_memory_import_ids
    filter_existing_memory_ids = _export_import.filter_existing_memory_ids
    prune_dangling_memory_references = (
        _export_import.prune_dangling_memory_references
    )
    restore_prepared_export = _export_import.restore_prepared_export
    prepare_memory_import_record = _export_import.prepare_memory_import_record
    import_memory_record = _export_import.import_memory_record


class CoreMemoryRepository:
    list_core_memory_sections = _core_memory.list_core_memory_sections
    get_core_memory_section = _core_memory.get_core_memory_section
    upsert_core_memory_section = _core_memory.upsert_core_memory_section
    archive_core_memory_section = _core_memory.archive_core_memory_section
    list_core_memory_section_history = (
        _core_memory.list_core_memory_section_history
    )


class MemoryMergeRepository:
    merge_memories = _merge.merge_memories


class ConversationRepository:
    get_recent_context_summary = _conversation.get_recent_context_summary
    get_recent_context_summary_for_conversation = (
        _conversation.get_recent_context_summary_for_conversation
    )
    list_recent_context_summaries = _conversation.list_recent_context_summaries
    upsert_recent_context_summary = _conversation.upsert_recent_context_summary
    upsert_recent_context_state = _conversation.upsert_recent_context_state
    get_conversation_branch_node = _conversation.get_conversation_branch_node
    list_conversation_branch_nodes = _conversation.list_conversation_branch_nodes
    count_conversation_branch_nodes = _conversation.count_conversation_branch_nodes
    archive_conversation_branch_subtree = (
        _conversation.archive_conversation_branch_subtree
    )
    restore_conversation_branch_subtree = (
        _conversation.restore_conversation_branch_subtree
    )
    upsert_conversation_branch_node = (
        _conversation.upsert_conversation_branch_node
    )


class MemoryPurgeRepository:
    preview_archived_memory_purge = (
        _lifecycle_purge.preview_archived_memory_purge
    )
    commit_archived_memory_purge = _lifecycle_purge.commit_archived_memory_purge
    purge_archived_memory = _lifecycle_purge.purge_archived_memory
    list_purge_affected_core_sections = (
        _lifecycle_purge.list_purge_affected_core_sections
    )


class MemorySpaceRepository:
    upsert_memory_space = _spaces.upsert_memory_space
    list_memory_spaces = _spaces.list_memory_spaces
    list_memory_space_summaries = _spaces.list_memory_space_summaries
    get_memory_space = _spaces.get_memory_space
    create_memory_space = _spaces.create_memory_space
    update_memory_space = _spaces.update_memory_space
    set_memory_space_archived = _spaces.set_memory_space_archived
    delete_memory_space = _spaces.delete_memory_space
    list_memories_for_space = _spaces.list_memories_for_space
    replace_memory_spaces = _spaces.replace_memory_spaces


class MemoryDigestRepository:
    list_undigested_memories = _digest.list_undigested_memories
    get_digest_source_memories = _digest.get_digest_source_memories
    apply_memory_digest = _digest.apply_memory_digest


class DecisionLogRepository:
    create_decision_log = _decision_logs.create_decision_log
    list_decision_logs = _decision_logs.list_decision_logs


class MemorySchemaRepository:
    _create_tables = staticmethod(_schema.create_tables)
    _create_indexes = staticmethod(_schema.create_indexes)
    _run_migrations = staticmethod(_schema_ensure._run_migrations)


class MemoryStore(
    ChatFinalizeRepository,
    MemoryCrudRepository,
    MemoryTemporalRepository,
    MemoryFtsRepository,
    MemoryExportRepository,
    CoreMemoryRepository,
    MemoryMergeRepository,
    ConversationRepository,
    MemoryPurgeRepository,
    MemorySpaceRepository,
    MemoryDigestRepository,
    DecisionLogRepository,
    MemorySchemaRepository,
):
    def __init__(self, database_path: str):
        self.database_path = database_path

    @_serialize_memory_init
    def init_db(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            enable_wal_with_retry(connection)
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection
