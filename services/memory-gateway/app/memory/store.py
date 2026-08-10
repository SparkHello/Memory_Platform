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
from app.memory.redaction import detect_text_sensitivity
from app.memory.purge_preview import purge_memory_ids_digest
from app.memory.utils import _parse_iso_datetime
from app.schema_migrations import (
    apply_schema_migrations,
    enable_wal_with_retry,
    validated_schema_version,
)


_UNSET = object()
_TIME_RIPPLE_MAX_CANDIDATES = 100
_SENSITIVITY_RANK = {"normal": 0, "private": 1, "sensitive": 2}
# 每用户决策日志保留上限：超出后按创建时间从旧到新裁剪，避免全库无界增长。
_DECISION_LOG_RETENTION_LIMIT = 5000
# Branch snapshots are an operational context index, not an unlimited transcript
# archive. Old nodes can always fall back to the visible history sent by the client.
_CONVERSATION_BRANCH_NODE_RETENTION_LIMIT = 5000
_MEMORY_DB_INIT_LOCK = threading.Lock()


class RevisionConflictError(RuntimeError):
    """A caller attempted to mutate a stale persisted representation."""

    def __init__(
        self,
        *,
        resource: str,
        resource_id: str,
        expected_revision: int,
        current_revision: int,
    ) -> None:
        super().__init__(f"stale {resource} revision")
        self.resource = resource
        self.resource_id = resource_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class PurgePreviewConflictError(RuntimeError):
    """A batch purge preview cannot be safely created or committed."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        missing_memory_ids: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.missing_memory_ids = missing_memory_ids or []


@dataclass
class _BatchPurgeSnapshot:
    requested_memory_ids: list[str]
    purge_memory_ids: list[str]
    dependent_memory_ids: list[str]
    affected_core_memory_sections: list[dict[str, object]]
    fingerprint: str
    effects: dict[str, int]
    affected_memory_rows: list[sqlite3.Row]
    affected_core_rows: list[sqlite3.Row]
    affected_core_history_rows: list[sqlite3.Row]
    affected_decision_log_rows: list[sqlite3.Row]
    temporal_repairs: list[tuple[str, str | None, str | None]]


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


def _sensitivity_with_floor(
    *,
    declared: MemorySensitivity,
    content: str,
    source_message: str | None = None,
    entities: list[str] | None = None,
) -> MemorySensitivity:
    detected = detect_text_sensitivity(
        "\n".join(
            part
            for part in (content, source_message or "", *(entities or []))
            if part
        )
    )
    return max((declared, detected), key=_SENSITIVITY_RANK.__getitem__)


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
                _MEMORY_SCHEMA_MIGRATIONS,
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

    @staticmethod
    def _create_tables(connection: sqlite3.Connection) -> None:
        """幂等建表。老库已存在的表会被跳过；新列由 _run_migrations 补齐。"""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                content TEXT,
                type TEXT,
                importance INTEGER,
                confidence REAL,
                valence REAL DEFAULT 0.5,
                arousal REAL DEFAULT 0.3,
                source_message TEXT,
                source_conversation_id TEXT,
                origin TEXT DEFAULT 'user_asserted',
                embedding_json TEXT,
                embedding_space_id TEXT,
                last_used_at TEXT,
                usage_count REAL DEFAULT 0.0,
                stability TEXT DEFAULT 'stable',
                valid_from TEXT,
                valid_until TEXT,
                review_after TEXT,
                sensitivity TEXT DEFAULT 'normal',
                evidence_memory_ids_json TEXT,
                topics_json TEXT,
                entities_json TEXT,
                temporal_subject TEXT,
                temporal_predicate TEXT,
                status TEXT DEFAULT 'dynamic',
                digested INTEGER DEFAULT 0,
                decay_lambda REAL,
                supersedes TEXT,
                superseded_by TEXT,
                created_at TEXT,
                updated_at TEXT,
                archived_at TEXT,
                archived INTEGER DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_spaces (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                name TEXT,
                normalized_name TEXT,
                created_at TEXT,
                updated_at TEXT,
                archived INTEGER DEFAULT 0,
                UNIQUE(user_id, normalized_name)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_space_links (
                user_id TEXT,
                memory_id TEXT,
                space_id TEXT,
                created_at TEXT,
                PRIMARY KEY(user_id, memory_id, space_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_decision_logs (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                conversation_id TEXT,
                candidate_json TEXT,
                decision TEXT,
                reason TEXT,
                created_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS core_memory_sections (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                section TEXT,
                content TEXT,
                evidence_memory_ids_json TEXT,
                confidence REAL,
                version INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT,
                archived INTEGER DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS core_memory_section_history (
                id TEXT PRIMARY KEY,
                core_memory_section_id TEXT,
                user_id TEXT,
                section TEXT,
                content TEXT,
                evidence_memory_ids_json TEXT,
                confidence REAL,
                version INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT,
                replaced_at TEXT,
                revision INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recent_context_summaries (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                conversation_id TEXT,
                summary TEXT,
                compressed_summary TEXT DEFAULT '',
                recent_turns_json TEXT DEFAULT '[]',
                turn_count INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                archived INTEGER DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_branch_nodes (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                conversation_id TEXT,
                history_fingerprint TEXT NOT NULL,
                parent_history_fingerprint TEXT DEFAULT '',
                turn_fingerprint TEXT NOT NULL,
                assistant_digest TEXT NOT NULL,
                summary TEXT DEFAULT '',
                compressed_summary TEXT DEFAULT '',
                recent_turns_json TEXT DEFAULT '[]',
                turn_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived INTEGER DEFAULT 0
            )
            """
        )

    @staticmethod
    def _create_indexes(connection: sqlite3.Connection) -> None:
        """幂等建索引。必须在 _run_migrations 之后执行，
        因为部分索引引用了迁移补齐的列（如 memories.temporal_subject）。"""
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_user_archived ON memories(user_id, archived)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_user_archived_importance_updated
            ON memories(user_id, archived, importance DESC, updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_user_archived_archived_updated
            ON memories(user_id, archived, archived_at DESC, updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_temporal_key
            ON memories(user_id, temporal_subject, temporal_predicate, archived)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_spaces_user_archived
            ON memory_spaces(user_id, archived, updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_space_links_user_space
            ON memory_space_links(user_id, space_id, memory_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_decision_logs_user_conversation
            ON memory_decision_logs(user_id, conversation_id, created_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_core_memory_sections_user_archived
            ON core_memory_sections(user_id, archived, section)
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_core_memory_user_section_active
            ON core_memory_sections(user_id, section)
            WHERE archived = 0
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_core_memory_history_user_section
            ON core_memory_section_history(user_id, section, replaced_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recent_context_user_updated
            ON recent_context_summaries(user_id, archived, updated_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recent_context_user_conversation_archived_updated
            ON recent_context_summaries(user_id, conversation_id, archived, updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recent_context_user_archived_updated_desc
            ON recent_context_summaries(user_id, archived, updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_recent_context_user_conversation_active
            ON recent_context_summaries(user_id, conversation_id)
            WHERE archived = 0 AND conversation_id IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_recent_context_user_global_active
            ON recent_context_summaries(user_id)
            WHERE archived = 0 AND conversation_id IS NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_branch_user_history
            ON conversation_branch_nodes(user_id, history_fingerprint)
            WHERE archived = 0
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_branch_user_parent
            ON conversation_branch_nodes(
                user_id, parent_history_fingerprint, archived, updated_at DESC
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_branch_user_conversation
            ON conversation_branch_nodes(
                user_id, conversation_id, archived, updated_at DESC
            )
            """
        )

    @staticmethod
    def _run_migrations(connection: sqlite3.Connection) -> None:
        """按 PRAGMA user_version 顺序执行一次性的 schema/数据迁移。

        建表语句保持幂等并在每次启动执行；迁移只运行尚未应用的版本，
        避免老库每次启动都重复全表 UPDATE 回填。
        """
        apply_schema_migrations(
            connection,
            _MEMORY_SCHEMA_MIGRATIONS,
            schema_name="memory database",
        )

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
        now = utc_now_iso()
        evidence_memory_ids = evidence_memory_ids or []
        topics = normalize_classification_names(topics or [], max_items=20, field_name="topics")
        entities = normalize_classification_names(entities or [], max_items=20, field_name="entities")
        sensitivity = _sensitivity_with_floor(
            declared=sensitivity,
            content=content,
            source_message=source_message,
            entities=entities,
        )
        temporal_subject = normalize_optional_text(temporal_subject)
        temporal_predicate = normalize_optional_text(temporal_predicate)
        space_ids = _ordered_unique([str(space_id).strip() for space_id in (space_ids or []) if str(space_id).strip()])
        if len(space_ids) > 10:
            raise ValueError("space_ids 最多 10 个")
        memory = MemoryRecord(
            id=new_memory_id(),
            user_id=user_id,
            content=content,
            type=type,
            importance=importance,
            confidence=confidence,
            valence=valence,
            arousal=arousal,
            source_message=source_message,
            source_conversation_id=source_conversation_id,
            origin=origin,
            embedding_json=embedding_json,
            embedding_space_id=embedding_space_id,
            last_used_at=None,
            usage_count=0,
            stability=stability,
            valid_from=valid_from,
            valid_until=valid_until,
            review_after=review_after,
            sensitivity=sensitivity,
            evidence_memory_ids=evidence_memory_ids,
            topics=topics,
            entities=entities,
            temporal_subject=temporal_subject,
            temporal_predicate=temporal_predicate,
            space_ids=space_ids,
            decay_lambda=decay_lambda,
            created_at=now,
            updated_at=now,
            archived_at=None,
            archived=0,
        )
        with self._connect() as connection:
            if final_matcher is not None:
                connection.execute("BEGIN IMMEDIATE")
                latest_rows = connection.execute(
                    """
                    SELECT * FROM memories
                    WHERE user_id = ? AND archived = 0
                      AND (status IS NULL OR status != 'archived')
                    ORDER BY importance DESC, updated_at DESC
                    """,
                    (user_id,),
                ).fetchall()
                matched = final_matcher(
                    self._rows_to_memories_on_connection(
                        connection=connection,
                        rows=latest_rows,
                    )
                )
                if matched is not None:
                    return matched
            self._validate_space_ids(
                connection=connection,
                user_id=user_id,
                space_ids=space_ids,
            )
            self._insert_memory_row(connection=connection, memory=memory)
            self._replace_memory_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=memory.id,
                space_ids=space_ids,
                created_at=now,
            )
            self._apply_temporal_invalidation(
                connection=connection,
                user_id=user_id,
                new_memory=memory,
            )
        return memory

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
        valid_until_was_unset = valid_until is _UNSET
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            live_row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if live_row is None:
                return None

            current_revision = max(1, int(live_row["revision"] or 1))
            if (
                expected_revision is not None
                and int(expected_revision) != current_revision
            ):
                raise RevisionConflictError(
                    resource="memory",
                    resource_id=memory_id,
                    expected_revision=int(expected_revision),
                    current_revision=current_revision,
                )

            existing_space_ids = self._space_ids_for_memory_ids_on_connection(
                connection=connection,
                user_id=user_id,
                memory_ids=[memory_id],
            ).get(memory_id, [])
            existing = self._row_to_memory(live_row, space_ids=existing_space_ids)
            if replacement_space_ids is not None or replacement_space_names is not None:
                replacement_space_ids = _ordered_unique(replacement_space_ids or [])
                replacement_space_names = replacement_space_names or []
                if len(replacement_space_ids) + len(replacement_space_names) > 10:
                    raise ValueError("space_ids 最多 10 个")
                created_spaces = [
                    self._upsert_memory_space_on_connection(
                        connection=connection,
                        user_id=user_id,
                        display_name=normalize_classification_name(
                            name,
                            field_name="space",
                        ),
                    )
                    for name in replacement_space_names
                ]
                replacement_space_ids = _ordered_unique(
                    [
                        *replacement_space_ids,
                        *(space.id for space in created_spaces),
                    ]
                )
                self._validate_space_ids(
                    connection=connection,
                    user_id=user_id,
                    space_ids=replacement_space_ids,
                )
            if evidence_memory_ids is None:
                evidence_memory_ids = existing.evidence_memory_ids
            if topics is None:
                topics = existing.topics
            if entities is None:
                entities = existing.entities
            if valid_from is _UNSET:
                valid_from = existing.valid_from
            if valid_until is _UNSET:
                valid_until = existing.valid_until
            if temporal_subject is _UNSET:
                temporal_subject = existing.temporal_subject
            if temporal_predicate is _UNSET:
                temporal_predicate = existing.temporal_predicate
            if decay_lambda is _UNSET:
                decay_lambda = existing.decay_lambda
            content_changed = content != existing.content
            if content_changed:
                embedding_json = None
                embedding_space_id = None
            elif embedding_json is None:
                embedding_space_id = None
            elif embedding_space_id is _UNSET:
                embedding_space_id = (
                    existing.embedding_space_id
                    if embedding_json == existing.embedding_json
                    else None
                )
            if (
                valid_until_was_unset
                and existing.superseded_by
                and (
                    normalize_iso_text(valid_from) != existing.valid_from
                    or normalize_optional_text(temporal_subject)
                    != existing.temporal_subject
                    or normalize_optional_text(temporal_predicate)
                    != existing.temporal_predicate
                )
            ):
                # This boundary was synthesized from the old successor. A moved
                # row must let its new chain position choose the next boundary.
                valid_until = None
                if status is None and existing.status == "resolved":
                    # The resolved state was derived from that synthesized
                    # boundary; a moved row re-enters its new key as live.
                    status = "dynamic"

            topics = normalize_classification_names(
                topics,
                max_items=20,
                field_name="topics",
            )
            entities = normalize_classification_names(
                entities,
                max_items=20,
                field_name="entities",
            )
            sensitivity = _sensitivity_with_floor(
                declared=sensitivity,
                content=content,
                source_message=source_message,
                entities=entities,
            )
            now = utc_now_iso()
            prospective = MemoryRecord(
                **{
                    **existing.model_dump(),
                    "content": content,
                    "type": type,
                    "importance": importance,
                    "confidence": confidence,
                    "valence": valence,
                    "arousal": arousal,
                    "source_message": source_message,
                    "source_conversation_id": source_conversation_id,
                    "embedding_json": embedding_json,
                    "embedding_space_id": embedding_space_id,
                    "stability": stability,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "review_after": review_after,
                    "sensitivity": sensitivity,
                    "evidence_memory_ids": evidence_memory_ids,
                    "topics": topics,
                    "entities": entities,
                    "temporal_subject": temporal_subject,
                    "temporal_predicate": temporal_predicate,
                    "status": status if status is not None else existing.status,
                    "decay_lambda": decay_lambda,
                    "updated_at": now,
                    "revision": current_revision + 1,
                }
            )
            topology_changed = any(
                getattr(existing, field_name) != getattr(prospective, field_name)
                for field_name in (
                    "valid_from",
                    "valid_until",
                    "temporal_subject",
                    "temporal_predicate",
                )
            )
            if topology_changed:
                prospective.supersedes = None
                prospective.superseded_by = None

            evidence_json = json.dumps(
                prospective.evidence_memory_ids,
                ensure_ascii=False,
            )
            topics_json = json.dumps(prospective.topics, ensure_ascii=False)
            entities_json = json.dumps(prospective.entities, ensure_ascii=False)
            if topology_changed:
                self._detach_temporal_position(
                    connection=connection,
                    user_id=user_id,
                    memory=existing,
                )
            cursor = connection.execute(
                """
                UPDATE memories
                SET content = ?, type = ?, importance = ?, confidence = ?,
                    valence = ?, arousal = ?, source_message = ?, source_conversation_id = ?,
                    embedding_json = ?, embedding_space_id = ?,
                    stability = ?, valid_from = ?, valid_until = ?,
                    review_after = ?, sensitivity = ?,
                    evidence_memory_ids_json = ?, topics_json = ?, entities_json = ?,
                    temporal_subject = ?, temporal_predicate = ?, status = ?,
                    decay_lambda = ?, supersedes = ?, superseded_by = ?, updated_at = ?,
                    revision = ?
                WHERE id = ? AND user_id = ? AND archived = 0 AND revision = ?
                """,
                (
                    prospective.content,
                    prospective.type,
                    prospective.importance,
                    prospective.confidence,
                    prospective.valence,
                    prospective.arousal,
                    prospective.source_message,
                    prospective.source_conversation_id,
                    prospective.embedding_json,
                    prospective.embedding_space_id,
                    prospective.stability,
                    prospective.valid_from,
                    prospective.valid_until,
                    prospective.review_after,
                    prospective.sensitivity,
                    evidence_json,
                    topics_json,
                    entities_json,
                    prospective.temporal_subject,
                    prospective.temporal_predicate,
                    prospective.status,
                    prospective.decay_lambda,
                    prospective.supersedes,
                    prospective.superseded_by,
                    now,
                    prospective.revision,
                    memory_id,
                    user_id,
                    current_revision,
                ),
            )
            if cursor.rowcount == 0:
                raise RuntimeError("Memory revision changed while holding the write lock.")
            if replacement_space_ids is not None:
                self._replace_memory_space_links(
                    connection=connection,
                    user_id=user_id,
                    memory_id=memory_id,
                    space_ids=replacement_space_ids,
                    created_at=now,
                )
                existing_space_ids = replacement_space_ids
            if (
                topology_changed
                and prospective.status != "archived"
                and prospective.temporal_subject
                and prospective.temporal_predicate
            ):
                self._apply_temporal_invalidation(
                    connection=connection,
                    user_id=user_id,
                    new_memory=prospective,
                )
            if topology_changed:
                temporal_keys = {
                    key
                    for key in (
                        (
                            existing.temporal_subject,
                            existing.temporal_predicate,
                        ),
                        (
                            prospective.temporal_subject,
                            prospective.temporal_predicate,
                        ),
                    )
                    if key[0] is not None and key[1] is not None
                }
                for subject, predicate in temporal_keys:
                    self._rebuild_temporal_key(
                        connection=connection,
                        user_id=user_id,
                        temporal_subject=subject,
                        temporal_predicate=predicate,
                    )
            updated_row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if updated_row is None:
                raise RuntimeError("Memory update did not persist.")
            return self._row_to_memory(updated_row, space_ids=existing_space_ids)

    def get_memory(self, *, memory_id: str, user_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
        return self._row_to_memory(row) if row else None

    def list_memory_timeline(
        self,
        *,
        user_id: str,
        subject: str,
        predicate: str | None = None,
        include_archived: bool = False,
    ) -> list[MemoryRecord]:
        temporal_subject = normalize_optional_text(subject)
        temporal_predicate = normalize_optional_text(predicate)
        if temporal_subject is None:
            return []

        conditions = [
            "user_id = ?",
            "archived = 0",
            "temporal_subject = ?",
        ]
        params: list[object] = [user_id, temporal_subject]
        if temporal_predicate is not None:
            conditions.append("temporal_predicate = ?")
            params.append(temporal_predicate)
        if not include_archived:
            conditions.append("(status IS NULL OR status != 'archived')")

        query = f"""
            SELECT * FROM memories
            WHERE {' AND '.join(conditions)}
            ORDER BY COALESCE(valid_from, created_at) ASC, created_at ASC
        """
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return self._rows_to_memories(rows)

    def restore_temporal_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
    ) -> MemoryRecord | None:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if row is None:
                return None

            space_ids = self._space_ids_for_memory_ids_on_connection(
                connection=connection,
                user_id=user_id,
                memory_ids=[memory_id],
            ).get(memory_id, [])
            source = self._row_to_memory(row, space_ids=space_ids)
            current_instant = _parse_iso_datetime(now)
            starts_at = _parse_iso_datetime(source.valid_from or source.created_at)
            ends_at = _parse_iso_datetime(source.valid_until)
            if (
                source.status in {"dynamic", "pinned"}
                and not source.superseded_by
                and (starts_at is None or current_instant is None or starts_at <= current_instant)
                and (ends_at is None or current_instant is None or ends_at > current_instant)
            ):
                return source

            restored = MemoryRecord(
                **{
                    **source.model_dump(),
                    "id": new_memory_id(),
                    "last_used_at": None,
                    "usage_count": 0.0,
                    "valid_from": now,
                    "valid_until": None,
                    "status": "pinned" if source.status == "pinned" else "dynamic",
                    "supersedes": None,
                    "superseded_by": None,
                    "created_at": now,
                    "updated_at": now,
                    "archived_at": None,
                    "archived": 0,
                }
            )
            self._insert_memory_row(connection=connection, memory=restored)
            self._replace_memory_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=restored.id,
                space_ids=space_ids,
                created_at=now,
            )
            self._apply_temporal_invalidation(
                connection=connection,
                user_id=user_id,
                new_memory=restored,
            )

            self._insert_decision_log(
                connection=connection,
                user_id=user_id,
                conversation_id=None,
                candidate_json=json.dumps(
                    {
                        "source": "temporal_restore",
                        "source_memory_id": memory_id,
                        "restored_memory_id": restored.id,
                        "before": self._temporal_snapshot(row),
                        "after": {
                            "valid_from": restored.valid_from,
                            "valid_until": restored.valid_until,
                            "supersedes": restored.supersedes,
                            "superseded_by": restored.superseded_by,
                            "status": restored.status,
                        },
                    },
                    ensure_ascii=False,
                ),
                decision="update",
                reason="Copied a historical temporal fact into a new current version",
            )
        return self.get_memory(memory_id=restored.id, user_id=user_id)

    def list_memories(
        self,
        *,
        user_id: str,
        limit: int = 200,
        status: str | None = None,
        include_lifecycle_archived: bool = False,
    ) -> list[MemoryRecord]:
        if status and status != "all":
            sql = """SELECT * FROM memories
                     WHERE user_id = ? AND archived = 0 AND status = ?
                     ORDER BY importance DESC, updated_at DESC
                     LIMIT ?"""
            params: tuple = (user_id, status, limit)
        elif include_lifecycle_archived or status == "all":
            sql = """SELECT * FROM memories
                     WHERE user_id = ? AND archived = 0
                     ORDER BY importance DESC, updated_at DESC
                     LIMIT ?"""
            params = (user_id, limit)
        else:
            sql = """SELECT * FROM memories
                     WHERE user_id = ? AND archived = 0
                       AND (status IS NULL OR status != 'archived')
                     ORDER BY importance DESC, updated_at DESC
                     LIMIT ?"""
            params = (user_id, limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return self._rows_to_memories(rows)

    def list_memories_for_resolution(self, *, user_id: str) -> list[MemoryRecord]:
        """Return the complete active candidate set used for write deduplication.

        Resolver correctness must not depend on importance ordering: an exact
        duplicate below an arbitrary top-N cutoff is still a duplicate.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND archived = 0
                  AND (status IS NULL OR status != 'archived')
                ORDER BY importance DESC, updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return self._rows_to_memories(rows)

    @contextmanager
    def memory_recall_snapshot(
        self,
        *,
        user_id: str,
        page_size: int = 500,
    ) -> Iterator[Callable[[], Iterator[list[MemoryRecord]]]]:
        """Expose repeatable, bounded page scans from one SQLite snapshot.

        Search needs two passes because keyword IDF is corpus-wide.  Returning a
        fresh page iterator for each pass keeps the read transaction (and thus
        the visible corpus) stable without ever materializing the whole library.
        """
        bounded_page_size = max(1, min(int(page_size), 1000))
        with self._connect() as connection:
            connection.execute("BEGIN")

            def read_pages() -> Iterator[list[MemoryRecord]]:
                last_rowid: int | None = None
                while True:
                    if last_rowid is None:
                        rows = connection.execute(
                            """
                            SELECT rowid AS recall_rowid, *
                            FROM memories
                            WHERE user_id = ? AND archived = 0
                              AND (status IS NULL OR status != 'archived')
                            ORDER BY rowid ASC
                            LIMIT ?
                            """,
                            (user_id, bounded_page_size),
                        ).fetchall()
                    else:
                        rows = connection.execute(
                            """
                            SELECT rowid AS recall_rowid, *
                            FROM memories
                            WHERE user_id = ? AND archived = 0
                              AND (status IS NULL OR status != 'archived')
                              AND rowid > ?
                            ORDER BY rowid ASC
                            LIMIT ?
                            """,
                            (user_id, last_rowid, bounded_page_size),
                        ).fetchall()
                    if not rows:
                        return
                    last_rowid = int(rows[-1]["recall_rowid"])
                    yield self._rows_to_memories_on_connection(
                        connection=connection,
                        rows=rows,
                    )

            yield read_pages

    def list_all_memories_for_export(
        self,
        *,
        user_id: str,
        archived: bool,
        page_size: int = 500,
    ) -> list[MemoryRecord]:
        """Read every user row in bounded pages for a complete backup export."""
        bounded_page_size = max(1, min(int(page_size), 1000))
        with self._connect() as connection:
            connection.execute("BEGIN")
            rows: list[sqlite3.Row] = []
            last_rowid = 0
            while True:
                page = connection.execute(
                    """
                    SELECT rowid AS export_rowid, *
                    FROM memories
                    WHERE user_id = ? AND archived = ? AND rowid > ?
                    ORDER BY rowid ASC
                    LIMIT ?
                    """,
                    (user_id, int(archived), last_rowid, bounded_page_size),
                ).fetchall()
                if not page:
                    break
                rows.extend(page)
                last_rowid = int(page[-1]["export_rowid"])
            return self._rows_to_memories_on_connection(
                connection=connection,
                rows=rows,
            )

    def read_memory_export_snapshot(
        self,
        *,
        user_id: str,
        include_deleted: bool = True,
        page_size: int = 500,
    ) -> dict[str, list[object]]:
        """Read every export partition from one SQLite snapshot."""
        bounded_page_size = max(1, min(int(page_size), 1000))
        with self._connect() as connection:
            connection.execute("BEGIN")
            memory_rows: list[sqlite3.Row] = []
            last_rowid = 0
            while True:
                archive_clause = "" if include_deleted else " AND archived = 0"
                rows = connection.execute(
                    f"""
                    SELECT rowid AS export_rowid, *
                    FROM memories
                    WHERE user_id = ?{archive_clause} AND rowid > ?
                    ORDER BY rowid ASC
                    LIMIT ?
                    """,
                    (user_id, last_rowid, bounded_page_size),
                ).fetchall()
                if not rows:
                    break
                memory_rows.extend(rows)
                last_rowid = int(rows[-1]["export_rowid"])

            memories = self._rows_to_memories_on_connection(
                connection=connection,
                rows=memory_rows,
            )
            space_rows = connection.execute(
                """
                SELECT * FROM memory_spaces
                WHERE user_id = ? AND archived = 0
                ORDER BY updated_at DESC, name ASC
                """,
                (user_id,),
            ).fetchall()
            core_rows = connection.execute(
                """
                SELECT * FROM core_memory_sections
                WHERE user_id = ? AND archived = 0
                ORDER BY
                    CASE section
                        WHEN 'profile' THEN 1
                        WHEN 'preferences' THEN 2
                        WHEN 'relationships' THEN 3
                        WHEN 'routines' THEN 4
                        WHEN 'goals' THEN 5
                        WHEN 'communication' THEN 6
                        ELSE 99
                    END,
                    updated_at DESC
                """,
                (user_id,),
            ).fetchall()
            core_history_rows = connection.execute(
                """
                SELECT * FROM core_memory_section_history
                WHERE user_id = ?
                ORDER BY replaced_at DESC
                """,
                (user_id,),
            ).fetchall()
            recent_context_rows = connection.execute(
                """
                SELECT * FROM recent_context_summaries
                WHERE user_id = ? AND archived = 0
                ORDER BY updated_at DESC
                """,
                (user_id,),
            ).fetchall()
            branch_rows = connection.execute(
                """
                SELECT * FROM conversation_branch_nodes
                WHERE user_id = ? AND archived = 0
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (user_id, _CONVERSATION_BRANCH_NODE_RETENTION_LIMIT),
            ).fetchall()
            decision_rows = connection.execute(
                """
                SELECT * FROM memory_decision_logs
                WHERE user_id = ?
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()

            return {
                "memory_spaces": [self._row_to_memory_space(row) for row in space_rows],
                "memories": [memory for memory in memories if not memory.archived],
                "deleted_memories": [memory for memory in memories if memory.archived],
                "core_memory_sections": [
                    self._row_to_core_memory_section(row) for row in core_rows
                ],
                "core_memory_section_history": [
                    self._row_to_core_memory_section_history(row)
                    for row in core_history_rows
                ],
                "recent_context_summaries": [
                    self._row_to_recent_context_summary(row)
                    for row in recent_context_rows
                ],
                "conversation_branch_nodes": [
                    self._row_to_conversation_branch_node(row) for row in branch_rows
                ],
                "decision_logs": [DecisionLog(**dict(row)) for row in decision_rows],
            }

    def read_memory_selection_export_snapshot(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> dict[str, list[object] | list[str]]:
        """Read an exact, user-scoped selection from one SQLite snapshot.

        The API deliberately treats an archived row as selectable too so the
        recycle-bin view can use the same safe export contract. Callers must
        reject ``missing_memory_ids`` rather than returning a partial archive.
        """
        requested_ids = _ordered_unique(memory_ids)
        with self._connect() as connection:
            connection.execute("BEGIN")
            rows_by_id: dict[str, sqlite3.Row] = {}
            for offset in range(0, len(requested_ids), 500):
                batch = requested_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT * FROM memories
                    WHERE user_id = ? AND id IN ({placeholders})
                    """,
                    (user_id, *batch),
                ).fetchall()
                rows_by_id.update({str(row["id"]): row for row in rows})

            missing_ids = [
                memory_id for memory_id in requested_ids if memory_id not in rows_by_id
            ]
            ordered_rows = [
                rows_by_id[memory_id]
                for memory_id in requested_ids
                if memory_id in rows_by_id
            ]
            memories = self._rows_to_memories_on_connection(
                connection=connection,
                rows=ordered_rows,
            )
            referenced_space_ids = _ordered_unique(
                [space_id for memory in memories for space_id in memory.space_ids]
            )
            spaces_by_id: dict[str, MemorySpace] = {}
            for offset in range(0, len(referenced_space_ids), 500):
                batch = referenced_space_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT * FROM memory_spaces
                    WHERE user_id = ? AND id IN ({placeholders})
                    """,
                    (user_id, *batch),
                ).fetchall()
                spaces_by_id.update(
                    {
                        str(row["id"]): self._row_to_memory_space(row)
                        for row in rows
                    }
                )
            spaces = [
                spaces_by_id[space_id]
                for space_id in referenced_space_ids
                if space_id in spaces_by_id
            ]
            return {
                "memories": memories,
                "memory_spaces": spaces,
                "missing_memory_ids": missing_ids,
            }

    def get_memories_max_updated_at(self, *, user_id: str) -> str | None:
        """返回该用户所有活跃记忆的最新 updated_at，用于缓存失效比对。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(updated_at) FROM memories
                WHERE user_id = ? AND archived = 0
                  AND (status IS NULL OR status != 'archived')
                """,
                (user_id,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_active_memory_count(self, *, user_id: str) -> int:
        """返回该用户活跃记忆的数量，用于缓存失效比对。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM memories
                WHERE user_id = ? AND archived = 0
                  AND (status IS NULL OR status != 'archived')
                """,
                (user_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def get_next_temporal_boundary(
        self,
        *,
        user_id: str,
        after: datetime,
    ) -> datetime | None:
        """Return the next validity boundary that can change recall eligibility."""
        current = after if after.tzinfo is not None else after.replace(tzinfo=UTC)
        current = current.astimezone(UTC)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT valid_from, valid_until
                FROM memories
                WHERE user_id = ? AND archived = 0
                  AND (status IS NULL OR status != 'archived')
                  AND (valid_from IS NOT NULL OR valid_until IS NOT NULL)
                """,
                (user_id,),
            ).fetchall()
        boundaries = [
            parsed.astimezone(UTC)
            for row in rows
            for value in (row["valid_from"], row["valid_until"])
            if (parsed := _parse_iso_datetime(value)) is not None and parsed > current
        ]
        return min(boundaries) if boundaries else None

    def list_archived_memories(
        self,
        *,
        user_id: str,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND archived = 1
                ORDER BY archived_at DESC, updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return self._rows_to_memories(rows)

    def list_core_memory_sections(
        self,
        *,
        user_id: str,
    ) -> list[CoreMemorySection]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM core_memory_sections
                WHERE user_id = ? AND archived = 0
                ORDER BY
                    CASE section
                        WHEN 'profile' THEN 1
                        WHEN 'preferences' THEN 2
                        WHEN 'relationships' THEN 3
                        WHEN 'routines' THEN 4
                        WHEN 'goals' THEN 5
                        WHEN 'communication' THEN 6
                        ELSE 99
                    END,
                    updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_core_memory_section(row) for row in rows]

    def get_core_memory_section(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName,
    ) -> CoreMemorySection | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM core_memory_sections
                WHERE user_id = ? AND section = ? AND archived = 0
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user_id, section),
            ).fetchone()
        return self._row_to_core_memory_section(row) if row else None

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
        evidence_json = json.dumps(evidence_memory_ids, ensure_ascii=False)
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM core_memory_sections
                WHERE user_id = ? AND section = ? AND archived = 0
                LIMIT 1
                """,
                (user_id, section),
            ).fetchone()
            existing = self._row_to_core_memory_section(row) if row else None
            if expected_revision is not None:
                current_revision = existing.revision if existing is not None else 0
                if int(expected_revision) != current_revision:
                    raise RevisionConflictError(
                        resource="core_memory",
                        resource_id=section,
                        expected_revision=int(expected_revision),
                        current_revision=current_revision,
                    )

            if existing is None:
                core_memory = CoreMemorySection(
                    id=new_memory_id(),
                    user_id=user_id,
                    section=section,
                    content=content.strip(),
                    evidence_memory_ids=evidence_memory_ids,
                    confidence=confidence,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    archived=0,
                    revision=1,
                )
                connection.execute(
                    """
                    INSERT INTO core_memory_sections (
                        id, user_id, section, content, evidence_memory_ids_json,
                        confidence, version, created_at, updated_at, archived, revision
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        core_memory.id,
                        core_memory.user_id,
                        core_memory.section,
                        core_memory.content,
                        evidence_json,
                        core_memory.confidence,
                        core_memory.version,
                        core_memory.created_at,
                        core_memory.updated_at,
                        core_memory.archived,
                        core_memory.revision,
                    ),
                )
                return "create", core_memory

            normalized_content = content.strip()
            if (
                existing.content == normalized_content
                and existing.evidence_memory_ids == evidence_memory_ids
                and abs(existing.confidence - confidence) < 0.001
            ):
                return "ignore", existing
            self._create_core_memory_section_history(
                connection=connection,
                section=existing,
                replaced_at=now,
            )
            cursor = connection.execute(
                """
                UPDATE core_memory_sections
                SET content = ?, evidence_memory_ids_json = ?, confidence = ?,
                    version = ?, updated_at = ?, revision = revision + 1
                WHERE id = ? AND user_id = ? AND archived = 0 AND revision = ?
                """,
                (
                    normalized_content,
                    evidence_json,
                    confidence,
                    existing.version + 1,
                    now,
                    existing.id,
                    user_id,
                    existing.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Core memory revision changed while holding the write lock.")
            updated_row = connection.execute(
                """
                SELECT * FROM core_memory_sections
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (existing.id, user_id),
            ).fetchone()
            if updated_row is None:
                raise RuntimeError("Core memory update did not persist.")
            return "update", self._row_to_core_memory_section(updated_row)

    def archive_core_memory_section(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName,
        expected_revision: int | None = None,
    ) -> bool:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, revision FROM core_memory_sections
                WHERE user_id = ? AND section = ? AND archived = 0
                LIMIT 1
                """,
                (user_id, section),
            ).fetchone()
            if existing is None:
                return False
            current_revision = max(1, int(existing["revision"] or 1))
            if (
                expected_revision is not None
                and int(expected_revision) != current_revision
            ):
                raise RevisionConflictError(
                    resource="core_memory",
                    resource_id=section,
                    expected_revision=int(expected_revision),
                    current_revision=current_revision,
                )
            cursor = connection.execute(
                """
                UPDATE core_memory_sections
                SET archived = 1, updated_at = ?, revision = revision + 1
                WHERE user_id = ? AND section = ? AND archived = 0 AND revision = ?
                """,
                (now, user_id, section, current_revision),
            )
        return cursor.rowcount > 0

    def list_core_memory_section_history(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName | None = None,
        limit: int | None = 50,
    ) -> list[CoreMemorySectionHistory]:
        query = """
            SELECT * FROM core_memory_section_history
            WHERE user_id = ?
        """
        params: list[object] = [user_id]
        if section is not None:
            query += " AND section = ?"
            params.append(section)
        query += " ORDER BY replaced_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_core_memory_section_history(row) for row in rows]

    def explain_memory_source(
        self,
        *,
        memory_id: str,
        user_id: str,
    ) -> MemorySourceExplanation | None:
        memory = self.get_memory(memory_id=memory_id, user_id=user_id)
        if memory is None:
            return None
        core_sections = [
            section.section
            for section in self.list_core_memory_sections(user_id=user_id)
            if memory.id in section.evidence_memory_ids
        ]
        return MemorySourceExplanation(
            memory_id=memory.id,
            content=memory.content,
            source_excerpt=memory.source_message,
            source_conversation_id=memory.source_conversation_id,
            saved_at=memory.created_at,
            updated_at=memory.updated_at,
            confidence=memory.confidence,
            is_core_memory_evidence=bool(core_sections),
            core_memory_sections=core_sections,
            evidence_memory_ids=memory.evidence_memory_ids,
        )

    def merge_memories(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
        content: str | None = None,
    ) -> MemoryMergeResult:
        ordered_ids = _ordered_unique(memory_ids)
        if len(ordered_ids) < 2:
            return MemoryMergeResult(
                action="ignore",
                reason="至少需要两个 memory_id 才能合并",
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows: list[sqlite3.Row] = []
            for offset in range(0, len(ordered_ids), 500):
                batch = ordered_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT * FROM memories
                        WHERE user_id = ? AND archived = 0
                          AND id IN ({placeholders})
                        """,
                        (user_id, *batch),
                    ).fetchall()
                )
            rows_by_id = {str(row["id"]): row for row in rows}
            if any(memory_id not in rows_by_id for memory_id in ordered_ids):
                return MemoryMergeResult(
                    action="ignore",
                    reason="部分记忆不存在或已删除，无法合并",
                )

            link_rows: list[sqlite3.Row] = []
            for offset in range(0, len(ordered_ids), 500):
                batch = ordered_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                link_rows.extend(
                    connection.execute(
                        f"""
                        SELECT memory_id, space_id
                        FROM memory_space_links
                        WHERE user_id = ? AND memory_id IN ({placeholders})
                        ORDER BY created_at ASC, rowid ASC
                        """,
                        (user_id, *batch),
                    ).fetchall()
                )
            spaces_by_memory_id: dict[str, list[str]] = {
                memory_id: [] for memory_id in ordered_ids
            }
            for link_row in link_rows:
                spaces_by_memory_id.setdefault(str(link_row["memory_id"]), []).append(
                    str(link_row["space_id"])
                )
            active_memories = [
                self._row_to_memory(
                    rows_by_id[memory_id],
                    space_ids=spaces_by_memory_id.get(memory_id, []),
                )
                for memory_id in ordered_ids
            ]
            temporal_versions = [
                memory
                for memory in active_memories
                if (
                    (memory.temporal_subject and memory.temporal_predicate)
                    or memory.supersedes
                    or memory.superseded_by
                )
            ]
            if temporal_versions:
                signatures = {
                    (
                        memory.temporal_subject,
                        memory.temporal_predicate,
                        memory.valid_from,
                        memory.valid_until,
                        memory.supersedes,
                        memory.superseded_by,
                    )
                    for memory in active_memories
                }
                if len(temporal_versions) != len(active_memories) or len(signatures) != 1:
                    return MemoryMergeResult(
                        action="ignore",
                        reason="不同时间版本不能合并；请保留各自的历史区间",
                    )
            target = active_memories[0]
            merged_content = (content or _join_memory_contents(active_memories)).strip()
            if not merged_content:
                return MemoryMergeResult(action="ignore", reason="合并内容为空")

            evidence_memory_ids = _ordered_unique(
                [
                    evidence_id
                    for memory in active_memories
                    for evidence_id in (memory.id, *memory.evidence_memory_ids)
                ]
            )
            topics = normalize_classification_names(
                _ordered_unique(
                    [topic for memory in active_memories for topic in memory.topics]
                ),
                max_items=20,
                field_name="topics",
            )
            entities = normalize_classification_names(
                _ordered_unique(
                    [entity for memory in active_memories for entity in memory.entities]
                ),
                max_items=20,
                field_name="entities",
            )
            space_ids = _ordered_unique(
                [space_id for memory in active_memories for space_id in memory.space_ids]
            )
            if len(space_ids) > 10:
                raise ValueError("space_ids 最多 10 个")
            self._validate_space_ids(
                connection=connection,
                user_id=user_id,
                space_ids=space_ids,
            )

            merged_sensitivity = _sensitivity_with_floor(
                declared=_merged_sensitivity(active_memories),
                content=merged_content,
                source_message=target.source_message,
                entities=entities,
            )
            merged_valid_from = normalize_iso_text(
                _shared_value([memory.valid_from for memory in active_memories])
            )
            merged_valid_until = normalize_iso_text(
                _shared_value([memory.valid_until for memory in active_memories])
            )
            merged_review_after = normalize_iso_text(
                _earliest_datetime_text(
                    [
                        memory.review_after
                        for memory in active_memories
                        if memory.review_after
                    ]
                )
            )
            merged_temporal_subject = normalize_optional_text(
                _shared_value([memory.temporal_subject for memory in active_memories])
            )
            merged_temporal_predicate = normalize_optional_text(
                _shared_value([memory.temporal_predicate for memory in active_memories])
            )
            now = utc_now_iso()
            cursor = connection.execute(
                """
                UPDATE memories
                SET content = ?, type = ?, importance = ?, confidence = ?,
                    valence = ?, arousal = ?, source_message = ?,
                    source_conversation_id = ?, embedding_json = NULL,
                    embedding_space_id = NULL,
                    stability = ?, valid_from = ?, valid_until = ?,
                    review_after = ?, sensitivity = ?,
                    evidence_memory_ids_json = ?, topics_json = ?, entities_json = ?,
                    temporal_subject = ?, temporal_predicate = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (
                    merged_content,
                    _merged_type(active_memories),
                    max(memory.importance for memory in active_memories),
                    max(memory.confidence for memory in active_memories),
                    _average_float(
                        [memory.valence for memory in active_memories],
                        default=0.5,
                    ),
                    _average_float(
                        [memory.arousal for memory in active_memories],
                        default=0.3,
                    ),
                    target.source_message,
                    target.source_conversation_id,
                    _merged_stability(active_memories),
                    merged_valid_from,
                    merged_valid_until,
                    merged_review_after,
                    merged_sensitivity,
                    json.dumps(evidence_memory_ids, ensure_ascii=False),
                    json.dumps(topics, ensure_ascii=False),
                    json.dumps(entities, ensure_ascii=False),
                    merged_temporal_subject,
                    merged_temporal_predicate,
                    now,
                    target.id,
                    user_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Merge target disappeared during transaction.")
            self._replace_memory_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=target.id,
                space_ids=space_ids,
                created_at=now,
            )

            archived_ids = [memory.id for memory in active_memories[1:]]
            archived_count = 0
            for offset in range(0, len(archived_ids), 500):
                batch = archived_ids[offset : offset + 500]
                archived_placeholders = ", ".join("?" for _ in batch)
                archive_cursor = connection.execute(
                    f"""
                    UPDATE memories
                    SET archived = 1, archived_at = ?, updated_at = ?
                    WHERE user_id = ? AND archived = 0
                      AND id IN ({archived_placeholders})
                    """,
                    (now, now, user_id, *batch),
                )
                archived_count += max(0, int(archive_cursor.rowcount))
            if archived_count != len(archived_ids):
                raise RuntimeError("Merge sources changed during transaction.")

            updated_row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (target.id, user_id),
            ).fetchone()
            if updated_row is None:
                raise RuntimeError("Merge target was not persisted.")
            updated = self._row_to_memory(updated_row, space_ids=space_ids)

        return MemoryMergeResult(
            action="update",
            memory=updated,
            merged_memory_ids=ordered_ids,
            archived_memory_ids=archived_ids,
            reason="已合并记忆并保留 evidence ids",
        )

    def get_recent_context_summary(
        self,
        *,
        user_id: str,
        conversation_id: str | None = None,
    ) -> RecentContextSummary | None:
        if conversation_id is not None:
            return self.get_recent_context_summary_for_conversation(
                user_id=user_id,
                conversation_id=conversation_id,
            )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM recent_context_summaries
                WHERE user_id = ? AND archived = 0
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return self._row_to_recent_context_summary(row) if row else None

    def get_recent_context_summary_for_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
    ) -> RecentContextSummary | None:
        if conversation_id is None:
            query = """
                SELECT * FROM recent_context_summaries
                WHERE user_id = ? AND conversation_id IS NULL AND archived = 0
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
            """
            params = (user_id,)
        else:
            query = """
                SELECT * FROM recent_context_summaries
                WHERE user_id = ? AND conversation_id = ? AND archived = 0
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
            """
            params = (user_id, conversation_id)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._row_to_recent_context_summary(row) if row else None

    def list_recent_context_summaries(
        self,
        *,
        user_id: str,
        limit: int | None = 20,
    ) -> list[RecentContextSummary]:
        bounded_limit = None if limit is None else max(1, int(limit))
        with self._connect() as connection:
            query = """
                SELECT * FROM recent_context_summaries
                WHERE user_id = ? AND archived = 0
                ORDER BY updated_at DESC
            """
            params: list[object] = [user_id]
            if bounded_limit is not None:
                query += " LIMIT ?"
                params.append(bounded_limit)
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_recent_context_summary(row) for row in rows]

    def upsert_recent_context_summary(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        summary: str,
    ) -> RecentContextSummary:
        return self.upsert_recent_context_state(
            user_id=user_id,
            conversation_id=conversation_id,
            summary=summary,
            compressed_summary=summary,
            recent_turns=[],
            turn_count=0,
        )

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
        normalized_summary = summary.strip()
        normalized_compressed_summary = compressed_summary.strip()
        recent_turns_json = json.dumps(
            [turn.model_dump() for turn in recent_turns],
            ensure_ascii=False,
        )
        existing = self.get_recent_context_summary_for_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        now = utc_now_iso()
        if existing:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE recent_context_summaries
                    SET summary = ?, compressed_summary = ?,
                        recent_turns_json = ?, turn_count = ?, updated_at = ?
                    WHERE id = ? AND user_id = ? AND archived = 0
                    """,
                    (
                        normalized_summary,
                        normalized_compressed_summary,
                        recent_turns_json,
                        max(0, turn_count),
                        now,
                        existing.id,
                        user_id,
                    ),
                )
            updated = self.get_recent_context_summary_for_conversation(
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return updated if updated else existing

        recent_summary = RecentContextSummary(
            id=new_memory_id(),
            user_id=user_id,
            conversation_id=conversation_id,
            summary=normalized_summary,
            compressed_summary=normalized_compressed_summary,
            recent_turns=recent_turns,
            turn_count=max(0, turn_count),
            created_at=now,
            updated_at=now,
            archived=0,
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO recent_context_summaries (
                        id, user_id, conversation_id, summary,
                        compressed_summary, recent_turns_json, turn_count,
                        created_at, updated_at, archived
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recent_summary.id,
                        recent_summary.user_id,
                        recent_summary.conversation_id,
                        recent_summary.summary,
                        recent_summary.compressed_summary,
                        recent_turns_json,
                        recent_summary.turn_count,
                        recent_summary.created_at,
                        recent_summary.updated_at,
                        recent_summary.archived,
                    ),
                )
            return recent_summary
        except sqlite3.IntegrityError:
            return self.upsert_recent_context_state(
                user_id=user_id,
                conversation_id=conversation_id,
                summary=normalized_summary,
                compressed_summary=normalized_compressed_summary,
                recent_turns=recent_turns,
                turn_count=max(0, turn_count),
            )

    def get_conversation_branch_node(
        self,
        *,
        user_id: str,
        history_fingerprint: str,
    ) -> ConversationBranchNode | None:
        normalized = history_fingerprint.strip()
        if not normalized:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_branch_nodes
                WHERE user_id = ? AND history_fingerprint = ? AND archived = 0
                LIMIT 1
                """,
                (user_id, normalized),
            ).fetchone()
        return self._row_to_conversation_branch_node(row) if row else None

    def list_conversation_branch_nodes(
        self,
        *,
        user_id: str,
        limit: int = 5000,
        archived: bool = False,
    ) -> list[ConversationBranchNode]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM conversation_branch_nodes
                WHERE user_id = ? AND archived = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (
                    user_id,
                    int(archived),
                    max(1, min(limit, _CONVERSATION_BRANCH_NODE_RETENTION_LIMIT)),
                ),
            ).fetchall()
        return [self._row_to_conversation_branch_node(row) for row in rows]

    def count_conversation_branch_nodes(
        self,
        *,
        user_id: str,
        archived: bool = False,
    ) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM conversation_branch_nodes
                WHERE user_id = ? AND archived = ?
                """,
                (user_id, int(archived)),
            ).fetchone()
        return int(row["count"]) if row else 0

    def archive_conversation_branch_subtree(
        self,
        *,
        node_id: str,
        user_id: str,
    ) -> int:
        """Soft-delete one branch node and every active descendant."""

        now = utc_now_iso()
        with self._connect() as connection:
            before = connection.total_changes
            connection.execute(
                """
                WITH RECURSIVE subtree(history_fingerprint) AS (
                    SELECT history_fingerprint
                    FROM conversation_branch_nodes
                    WHERE id = ? AND user_id = ? AND archived = 0

                    UNION

                    SELECT child.history_fingerprint
                    FROM conversation_branch_nodes AS child
                    JOIN subtree AS parent
                      ON child.parent_history_fingerprint = parent.history_fingerprint
                    WHERE child.user_id = ? AND child.archived = 0
                )
                UPDATE conversation_branch_nodes
                SET archived = 1, updated_at = ?
                WHERE user_id = ? AND archived = 0
                  AND history_fingerprint IN (
                      SELECT history_fingerprint FROM subtree
                  )
                """,
                (node_id, user_id, user_id, now, user_id),
            )
            return connection.total_changes - before

    def restore_conversation_branch_subtree(
        self,
        *,
        node_id: str,
        user_id: str,
    ) -> int:
        """Restore one archived branch node and every archived descendant."""

        now = utc_now_iso()
        with self._connect() as connection:
            before = connection.total_changes
            connection.execute(
                """
                WITH RECURSIVE subtree(history_fingerprint) AS (
                    SELECT history_fingerprint
                    FROM conversation_branch_nodes
                    WHERE id = ? AND user_id = ? AND archived = 1

                    UNION

                    SELECT child.history_fingerprint
                    FROM conversation_branch_nodes AS child
                    JOIN subtree AS parent
                      ON child.parent_history_fingerprint = parent.history_fingerprint
                    WHERE child.user_id = ? AND child.archived = 1
                )
                UPDATE conversation_branch_nodes
                SET archived = 0, updated_at = ?
                WHERE user_id = ? AND archived = 1
                  AND history_fingerprint IN (
                      SELECT history_fingerprint FROM subtree
                  )
                """,
                (node_id, user_id, user_id, now, user_id),
            )
            return connection.total_changes - before

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
        normalized_history = history_fingerprint.strip()
        if not normalized_history:
            raise ValueError("history_fingerprint must not be empty")
        now = utc_now_iso()
        node_id = "branch-" + hashlib.sha256(
            f"{user_id}\0{normalized_history}".encode("utf-8")
        ).hexdigest()[:32]
        recent_turns_json = json.dumps(
            [turn.model_dump() for turn in recent_turns],
            ensure_ascii=False,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_branch_nodes (
                    id, user_id, conversation_id, history_fingerprint,
                    parent_history_fingerprint, turn_fingerprint,
                    assistant_digest, summary, compressed_summary,
                    recent_turns_json, turn_count, created_at, updated_at, archived
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(id)
                DO UPDATE SET
                    user_id = excluded.user_id,
                    conversation_id = excluded.conversation_id,
                    history_fingerprint = excluded.history_fingerprint,
                    parent_history_fingerprint = excluded.parent_history_fingerprint,
                    turn_fingerprint = excluded.turn_fingerprint,
                    assistant_digest = excluded.assistant_digest,
                    summary = excluded.summary,
                    compressed_summary = excluded.compressed_summary,
                    recent_turns_json = excluded.recent_turns_json,
                    turn_count = excluded.turn_count,
                    updated_at = excluded.updated_at,
                    archived = 0
                """,
                (
                    node_id,
                    user_id,
                    conversation_id,
                    normalized_history,
                    parent_history_fingerprint.strip(),
                    turn_fingerprint.strip(),
                    assistant_digest.strip(),
                    summary.strip(),
                    compressed_summary.strip(),
                    recent_turns_json,
                    max(0, turn_count),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM conversation_branch_nodes
                WHERE user_id = ? AND id IN (
                    SELECT id
                    FROM conversation_branch_nodes
                    WHERE user_id = ? AND archived = 0
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (
                    user_id,
                    user_id,
                    _CONVERSATION_BRANCH_NODE_RETENTION_LIMIT,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM conversation_branch_nodes
                WHERE user_id = ? AND history_fingerprint = ? AND archived = 0
                LIMIT 1
                """,
                (user_id, normalized_history),
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation branch node write did not persist")
        return self._row_to_conversation_branch_node(row)

    def archive_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
        expected_revision: int | None = None,
        return_revision: bool = False,
    ) -> bool | int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if row is None:
                return False
            current_revision = max(1, int(row["revision"] or 1))
            if (
                expected_revision is not None
                and int(expected_revision) != current_revision
            ):
                raise RevisionConflictError(
                    resource="memory",
                    resource_id=memory_id,
                    expected_revision=int(expected_revision),
                    current_revision=current_revision,
                )
            source = self._row_to_memory(row, space_ids=[])
            now = utc_now_iso()
            cursor = connection.execute(
                """
                UPDATE memories
                SET archived = 1, archived_at = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ? AND user_id = ? AND archived = 0 AND revision = ?
                """,
                (now, now, memory_id, user_id, current_revision),
            )
            if cursor.rowcount == 0:
                return False
            if source.temporal_subject and source.temporal_predicate:
                self._rebuild_temporal_key(
                    connection=connection,
                    user_id=user_id,
                    temporal_subject=source.temporal_subject,
                    temporal_predicate=source.temporal_predicate,
                )
            return current_revision + 1 if return_revision else True

    def restore_memory(self, *, memory_id: str, user_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 1
                """,
                (memory_id, user_id),
            ).fetchone()
            if row is None:
                return None
            space_ids = self._space_ids_for_memory_ids_on_connection(
                connection=connection,
                user_id=user_id,
                memory_ids=[memory_id],
            ).get(memory_id, [])
            source = self._row_to_memory(row, space_ids=space_ids)
            has_temporal_key = bool(
                source.temporal_subject and source.temporal_predicate
            )
            restored_status = (
                "pinned" if source.status == "pinned" else "dynamic"
            ) if has_temporal_key else source.status
            now = utc_now_iso()
            cursor = connection.execute(
                """
                UPDATE memories
                SET archived = 0, archived_at = NULL, status = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ? AND user_id = ? AND archived = 1
                """,
                (restored_status, now, memory_id, user_id),
            )
            if cursor.rowcount == 0:
                return None
            if has_temporal_key:
                self._rebuild_temporal_key(
                    connection=connection,
                    user_id=user_id,
                    temporal_subject=source.temporal_subject,
                    temporal_predicate=source.temporal_predicate,
                )
            restored_row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if restored_row is None:
                raise RuntimeError("Memory restore did not persist.")
            return self._row_to_memory(restored_row, space_ids=space_ids)

    def update_memory_embedding(
        self,
        *,
        memory_id: str,
        user_id: str,
        embedding_json: str,
        embedding_space_id: str,
    ) -> bool:
        """仅更新活跃记忆的 embedding，用于 re-embed 流程。"""
        normalized_space_id = " ".join(embedding_space_id.strip().split())
        if not normalized_space_id:
            raise ValueError("embedding_space_id 不能为空")
        if len(normalized_space_id) > 300:
            raise ValueError("embedding_space_id 最多 300 个字符")
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories
                SET embedding_json = ?, embedding_space_id = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (embedding_json, normalized_space_id, now, memory_id, user_id),
            )
        return cursor.rowcount > 0

    def archive_expired_memories(self, *, user_id: str) -> int:
        """Archive expired temporary memories without erasing version history."""
        now_iso = utc_now_iso()
        now = _parse_iso_datetime(now_iso)
        if now is None:
            return 0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, valid_until
                FROM memories
                WHERE user_id = ? AND archived = 0 AND valid_until IS NOT NULL
                  AND temporal_subject IS NULL
                  AND temporal_predicate IS NULL
                  AND supersedes IS NULL
                  AND superseded_by IS NULL
                """,
                (user_id,),
            ).fetchall()
            expired_ids = [
                str(row["id"])
                for row in rows
                if (expires := _parse_iso_datetime(row["valid_until"])) is not None
                and expires < now
            ]
            if not expired_ids:
                return 0
            placeholders = ", ".join("?" for _ in expired_ids)
            cursor = connection.execute(
                f"""
                UPDATE memories
                SET archived = 1, archived_at = ?, updated_at = ?
                WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
                """,
                (now_iso, now_iso, user_id, *expired_ids),
            )
        return int(cursor.rowcount)

    def preview_archived_memory_purge(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
    ) -> dict[str, object]:
        """Build a repeatable, user-scoped purge plan without writing data."""

        requested_ids = sorted(_ordered_unique(memory_ids))
        with self._connect() as connection:
            connection.execute("BEGIN")
            snapshot = _build_batch_purge_snapshot(
                connection,
                user_id=user_id,
                requested_memory_ids=requested_ids,
            )
        return {
            "requested_memory_ids": snapshot.requested_memory_ids,
            "purge_memory_ids": snapshot.purge_memory_ids,
            "dependent_memory_ids": snapshot.dependent_memory_ids,
            "affected_core_memory_sections": snapshot.affected_core_memory_sections,
            "fingerprint": snapshot.fingerprint,
            "effects": snapshot.effects,
        }

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
        """Revalidate and apply one batch purge in one immediate transaction."""

        requested_ids = sorted(_ordered_unique(memory_ids))
        purged_at = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = _build_batch_purge_snapshot(
                connection,
                user_id=user_id,
                requested_memory_ids=requested_ids,
            )
            if (
                len(snapshot.purge_memory_ids) != expected_purge_memory_count
                or purge_memory_ids_digest(snapshot.purge_memory_ids)
                != expected_purge_memory_ids_digest
                or snapshot.fingerprint != expected_fingerprint
            ):
                raise PurgePreviewConflictError(
                    code="purge_preview_stale",
                    message="永久删除预览已过期：所选记忆、依赖闭包或 Core 影响已变化。",
                )
            effects = _apply_batch_purge_snapshot(
                connection,
                user_id=user_id,
                snapshot=snapshot,
                purged_at=purged_at,
            )
            log = _insert_batch_purge_audit(
                connection,
                user_id=user_id,
                snapshot=snapshot,
                effects=effects,
                fingerprint=expected_fingerprint,
                purged_at=purged_at,
                call_source=call_source,
            )
        return (
            {
                "requested_memory_ids": snapshot.requested_memory_ids,
                "purged_memory_ids": snapshot.purge_memory_ids,
                "dependent_memory_ids": snapshot.dependent_memory_ids,
                "affected_core_memory_sections": snapshot.affected_core_memory_sections,
                "fingerprint": snapshot.fingerprint,
                "effects": effects,
            },
            log,
        )

    def purge_archived_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
        affected_core_sections: list[dict] | None = None,
        call_source: str = "rest_api",
    ) -> tuple[MemoryRecord, DecisionLog] | None:
        purged_at = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 1
                """,
                (memory_id, user_id),
            ).fetchone()
            if row is None:
                return None

            space_rows = connection.execute(
                """
                SELECT space_id
                FROM memory_space_links
                WHERE user_id = ? AND memory_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (user_id, memory_id),
            ).fetchall()
            memory = self._row_to_memory(
                row,
                space_ids=[str(space_row["space_id"]) for space_row in space_rows],
            )
            scrubbed_artifacts = _scrub_purged_memory_artifacts(
                connection,
                memory=memory,
                purged_at=purged_at,
            )
            internally_affected_core = scrubbed_artifacts.pop(
                "affected_core_sections",
                [],
            )
            affected_core_audit = _merge_core_section_audit_summaries(
                affected_core_sections or [],
                internally_affected_core,
            )
            log = DecisionLog(
                id=new_memory_id(),
                user_id=user_id,
                conversation_id=None,
                candidate_json=json.dumps(
                    {
                        "source": "permanent_purge",
                        "memory_id": memory.id,
                        "type": memory.type,
                        "sensitivity": memory.sensitivity,
                        "archived_at": memory.archived_at,
                        "purged_at": purged_at,
                        "content_length": len(memory.content),
                        "content_sha256": hashlib.sha256(
                            memory.content.encode("utf-8")
                        ).hexdigest(),
                        "call_source": call_source,
                        "affected_core_sections": affected_core_audit,
                        "scrubbed_artifacts": scrubbed_artifacts,
                    },
                    ensure_ascii=False,
                ),
                decision="purge",
                reason="永久删除回收站记忆",
                created_at=purged_at,
            )
            connection.execute(
                """
                INSERT INTO memory_decision_logs (
                    id, user_id, conversation_id, candidate_json, decision, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log.id,
                    log.user_id,
                    log.conversation_id,
                    log.candidate_json,
                    log.decision,
                    log.reason,
                    log.created_at,
                ),
            )
            connection.execute(
                """
                DELETE FROM memory_space_links
                WHERE user_id = ? AND memory_id = ?
                """,
                (user_id, memory_id),
            )
            cursor = connection.execute(
                """
                DELETE FROM memories
                WHERE id = ? AND user_id = ? AND archived = 1
                """,
                (memory_id, user_id),
            )
            if cursor.rowcount == 0:
                raise RuntimeError("Purge target disappeared during transaction.")
        return memory, log

    def list_purge_affected_core_sections(
        self,
        *,
        memory_id: str,
        user_id: str,
    ) -> list[CoreMemorySection]:
        """Preview active core sections affected by a user-scoped purge closure."""
        with self._connect() as connection:
            target = connection.execute(
                """
                SELECT id FROM memories
                WHERE id = ? AND user_id = ? AND archived = 1
                """,
                (memory_id, user_id),
            ).fetchone()
            if target is None:
                return []
            affected_ids, _ = _derived_memory_dependency_closure(
                connection,
                user_id=user_id,
                root_memory_id=memory_id,
            )
            rows = connection.execute(
                """
                SELECT * FROM core_memory_sections
                WHERE user_id = ? AND archived = 0
                """,
                (user_id,),
            ).fetchall()
        return [
            self._row_to_core_memory_section(row)
            for row in rows
            if affected_ids.intersection(
                _json_string_list(row["evidence_memory_ids_json"])
            )
        ]

    def upsert_memory_space(self, *, user_id: str, name: str) -> MemorySpace:
        display_name = normalize_classification_name(name, field_name="space")
        with self._connect() as connection:
            return self._upsert_memory_space_on_connection(
                connection=connection,
                user_id=user_id,
                display_name=display_name,
            )

    def _upsert_memory_space_on_connection(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        display_name: str,
    ) -> MemorySpace:
        normalized_name = display_name.casefold()
        now = utc_now_iso()
        row = connection.execute(
            """
            SELECT * FROM memory_spaces
            WHERE user_id = ? AND normalized_name = ?
            """,
            (user_id, normalized_name),
        ).fetchone()
        if row is not None:
            connection.execute(
                """
                UPDATE memory_spaces
                SET name = ?, updated_at = ?, archived = 0
                WHERE id = ? AND user_id = ?
                """,
                (display_name, now, row["id"], user_id),
            )
            updated = connection.execute(
                "SELECT * FROM memory_spaces WHERE id = ? AND user_id = ?",
                (row["id"], user_id),
            ).fetchone()
            return self._row_to_memory_space(updated)

        space = MemorySpace(
            id=new_memory_id(),
            user_id=user_id,
            name=display_name,
            normalized_name=normalized_name,
            created_at=now,
            updated_at=now,
            archived=0,
        )
        connection.execute(
            """
            INSERT INTO memory_spaces (
                id, user_id, name, normalized_name, created_at, updated_at, archived
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                space.id,
                space.user_id,
                space.name,
                space.normalized_name,
                space.created_at,
                space.updated_at,
                space.archived,
            ),
        )
        return space

    def prepare_memory_space_import(
        self,
        *,
        data: dict,
    ) -> dict[str, object] | None:
        """Validate one exported space without opening or mutating SQLite."""
        raw_name = data.get("name")
        if not isinstance(raw_name, str):
            return None
        try:
            display_name = normalize_classification_name(raw_name, field_name="space")
        except ValueError:
            return None
        source_id = str(data.get("id") or "").strip()
        now = utc_now_iso()
        return {
            "source_id": source_id,
            "requested_id": source_id or new_memory_id(),
            "name": display_name,
            "normalized_name": display_name.casefold(),
            "created_at": str(data.get("created_at") or now),
            "archived": 1 if data.get("archived") else 0,
        }

    def import_memory_space(
        self,
        *,
        user_id: str,
        data: dict,
        overwrite: bool = False,
    ) -> tuple[str, MemorySpace | None, str | None]:
        raw_name = data.get("name")
        if not isinstance(raw_name, str):
            return "invalid", None, None
        try:
            display_name = normalize_classification_name(raw_name, field_name="space")
        except ValueError:
            return "invalid", None, None
        old_id = str(data.get("id") or "")
        normalized_name = display_name.casefold()
        now = utc_now_iso()
        space_id = old_id or new_memory_id()
        with self._connect() as connection:
            existing_id = connection.execute(
                "SELECT user_id FROM memory_spaces WHERE id = ?",
                (space_id,),
            ).fetchone()
            if existing_id is not None and existing_id["user_id"] != user_id:
                space_id = new_memory_id()

            existing_name = connection.execute(
                """
                SELECT * FROM memory_spaces
                WHERE user_id = ? AND normalized_name = ?
                """,
                (user_id, normalized_name),
            ).fetchone()
            if existing_name is not None and (
                existing_name["id"] != space_id or not overwrite
            ):
                connection.execute(
                    """
                    UPDATE memory_spaces
                    SET name = ?, updated_at = ?, archived = 0
                    WHERE id = ? AND user_id = ?
                    """,
                    (display_name, now, existing_name["id"], user_id),
                )
                updated = connection.execute(
                    "SELECT * FROM memory_spaces WHERE id = ? AND user_id = ?",
                    (existing_name["id"], user_id),
                ).fetchone()
                return "updated", self._row_to_memory_space(updated), old_id or existing_name["id"]

            existing_same_id = connection.execute(
                "SELECT * FROM memory_spaces WHERE id = ? AND user_id = ?",
                (space_id, user_id),
            ).fetchone()
            if existing_same_id is not None:
                if not overwrite:
                    return "skipped", self._row_to_memory_space(existing_same_id), old_id or space_id
                connection.execute(
                    """
                    UPDATE memory_spaces
                    SET name = ?, normalized_name = ?, updated_at = ?, archived = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        display_name,
                        normalized_name,
                        now,
                        1 if data.get("archived") else 0,
                        space_id,
                        user_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM memory_spaces WHERE id = ? AND user_id = ?",
                    (space_id, user_id),
                ).fetchone()
                return "updated", self._row_to_memory_space(updated), old_id or space_id

            space = MemorySpace(
                id=space_id,
                user_id=user_id,
                name=display_name,
                normalized_name=normalized_name,
                created_at=str(data.get("created_at") or now),
                updated_at=now,
                archived=1 if data.get("archived") else 0,
            )
            connection.execute(
                """
                INSERT INTO memory_spaces (
                    id, user_id, name, normalized_name, created_at, updated_at, archived
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    space.id,
                    space.user_id,
                    space.name,
                    space.normalized_name,
                    space.created_at,
                    space.updated_at,
                    space.archived,
                ),
            )
        return "created", space, old_id or space_id

    def list_memory_spaces(
        self,
        *,
        user_id: str,
        include_archived: bool = False,
    ) -> list[MemorySpace]:
        query = "SELECT * FROM memory_spaces WHERE user_id = ?"
        params: list[object] = [user_id]
        if not include_archived:
            query += " AND archived = 0"
        query += " ORDER BY updated_at DESC, name ASC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_memory_space(row) for row in rows]

    def list_memory_space_summaries(self, *, user_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.*,
                    COUNT(m.id) AS active_memory_count,
                    MAX(m.updated_at) AS last_memory_updated_at
                FROM memory_spaces AS s
                LEFT JOIN memory_space_links AS l
                    ON l.user_id = s.user_id AND l.space_id = s.id
                LEFT JOIN memories AS m
                    ON m.user_id = s.user_id
                    AND m.id = l.memory_id
                    AND m.archived = 0
                WHERE s.user_id = ? AND s.archived = 0
                GROUP BY s.id
                ORDER BY active_memory_count DESC, s.updated_at DESC, s.name ASC
                """,
                (user_id,),
            ).fetchall()
        summaries: list[dict] = []
        for row in rows:
            space = self._row_to_memory_space(row)
            payload = space.model_dump()
            payload["active_memory_count"] = int(row["active_memory_count"] or 0)
            payload["last_memory_updated_at"] = row["last_memory_updated_at"]
            summaries.append(payload)
        return summaries

    def get_memory_space(self, *, user_id: str, space_id: str) -> MemorySpace | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_spaces
                WHERE user_id = ? AND id = ? AND archived = 0
                """,
                (user_id, space_id),
            ).fetchone()
        return self._row_to_memory_space(row) if row else None

    def list_memories_for_space(
        self,
        *,
        user_id: str,
        space_id: str,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*
                FROM memories AS m
                INNER JOIN memory_space_links AS l
                    ON l.user_id = m.user_id AND l.memory_id = m.id
                WHERE m.user_id = ?
                    AND l.space_id = ?
                    AND m.archived = 0
                ORDER BY m.importance DESC, m.updated_at DESC
                LIMIT ?
                """,
                (user_id, space_id, limit),
            ).fetchall()
        return self._rows_to_memories(rows)

    def replace_memory_spaces(
        self,
        *,
        memory_id: str,
        user_id: str,
        space_ids: list[str],
        create_space_names: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> MemoryRecord | None:
        normalized_space_ids = _ordered_unique(
            [str(space_id).strip() for space_id in space_ids if str(space_id).strip()]
        )
        create_space_names = create_space_names or []
        if len(normalized_space_ids) + len(create_space_names) > 10:
            raise ValueError("space_ids 最多 10 个")
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            memory_exists = connection.execute(
                """
                SELECT id, revision FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if memory_exists is None:
                return None
            current_revision = max(1, int(memory_exists["revision"] or 1))
            if (
                expected_revision is not None
                and int(expected_revision) != current_revision
            ):
                raise RevisionConflictError(
                    resource="memory",
                    resource_id=memory_id,
                    expected_revision=int(expected_revision),
                    current_revision=current_revision,
                )
            created_spaces = [
                self._upsert_memory_space_on_connection(
                    connection=connection,
                    user_id=user_id,
                    display_name=normalize_classification_name(name, field_name="space"),
                )
                for name in create_space_names
            ]
            normalized_space_ids = _ordered_unique(
                [*normalized_space_ids, *(space.id for space in created_spaces)]
            )
            if len(normalized_space_ids) > 10:
                raise ValueError("space_ids 最多 10 个")
            self._validate_space_ids(
                connection=connection,
                user_id=user_id,
                space_ids=normalized_space_ids,
            )
            self._replace_memory_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=memory_id,
                space_ids=normalized_space_ids,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE memories
                SET updated_at = ?, revision = revision + 1
                WHERE id = ? AND user_id = ? AND archived = 0 AND revision = ?
                """,
                (now, memory_id, user_id, current_revision),
            )
            updated_row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if updated_row is None:
                raise RuntimeError("Memory space update did not persist.")
            return self._row_to_memory(
                updated_row,
                space_ids=normalized_space_ids,
            )

    def plan_memory_import_ids(
        self,
        *,
        user_id: str,
        source_ids: list[str],
        rebind_all: bool = False,
    ) -> dict[str, str]:
        """Allocate stable target IDs before restore so graph references can follow."""
        ordered_ids = _ordered_unique(
            [str(memory_id).strip() for memory_id in source_ids if str(memory_id).strip()]
        )
        if not ordered_ids:
            return {}
        with self._connect() as connection:
            return self._plan_memory_import_ids_on_connection(
                connection=connection,
                user_id=user_id,
                source_ids=ordered_ids,
                rebind_all=rebind_all,
            )

    @staticmethod
    def _plan_memory_import_ids_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        source_ids: list[str],
        rebind_all: bool,
    ) -> dict[str, str]:
        ordered_ids = _ordered_unique(
            [str(memory_id).strip() for memory_id in source_ids if str(memory_id).strip()]
        )
        owners: dict[str, str] = {}
        for offset in range(0, len(ordered_ids), 500):
            batch = ordered_ids[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT id, user_id FROM memories WHERE id IN ({placeholders})",
                tuple(batch),
            ).fetchall()
            owners.update(
                {str(row["id"]): str(row["user_id"] or "default") for row in rows}
            )

        result: dict[str, str] = {}
        allocated = set(ordered_ids)
        for source_id in ordered_ids:
            owner = owners.get(source_id)
            if not rebind_all and (owner is None or owner == user_id):
                result[source_id] = source_id
                continue
            while True:
                target_id = new_memory_id()
                if target_id in allocated:
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM memories WHERE id = ?",
                    (target_id,),
                ).fetchone()
                if exists is None:
                    break
            result[source_id] = target_id
            allocated.add(target_id)
        return result

    def filter_existing_memory_ids(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> set[str]:
        """Return IDs that currently belong to the user, including archived rows."""
        ordered_ids = _ordered_unique(
            [str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()]
        )
        with self._connect() as connection:
            return self._filter_existing_memory_ids_on_connection(
                connection=connection,
                user_id=user_id,
                memory_ids=ordered_ids,
            )

    @staticmethod
    def _filter_existing_memory_ids_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory_ids: list[str],
    ) -> set[str]:
        ordered_ids = _ordered_unique(
            [str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()]
        )
        existing: set[str] = set()
        for offset in range(0, len(ordered_ids), 500):
            batch = ordered_ids[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT id FROM memories
                WHERE user_id = ? AND id IN ({placeholders})
                """,
                (user_id, *batch),
            ).fetchall()
            existing.update(str(row["id"]) for row in rows)
        return existing

    def prune_dangling_memory_references(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> int:
        """Remove graph references that do not resolve inside the current user."""
        target_ids = _ordered_unique(
            [str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()]
        )
        if not target_ids:
            return 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._prune_dangling_memory_references_on_connection(
                connection=connection,
                user_id=user_id,
                memory_ids=target_ids,
            )

    @staticmethod
    def _prune_dangling_memory_references_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory_ids: list[str],
    ) -> int:
        target_ids = _ordered_unique(
            [str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()]
        )
        if not target_ids:
            return 0
        now = utc_now_iso()
        changed_references = 0
        existing_ids = {
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM memories WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        }
        for offset in range(0, len(target_ids), 500):
            batch = target_ids[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT id, evidence_memory_ids_json, supersedes, superseded_by
                FROM memories
                WHERE user_id = ? AND id IN ({placeholders})
                """,
                (user_id, *batch),
            ).fetchall()
            for row in rows:
                evidence_before = _json_string_list(row["evidence_memory_ids_json"])
                evidence_after = [
                    memory_id
                    for memory_id in evidence_before
                    if memory_id in existing_ids
                ]
                supersedes_before = str(row["supersedes"]) if row["supersedes"] else None
                superseded_by_before = (
                    str(row["superseded_by"]) if row["superseded_by"] else None
                )
                supersedes_after = (
                    supersedes_before if supersedes_before in existing_ids else None
                )
                superseded_by_after = (
                    superseded_by_before if superseded_by_before in existing_ids else None
                )
                removed = (
                    len(evidence_before) - len(evidence_after)
                    + int(bool(supersedes_before and not supersedes_after))
                    + int(bool(superseded_by_before and not superseded_by_after))
                )
                if removed <= 0:
                    continue
                connection.execute(
                    """
                    UPDATE memories
                    SET evidence_memory_ids_json = ?, supersedes = ?,
                        superseded_by = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        json.dumps(evidence_after, ensure_ascii=False),
                        supersedes_after,
                        superseded_by_after,
                        now,
                        row["id"],
                        user_id,
                    ),
                )
                changed_references += removed
        return changed_references

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
        """Apply one fully prepared export in a single immediate transaction."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            # Finish every database-dependent mapping before the first write.
            space_plans, space_id_map = self._plan_memory_space_imports_on_connection(
                connection=connection,
                user_id=user_id,
                prepared_spaces=prepared_spaces,
                overwrite=overwrite,
            )
            memory_id_map = self._plan_memory_import_ids_on_connection(
                connection=connection,
                user_id=user_id,
                source_ids=source_memory_ids,
                rebind_all=bool(exported_user_id and exported_user_id != user_id),
            )
            preserve_existing_references = (
                not exported_user_id or exported_user_id == user_id
            )
            allowed_existing_ids = (
                self._filter_existing_memory_ids_on_connection(
                    connection=connection,
                    user_id=user_id,
                    memory_ids=referenced_source_ids,
                )
                if preserve_existing_references
                else set()
            )
            mapped_memories: list[MemoryRecord] = []
            for source_id, prepared in prepared_memories:
                memory = prepared.model_copy(deep=True)
                if source_id in memory_id_map:
                    memory.id = memory_id_map[source_id]
                memory.space_ids = _ordered_unique(
                    [
                        mapped
                        for source_space_id in memory.space_ids
                        if (mapped := space_id_map.get(source_space_id))
                    ]
                )[:10]
                mapped_evidence: list[str] = []
                for evidence_id in memory.evidence_memory_ids:
                    mapped_id = memory_id_map.get(evidence_id)
                    if mapped_id is None and evidence_id in allowed_existing_ids:
                        mapped_id = evidence_id
                    if mapped_id and mapped_id not in mapped_evidence:
                        mapped_evidence.append(mapped_id)
                memory.evidence_memory_ids = mapped_evidence
                for field_name in ("supersedes", "superseded_by"):
                    reference = getattr(memory, field_name)
                    mapped_reference = memory_id_map.get(reference) if reference else None
                    if mapped_reference is None and reference in allowed_existing_ids:
                        mapped_reference = reference
                    setattr(memory, field_name, mapped_reference)
                mapped_memories.append(memory)

            # No validation, parsing or identifier allocation occurs below this
            # point. Any unexpected exception escapes the context manager and
            # rolls back every already-written partition.
            for plan in space_plans:
                self._apply_memory_space_import_plan_on_connection(
                    connection=connection,
                    user_id=user_id,
                    plan=plan,
                )

            memory_results: list[tuple[str, MemoryRecord | None]] = []
            imported_memory_ids: list[str] = []
            for memory in mapped_memories:
                action, persisted = self._import_prepared_memory_record_on_connection(
                    connection=connection,
                    user_id=user_id,
                    memory=memory,
                    overwrite=overwrite,
                    rebind_on_conflict=False,
                )
                memory_results.append((action, persisted))
                if persisted is not None and action in {"created", "updated"}:
                    imported_memory_ids.append(persisted.id)

            dangling_removed = self._prune_dangling_memory_references_on_connection(
                connection=connection,
                user_id=user_id,
                memory_ids=imported_memory_ids,
            )
            final_existing_ids = self._filter_existing_memory_ids_on_connection(
                connection=connection,
                user_id=user_id,
                memory_ids=[*memory_id_map.values(), *referenced_source_ids],
            )

            recent_context_actions = [
                self._restore_recent_context_on_connection(
                    connection=connection,
                    user_id=user_id,
                    prepared=prepared,
                    overwrite=overwrite,
                )
                for prepared in recent_contexts
            ]
            branch_node_actions = [
                self._restore_branch_node_on_connection(
                    connection=connection,
                    user_id=user_id,
                    prepared=prepared,
                    overwrite=overwrite,
                )
                for prepared in branch_nodes
            ]
            result: dict[str, object] = {
                "space_results": [
                    (str(plan["action"]), plan["space"])
                    for plan in space_plans
                ],
                "memory_results": memory_results,
                "recent_context_actions": recent_context_actions,
                "branch_node_actions": branch_node_actions,
                "dangling_references_removed": dangling_removed,
                "final_existing_ids": final_existing_ids,
            }
            if dry_run:
                connection.rollback()
            return result

    def _plan_memory_space_imports_on_connection(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        prepared_spaces: list[dict[str, object]],
        overwrite: bool,
    ) -> tuple[list[dict[str, object]], dict[str, str]]:
        target_rows = connection.execute(
            "SELECT * FROM memory_spaces WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        by_id = {
            str(row["id"]): self._row_to_memory_space(row)
            for row in target_rows
        }
        by_name = {space.normalized_name: space for space in by_id.values()}
        requested_ids = _ordered_unique(
            [str(item["requested_id"]) for item in prepared_spaces]
        )
        owners: dict[str, str] = {}
        for offset in range(0, len(requested_ids), 500):
            batch = requested_ids[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT id, user_id FROM memory_spaces WHERE id IN ({placeholders})",
                tuple(batch),
            ).fetchall()
            owners.update(
                {str(row["id"]): str(row["user_id"] or "default") for row in rows}
            )
        reserved_ids = set(requested_ids) | set(owners)

        def allocate_id() -> str:
            while True:
                candidate = new_memory_id()
                if candidate in reserved_ids:
                    continue
                if connection.execute(
                    "SELECT 1 FROM memory_spaces WHERE id = ?",
                    (candidate,),
                ).fetchone() is None:
                    reserved_ids.add(candidate)
                    return candidate

        plans: list[dict[str, object]] = []
        space_id_map: dict[str, str] = {}
        for prepared in prepared_spaces:
            source_id = str(prepared["source_id"])
            requested_id = str(prepared["requested_id"])
            if owners.get(requested_id) not in {None, user_id}:
                requested_id = allocate_id()
            name = str(prepared["name"])
            normalized_name = str(prepared["normalized_name"])
            now = utc_now_iso()
            existing_name = by_name.get(normalized_name)
            if existing_name is not None and (
                existing_name.id != requested_id or not overwrite
            ):
                space = existing_name.model_copy(
                    update={"name": name, "updated_at": now, "archived": 0}
                )
                action = "updated"
                operation = "update_name"
            else:
                existing_id = by_id.get(requested_id)
                if existing_id is not None and not overwrite:
                    space = existing_id
                    action = "skipped"
                    operation = "none"
                elif existing_id is not None:
                    old_normalized_name = existing_id.normalized_name
                    space = existing_id.model_copy(
                        update={
                            "name": name,
                            "normalized_name": normalized_name,
                            "updated_at": now,
                            "archived": int(prepared["archived"]),
                        }
                    )
                    by_name.pop(old_normalized_name, None)
                    action = "updated"
                    operation = "update_all"
                else:
                    space = MemorySpace(
                        id=requested_id,
                        user_id=user_id,
                        name=name,
                        normalized_name=normalized_name,
                        created_at=str(prepared["created_at"]),
                        updated_at=now,
                        archived=int(prepared["archived"]),
                    )
                    action = "created"
                    operation = "insert"
            by_id[space.id] = space
            by_name[space.normalized_name] = space
            owners[space.id] = user_id
            plans.append(
                {
                    "action": action,
                    "operation": operation,
                    "space": space,
                }
            )
            space_id_map[source_id or space.id] = space.id
        return plans, space_id_map

    @staticmethod
    def _apply_memory_space_import_plan_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        plan: dict[str, object],
    ) -> None:
        operation = str(plan["operation"])
        space = plan["space"]
        if not isinstance(space, MemorySpace) or operation == "none":
            return
        if operation == "insert":
            connection.execute(
                """
                INSERT INTO memory_spaces (
                    id, user_id, name, normalized_name, created_at, updated_at, archived
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    space.id,
                    space.user_id,
                    space.name,
                    space.normalized_name,
                    space.created_at,
                    space.updated_at,
                    space.archived,
                ),
            )
        elif operation == "update_name":
            connection.execute(
                """
                UPDATE memory_spaces
                SET name = ?, updated_at = ?, archived = 0
                WHERE id = ? AND user_id = ?
                """,
                (space.name, space.updated_at, space.id, user_id),
            )
        elif operation == "update_all":
            connection.execute(
                """
                UPDATE memory_spaces
                SET name = ?, normalized_name = ?, updated_at = ?, archived = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    space.name,
                    space.normalized_name,
                    space.updated_at,
                    space.archived,
                    space.id,
                    user_id,
                ),
            )

    @staticmethod
    def _restore_recent_context_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        prepared: dict[str, object],
        overwrite: bool,
    ) -> str:
        conversation_id = prepared["conversation_id"]
        if conversation_id is None:
            row = connection.execute(
                """
                SELECT id FROM recent_context_summaries
                WHERE user_id = ? AND conversation_id IS NULL AND archived = 0
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT id FROM recent_context_summaries
                WHERE user_id = ? AND conversation_id = ? AND archived = 0
                LIMIT 1
                """,
                (user_id, conversation_id),
            ).fetchone()
        if row is not None and not overwrite:
            return "skipped"
        turns = prepared["recent_turns"]
        if not isinstance(turns, list):
            raise RuntimeError("prepared recent context turns are invalid")
        turns_json = json.dumps(
            [turn.model_dump() for turn in turns if isinstance(turn, RecentContextTurn)],
            ensure_ascii=False,
        )
        now = utc_now_iso()
        if row is not None:
            connection.execute(
                """
                UPDATE recent_context_summaries
                SET summary = ?, compressed_summary = ?, recent_turns_json = ?,
                    turn_count = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (
                    str(prepared["summary"]),
                    str(prepared["compressed_summary"]),
                    turns_json,
                    int(prepared["turn_count"]),
                    now,
                    row["id"],
                    user_id,
                ),
            )
            return "updated"
        connection.execute(
            """
            INSERT INTO recent_context_summaries (
                id, user_id, conversation_id, summary, compressed_summary,
                recent_turns_json, turn_count, created_at, updated_at, archived
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                new_memory_id(),
                user_id,
                conversation_id,
                str(prepared["summary"]),
                str(prepared["compressed_summary"]),
                turns_json,
                int(prepared["turn_count"]),
                now,
                now,
            ),
        )
        return "created"

    @staticmethod
    def _restore_branch_node_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        prepared: dict[str, object],
        overwrite: bool,
    ) -> str:
        history_fingerprint = str(prepared["history_fingerprint"])
        existing = connection.execute(
            """
            SELECT id FROM conversation_branch_nodes
            WHERE user_id = ? AND history_fingerprint = ? AND archived = 0
            LIMIT 1
            """,
            (user_id, history_fingerprint),
        ).fetchone()
        if existing is not None and not overwrite:
            return "skipped"
        node_id = "branch-" + hashlib.sha256(
            f"{user_id}\0{history_fingerprint}".encode("utf-8")
        ).hexdigest()[:32]
        turns = prepared["recent_turns"]
        if not isinstance(turns, list):
            raise RuntimeError("prepared branch turns are invalid")
        turns_json = json.dumps(
            [turn.model_dump() for turn in turns if isinstance(turn, RecentContextTurn)],
            ensure_ascii=False,
        )
        now = utc_now_iso()
        connection.execute(
            """
            INSERT INTO conversation_branch_nodes (
                id, user_id, conversation_id, history_fingerprint,
                parent_history_fingerprint, turn_fingerprint,
                assistant_digest, summary, compressed_summary,
                recent_turns_json, turn_count, created_at, updated_at, archived
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id)
            DO UPDATE SET
                user_id = excluded.user_id,
                conversation_id = excluded.conversation_id,
                history_fingerprint = excluded.history_fingerprint,
                parent_history_fingerprint = excluded.parent_history_fingerprint,
                turn_fingerprint = excluded.turn_fingerprint,
                assistant_digest = excluded.assistant_digest,
                summary = excluded.summary,
                compressed_summary = excluded.compressed_summary,
                recent_turns_json = excluded.recent_turns_json,
                turn_count = excluded.turn_count,
                updated_at = excluded.updated_at,
                archived = 0
            """,
            (
                node_id,
                user_id,
                prepared["conversation_id"],
                history_fingerprint,
                str(prepared["parent_history_fingerprint"]),
                str(prepared["turn_fingerprint"]),
                str(prepared["assistant_digest"]),
                str(prepared["summary"]),
                str(prepared["compressed_summary"]),
                turns_json,
                int(prepared["turn_count"]),
                now,
                now,
            ),
        )
        connection.execute(
            """
            DELETE FROM conversation_branch_nodes
            WHERE user_id = ? AND id IN (
                SELECT id FROM conversation_branch_nodes
                WHERE user_id = ? AND archived = 0
                ORDER BY updated_at DESC, created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (user_id, user_id, _CONVERSATION_BRANCH_NODE_RETENTION_LIMIT),
        )
        return "created" if existing is None else "updated"

    def prepare_memory_import_record(
        self,
        *,
        user_id: str,
        data: dict,
        archived: int | None = None,
        space_id_map: dict[str, str] | None = None,
    ) -> MemoryRecord | None:
        """Validate and normalize an import row without touching SQLite."""
        content = str(data.get("content") or "").strip()
        if not content:
            return None

        memory_id = str(data.get("id") or new_memory_id())

        now = utc_now_iso()
        try:
            archived_value = int(data.get("archived", 0) if archived is None else archived)
            archived_value = 1 if archived_value else 0
            evidence_memory_ids = _coerce_string_list(data.get("evidence_memory_ids"))
            topics = normalize_classification_names(
                _coerce_string_list(data.get("topics")),
                max_items=20,
                field_name="topics",
            )
            entities = normalize_classification_names(
                _coerce_string_list(data.get("entities")),
                max_items=20,
                field_name="entities",
            )
        except (TypeError, ValueError):
            return None
        archived_at = str(data.get("archived_at") or now) if archived_value else None
        raw_space_ids = _coerce_string_list(data.get("space_ids"))
        if space_id_map is not None:
            raw_space_ids = [
                mapped
                for space_id in raw_space_ids
                if (mapped := space_id_map.get(space_id))
            ]
        space_ids = _ordered_unique(raw_space_ids)[:10]

        try:
            memory = MemoryRecord(
                id=memory_id,
                user_id=user_id,
                content=content,
                type=normalize_memory_type(data.get("type", "semantic")),
                importance=_coerce_int(data.get("importance"), default=1),
                confidence=_coerce_float(data.get("confidence"), default=0.7),
                valence=_bounded_float(data.get("valence"), default=0.5),
                arousal=_bounded_float(data.get("arousal"), default=0.3),
                source_message=data.get("source_message"),
                source_conversation_id=data.get("source_conversation_id"),
                origin=data.get("origin", "user_asserted"),
                embedding_json=None,
                embedding_space_id=None,
                last_used_at=data.get("last_used_at"),
                usage_count=max(0.0, _coerce_float(data.get("usage_count"), default=0.0)),
                stability=data.get("stability", "stable"),
                valid_from=data.get("valid_from"),
                valid_until=data.get("valid_until"),
                review_after=data.get("review_after"),
                sensitivity=data.get("sensitivity", "normal"),
                evidence_memory_ids=evidence_memory_ids,
                topics=topics,
                entities=entities,
                space_ids=space_ids,
                temporal_subject=data.get("temporal_subject"),
                temporal_predicate=data.get("temporal_predicate"),
                status=str(data.get("status") or "dynamic"),
                digested=bool(data.get("digested", False)),
                decay_lambda=_coerce_float_or_none(data.get("decay_lambda")),
                supersedes=data.get("supersedes"),
                superseded_by=data.get("superseded_by"),
                created_at=str(data.get("created_at") or now),
                updated_at=now,
                archived_at=archived_at,
                archived=archived_value,
                revision=max(1, _coerce_int(data.get("revision"), default=1)),
            )
            memory.sensitivity = _sensitivity_with_floor(
                declared=memory.sensitivity,
                content=memory.content,
                source_message=memory.source_message,
                entities=memory.entities,
            )
        except (ValidationError, TypeError, ValueError):
            return None

        return memory

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
        memory = self.prepare_memory_import_record(
            user_id=user_id,
            data=data,
            archived=archived,
            space_id_map=space_id_map,
        )
        if memory is None:
            return "invalid", None

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._import_prepared_memory_record_on_connection(
                connection=connection,
                user_id=user_id,
                memory=memory,
                overwrite=overwrite,
                rebind_on_conflict=rebind_on_conflict,
            )

    def _import_prepared_memory_record_on_connection(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory: MemoryRecord,
        overwrite: bool,
        rebind_on_conflict: bool,
    ) -> tuple[str, MemoryRecord | None]:
        """Write a fully validated import row using the caller's transaction."""
        memory = memory.model_copy(deep=True)
        now = utc_now_iso()
        memory.space_ids = self._filter_existing_space_ids(
            connection=connection,
            user_id=user_id,
            space_ids=memory.space_ids,
        )
        row = connection.execute(
            "SELECT * FROM memories WHERE id = ?",
            (memory.id,),
        ).fetchone()
        if row is not None and row["user_id"] != user_id:
            if not rebind_on_conflict:
                return "invalid", None
            while True:
                replacement_id = new_memory_id()
                collision = connection.execute(
                    "SELECT 1 FROM memories WHERE id = ?",
                    (replacement_id,),
                ).fetchone()
                if collision is None:
                    break
            memory.id = replacement_id
            row = None
        elif row is not None and not overwrite:
            return "skipped", None

        existing_memory = (
            self._row_to_memory(row, space_ids=[])
            if row is not None
            else None
        )
        old_temporal_key = (
            (
                existing_memory.temporal_subject,
                existing_memory.temporal_predicate,
            )
            if existing_memory is not None
            and existing_memory.temporal_subject
            and existing_memory.temporal_predicate
            else None
        )
        new_temporal_key = (
            (memory.temporal_subject, memory.temporal_predicate)
            if memory.temporal_subject and memory.temporal_predicate
            else None
        )
        if existing_memory is not None and old_temporal_key != new_temporal_key:
            # Links are meaningful only inside one temporal key.  A partial
            # overwrite must not leave the old chain pointing through a row
            # that has moved to a different key.
            memory.supersedes = None
            memory.superseded_by = None

            old_successor = None
            if existing_memory.superseded_by:
                old_successor = connection.execute(
                    """
                    SELECT valid_from, created_at
                    FROM memories
                    WHERE id = ? AND user_id = ?
                    """,
                    (existing_memory.superseded_by, user_id),
                ).fetchone()
            if old_successor is not None and memory.valid_until is not None:
                old_successor_start = str(
                    old_successor["valid_from"] or old_successor["created_at"]
                )
                if _parse_iso_datetime(memory.valid_until) == _parse_iso_datetime(
                    old_successor_start
                ):
                    memory.valid_until = None
                    if memory.status == "resolved":
                        # The resolved state was derived from that synthesized
                        # boundary; the moved row re-enters its new key as live.
                        memory.status = "dynamic"

        evidence_json = json.dumps(memory.evidence_memory_ids, ensure_ascii=False)
        topics_json = json.dumps(memory.topics, ensure_ascii=False)
        entities_json = json.dumps(memory.entities, ensure_ascii=False)
        params = (
            memory.user_id,
            memory.content,
            memory.type,
            memory.importance,
            memory.confidence,
            memory.valence,
            memory.arousal,
            memory.source_message,
            memory.source_conversation_id,
            memory.origin,
            memory.embedding_json,
            memory.embedding_space_id,
            memory.last_used_at,
            memory.usage_count,
            memory.stability,
            memory.valid_from,
            memory.valid_until,
            memory.review_after,
            memory.sensitivity,
            evidence_json,
            topics_json,
            entities_json,
            memory.temporal_subject,
            memory.temporal_predicate,
            memory.status,
            int(memory.digested),
            memory.decay_lambda,
            memory.supersedes,
            memory.superseded_by,
            memory.created_at,
            memory.updated_at,
            memory.archived_at,
            memory.archived,
            memory.id,
        )
        if row is not None:
            cursor = connection.execute(
                """
                UPDATE memories
                SET user_id = ?, content = ?, type = ?, importance = ?,
                    confidence = ?, valence = ?, arousal = ?, source_message = ?,
                    source_conversation_id = ?, origin = ?, embedding_json = ?,
                    embedding_space_id = ?,
                    last_used_at = ?, usage_count = ?, stability = ?,
                    valid_from = ?, valid_until = ?, review_after = ?, sensitivity = ?,
                    evidence_memory_ids_json = ?, topics_json = ?, entities_json = ?,
                    temporal_subject = ?, temporal_predicate = ?,
                    status = ?, digested = ?, decay_lambda = ?,
                    supersedes = ?, superseded_by = ?,
                    created_at = ?,
                    updated_at = ?, archived_at = ?, archived = ?,
                    revision = revision + 1
                WHERE id = ? AND user_id = ?
                """,
                (*params, user_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Memory import update lost its user-scoped target.")
            self._replace_memory_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=memory.id,
                space_ids=memory.space_ids,
                created_at=now,
            )
            action = "updated"
        else:
            self._insert_memory_row(connection=connection, memory=memory)
            self._replace_memory_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=memory.id,
                space_ids=memory.space_ids,
                created_at=now,
            )
            action = "created"

        temporal_keys = {
            key
            for key in (old_temporal_key, new_temporal_key)
            if key is not None
        }
        for subject, predicate in temporal_keys:
            self._rebuild_temporal_key(
                connection=connection,
                user_id=user_id,
                temporal_subject=subject,
                temporal_predicate=predicate,
            )

        persisted_row = connection.execute(
            "SELECT * FROM memories WHERE id = ? AND user_id = ?",
            (memory.id, user_id),
        ).fetchone()
        if persisted_row is None:
            raise RuntimeError("Memory import did not persist.")
        persisted_space_ids = self._space_ids_for_memory_ids_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=[memory.id],
        ).get(memory.id, [])
        persisted = self._row_to_memory(
            persisted_row,
            space_ids=persisted_space_ids,
        )
        return action, persisted

    def mark_memories_used(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
        time_ripple_delta: float = 0.0,
        time_ripple_window_hours: int = 48,
    ) -> str | None:
        unique_ids = _ordered_unique([str(memory_id) for memory_id in memory_ids if memory_id])
        if not unique_ids:
            return None
        now = utc_now_iso()
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE memories
                SET usage_count = COALESCE(usage_count, 0) + 1,
                    last_used_at = ?
                WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
                """,
                (now, user_id, *unique_ids),
            )
            self._apply_time_ripple(
                connection=connection,
                user_id=user_id,
                seed_ids=unique_ids,
                used_at=now,
                delta=time_ripple_delta,
                window_hours=time_ripple_window_hours,
            )
        return now

    def touch_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
        time_ripple_delta: float = 0.0,
        time_ripple_window_hours: int = 48,
    ) -> None:
        """单条记忆 touch：递增 usage_count 并刷新 last_used_at。"""
        self.mark_memories_used(
            memory_ids=[memory_id],
            user_id=user_id,
            time_ripple_delta=time_ripple_delta,
            time_ripple_window_hours=time_ripple_window_hours,
        )

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
        ripple_delta = _bounded_float(delta, default=0.0)
        if ripple_delta <= 0:
            return
        capped_window_hours = max(1, min(720, _coerce_int(window_hours, default=48)))
        window_seconds = capped_window_hours * 60 * 60

        seed_placeholders = ", ".join("?" for _ in seed_ids)
        seed_rows = connection.execute(
            f"""
            SELECT id, valid_from, created_at, topics_json
            FROM memories
            WHERE user_id = ? AND archived = 0 AND id IN ({seed_placeholders})
            """,
            (user_id, *seed_ids),
        ).fetchall()
        seed_profiles = _time_ripple_profiles(seed_rows)
        if not seed_profiles:
            return

        seed_id_set = set(seed_profiles)
        link_rows = connection.execute(
            """
            SELECT memory_id, space_id
            FROM memory_space_links
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
        spaces_by_memory_id: dict[str, set[str]] = {}
        for row in link_rows:
            spaces_by_memory_id.setdefault(str(row["memory_id"]), set()).add(str(row["space_id"]))
        for memory_id, profile in seed_profiles.items():
            profile["spaces"] = spaces_by_memory_id.get(memory_id, set())

        candidate_rows = connection.execute(
            f"""
            SELECT id, valid_from, created_at, topics_json
            FROM memories
            WHERE user_id = ?
              AND archived = 0
              AND id NOT IN ({seed_placeholders})
              AND COALESCE(status, 'dynamic') IN ('dynamic', 'resolved')
              AND COALESCE(sensitivity, 'normal') NOT IN ('private', 'sensitive')
              AND COALESCE(origin, 'user_asserted') = 'user_asserted'
            """,
            (user_id, *seed_ids),
        ).fetchall()
        scored_candidates: list[tuple[int, float, str]] = []
        for row in candidate_rows:
            candidate_id = str(row["id"])
            if candidate_id in seed_id_set:
                continue
            candidate_anchor = _time_ripple_anchor(row)
            if candidate_anchor is None:
                continue
            candidate_topics = _casefold_set(_json_string_list(row["topics_json"]))
            candidate_spaces = spaces_by_memory_id.get(candidate_id, set())

            best_shared = 0
            best_distance = float("inf")
            for profile in seed_profiles.values():
                shared_count = len(candidate_spaces & profile["spaces"])
                shared_count += len(candidate_topics & profile["topics"])
                if shared_count <= 0:
                    continue
                distance_seconds = abs((candidate_anchor - profile["anchor"]).total_seconds())
                if distance_seconds > window_seconds:
                    continue
                if shared_count > best_shared or (
                    shared_count == best_shared and distance_seconds < best_distance
                ):
                    best_shared = shared_count
                    best_distance = distance_seconds
            if best_shared > 0:
                scored_candidates.append((best_shared, best_distance, candidate_id))

        if not scored_candidates:
            return
        scored_candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        ripple_ids = [item[2] for item in scored_candidates[:_TIME_RIPPLE_MAX_CANDIDATES]]
        ripple_placeholders = ", ".join("?" for _ in ripple_ids)
        connection.execute(
            f"""
            UPDATE memories
            SET usage_count = COALESCE(usage_count, 0) + ?,
                last_used_at = ?
            WHERE user_id = ? AND archived = 0 AND id IN ({ripple_placeholders})
            """,
            (ripple_delta, used_at, user_id, *ripple_ids),
        )

    def list_undigested_memories(
        self, *, user_id: str, limit: int = 10, include_sensitive: bool = False
    ) -> list[MemoryRecord]:
        """返回近期未消化的记忆，供 digest_memories 使用。"""
        with self._connect() as connection:
            sensitivity_sql = "" if include_sensitive else "AND COALESCE(sensitivity, 'normal') = 'normal'"
            rows = connection.execute(
                f"""
                SELECT * FROM memories
                WHERE user_id = ? AND archived = 0 AND digested = 0
                  AND COALESCE(origin, 'user_asserted') = 'user_asserted'
                  {sensitivity_sql}
                ORDER BY updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        memories = self._rows_to_memories(rows)
        if not include_sensitive:
            memories = [
                memory
                for memory in memories
                if _sensitivity_with_floor(
                    declared=memory.sensitivity,
                    content=memory.content,
                    source_message=memory.source_message,
                    entities=memory.entities,
                )
                == "normal"
            ]
        return memories[: max(0, limit)]

    def get_digest_source_memories(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
        include_sensitive: bool = False,
    ) -> list[MemoryRecord]:
        """Return every requested digest source or reject the whole set."""
        source_ids = _ordered_unique(
            [str(memory_id) for memory_id in memory_ids if memory_id]
        )
        if not source_ids:
            raise ValueError("source_ids must contain at least one memory ID")
        with self._connect() as connection:
            rows = self._validated_digest_source_rows(
                connection=connection,
                user_id=user_id,
                source_ids=source_ids,
                include_sensitive=include_sensitive,
            )
        return self._rows_to_memories(rows)

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
        """Persist a validated digestion result as one atomic change."""
        source_ids = _ordered_unique(
            [str(memory_id) for memory_id in source_ids if memory_id]
        )
        resolved_ids = _ordered_unique(
            [str(memory_id) for memory_id in resolved_ids if memory_id]
        )
        if not source_ids:
            raise ValueError("source_ids must contain at least one memory ID")
        source_id_set = set(source_ids)
        invalid_resolved_ids = [
            memory_id for memory_id in resolved_ids if memory_id not in source_id_set
        ]
        if invalid_resolved_ids:
            raise ValueError("resolved_ids must be a subset of source_ids")

        reflection = reflection.strip()
        feel = feel.strip()
        now = utc_now_iso()
        created: list[MemoryRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_rows = self._validated_digest_source_rows(
                connection=connection,
                user_id=user_id,
                source_ids=source_ids,
                include_sensitive=include_sensitive,
            )
            source_sensitivities = (
                str(row["sensitivity"] or "normal") for row in source_rows
            )
            sensitivity = max(
                (
                    value if value in _SENSITIVITY_RANK else "sensitive"
                    for value in source_sensitivities
                ),
                key=_SENSITIVITY_RANK.__getitem__,
            )

            derived_specs = (
                (
                    reflection,
                    "reflective",
                    6,
                    reflection_valence,
                    reflection_arousal,
                    "digest_memories:reflection",
                    ["digestion", "reflection"],
                ),
                (
                    feel,
                    "emotional",
                    5,
                    feel_valence,
                    feel_arousal,
                    "digest_memories:feel",
                    ["digestion", "feel"],
                ),
            )
            for (
                content,
                memory_type,
                importance,
                valence,
                arousal,
                source_message,
                topics,
            ) in derived_specs:
                if not content:
                    continue
                derived_sensitivity = _sensitivity_with_floor(
                    declared=sensitivity,
                    content=content,
                )
                memory = MemoryRecord(
                    id=new_memory_id(),
                    user_id=user_id,
                    content=content,
                    type=memory_type,
                    importance=importance,
                    confidence=0.8,
                    valence=valence,
                    arousal=arousal,
                    source_message=source_message,
                    origin="agent_derived",
                    sensitivity=derived_sensitivity,
                    evidence_memory_ids=source_ids,
                    topics=topics,
                    created_at=now,
                    updated_at=now,
                    archived=0,
                )
                self._insert_memory_row(connection=connection, memory=memory)
                created.append(memory)

            source_placeholders = ", ".join("?" for _ in source_ids)
            digested_cursor = connection.execute(
                f"""
                UPDATE memories
                SET digested = 1, updated_at = ?
                WHERE user_id = ? AND archived = 0 AND COALESCE(digested, 0) = 0
                  AND id IN ({source_placeholders})
                """,
                (now, user_id, *source_ids),
            )
            if digested_cursor.rowcount != len(source_ids):
                raise RuntimeError("digest source set changed during submission")

            resolved_count = 0
            if resolved_ids:
                resolved_placeholders = ", ".join("?" for _ in resolved_ids)
                resolved_cursor = connection.execute(
                    f"""
                    UPDATE memories
                    SET status = 'resolved', updated_at = ?
                    WHERE user_id = ? AND archived = 0 AND id IN ({resolved_placeholders})
                    """,
                    (now, user_id, *resolved_ids),
                )
                resolved_count = int(resolved_cursor.rowcount)
                if resolved_count != len(resolved_ids):
                    raise RuntimeError("resolved source set changed during submission")
        return created, resolved_count

    @staticmethod
    def _validated_digest_source_rows(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        source_ids: list[str],
        include_sensitive: bool = False,
    ) -> list[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in source_ids)
        sensitivity_sql = "" if include_sensitive else "AND COALESCE(sensitivity, 'normal') = 'normal'"
        rows = connection.execute(
            f"""
            SELECT * FROM memories
            WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
              AND COALESCE(origin, 'user_asserted') = 'user_asserted'
              AND COALESCE(digested, 0) = 0
              {sensitivity_sql}
            """,
            (user_id, *source_ids),
        ).fetchall()
        if not include_sensitive:
            rows = [
                row
                for row in rows
                if _sensitivity_with_floor(
                    declared=str(row["sensitivity"] or "normal"),
                    content=str(row["content"] or ""),
                    source_message=str(row["source_message"] or "") or None,
                    entities=_json_string_list(row["entities_json"]),
                )
                == "normal"
            ]
        rows_by_id = {str(row["id"]): row for row in rows}
        if any(memory_id not in rows_by_id for memory_id in source_ids):
            raise ValueError("source_ids contain missing or inaccessible memories")
        return [rows_by_id[memory_id] for memory_id in source_ids]

    def mark_digested(self, *, memory_ids: list[str], user_id: str) -> None:
        """标记记忆为已消化。"""
        if not memory_ids:
            return
        placeholders = ", ".join("?" for _ in memory_ids)
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE memories
                SET digested = 1, updated_at = ?
                WHERE id IN ({placeholders}) AND user_id = ? AND archived = 0
                """,
                (now, *memory_ids, user_id),
            )

    def update_memory_statuses(
        self,
        *,
        memory_ids: list[str],
        user_id: str,
        status: str,
    ) -> int:
        """Update lifecycle status for active memories and return affected row count."""
        if status not in {"dynamic", "resolved", "archived", "pinned"}:
            raise ValueError("status must be dynamic, resolved, archived, or pinned")
        unique_ids = _ordered_unique(memory_ids)
        if not unique_ids:
            return 0
        placeholders = ", ".join("?" for _ in unique_ids)
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE memories
                SET status = ?, updated_at = ?
                WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
                """,
                (status, now, user_id, *unique_ids),
            )
        return int(cursor.rowcount)

    def _rebuild_temporal_key(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        temporal_subject: str | None,
        temporal_predicate: str | None,
    ) -> int:
        """Rebuild one active temporal chain from effective-time order.

        Import/restore operations may materialize a chain a row at a time, and a
        soft-deleted head can be absent while a newer fact is created.  Raw link
        existence alone therefore cannot establish a valid chain.  Rebuilding
        both directions from the user-scoped temporal key makes the operation
        deterministic and also removes links that now cross keys.
        """
        subject = normalize_optional_text(temporal_subject)
        predicate = normalize_optional_text(temporal_predicate)
        if subject is None or predicate is None:
            return 0

        rows = connection.execute(
            """
            SELECT * FROM memories
            WHERE user_id = ? AND archived = 0
              AND temporal_subject = ? AND temporal_predicate = ?
              AND COALESCE(status, 'dynamic') IN ('dynamic', 'resolved', 'pinned')
            """,
            (user_id, subject, predicate),
        ).fetchall()
        memories = [self._row_to_memory(row, space_ids=[]) for row in rows]
        memories.sort(
            key=lambda memory: (
                _parse_iso_datetime(memory.valid_from or memory.created_at)
                or datetime.max.replace(tzinfo=UTC),
                _parse_iso_datetime(memory.created_at)
                or datetime.max.replace(tzinfo=UTC),
                memory.id,
            )
        )
        if not memories:
            return 0

        by_id = {memory.id: memory for memory in memories}
        current_instant = datetime.now(UTC)
        updated_at = utc_now_iso()
        changed = 0
        for index, memory in enumerate(memories):
            predecessor = memories[index - 1] if index > 0 else None
            successor = memories[index + 1] if index + 1 < len(memories) else None

            explicit_end = memory.valid_until
            old_successor = by_id.get(memory.superseded_by or "")
            if old_successor is not None and explicit_end is not None:
                old_successor_start = old_successor.valid_from or old_successor.created_at
                if _parse_iso_datetime(explicit_end) == _parse_iso_datetime(
                    old_successor_start
                ):
                    # This boundary was generated by the previous chain.  Let
                    # the rebuilt successor choose it instead of treating it as
                    # an independently declared expiry.
                    explicit_end = None

            valid_until = explicit_end
            if successor is not None:
                successor_start = successor.valid_from or successor.created_at
                successor_instant = _parse_iso_datetime(successor_start)
                explicit_end_instant = _parse_iso_datetime(explicit_end)
                if (
                    explicit_end_instant is None
                    or successor_instant is not None
                    and successor_instant < explicit_end_instant
                ):
                    valid_until = successor_start

            if memory.status == "pinned":
                rebuilt_status = "pinned"
            else:
                ends_at = _parse_iso_datetime(valid_until)
                if ends_at is None and memory.status == "resolved":
                    # 链上推导的 resolved 必有有效期；没有有效期说明是
                    # 用户手动了结的，重建时保留，不静默复活。
                    rebuilt_status = "resolved"
                else:
                    rebuilt_status = (
                        "resolved"
                        if ends_at is not None and ends_at <= current_instant
                        else "dynamic"
                    )
            supersedes = predecessor.id if predecessor is not None else None
            superseded_by = successor.id if successor is not None else None
            if (
                memory.valid_until == valid_until
                and memory.status == rebuilt_status
                and memory.supersedes == supersedes
                and memory.superseded_by == superseded_by
            ):
                continue
            cursor = connection.execute(
                """
                UPDATE memories
                SET valid_until = ?, status = ?, supersedes = ?,
                    superseded_by = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (
                    valid_until,
                    rebuilt_status,
                    supersedes,
                    superseded_by,
                    updated_at,
                    memory.id,
                    user_id,
                ),
            )
            changed += max(0, int(cursor.rowcount))
        return changed

    def _rebuild_all_active_temporal_chains(
        self,
        *,
        connection: sqlite3.Connection,
    ) -> int:
        """Repair legacy active links that still point into the recycle bin."""
        keys = connection.execute(
            """
            SELECT DISTINCT user_id, temporal_subject, temporal_predicate
            FROM memories
            WHERE archived = 0
              AND temporal_subject IS NOT NULL
              AND temporal_predicate IS NOT NULL
            """
        ).fetchall()
        changed = 0
        for row in keys:
            user_id = str(row["user_id"] or "default")
            changed += self._rebuild_temporal_key(
                connection=connection,
                user_id=user_id,
                temporal_subject=row["temporal_subject"],
                temporal_predicate=row["temporal_predicate"],
            )
        return changed

    def _detach_temporal_position(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory: MemoryRecord,
    ) -> None:
        """Remove one row from its old version-chain position and bridge neighbors."""
        predecessor_id = memory.supersedes
        successor_id = memory.superseded_by
        predecessor = None
        successor = None
        if predecessor_id:
            predecessor = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (predecessor_id, user_id),
            ).fetchone()
        if predecessor is None:
            predecessor = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND archived = 0 AND superseded_by = ?
                ORDER BY COALESCE(valid_from, created_at) DESC, updated_at DESC
                LIMIT 1
                """,
                (user_id, memory.id),
            ).fetchone()
            predecessor_id = str(predecessor["id"]) if predecessor else None
        if successor_id:
            successor = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (successor_id, user_id),
            ).fetchone()
        if successor is None:
            successor = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND archived = 0 AND supersedes = ?
                ORDER BY COALESCE(valid_from, created_at) ASC, updated_at ASC
                LIMIT 1
                """,
                (user_id, memory.id),
            ).fetchone()
            successor_id = str(successor["id"]) if successor else None

        now = utc_now_iso()
        current_instant = _parse_iso_datetime(now)
        old_effective = _parse_iso_datetime(memory.valid_from or memory.created_at)
        successor_effective_text = (
            str(successor["valid_from"] or successor["created_at"])
            if successor is not None
            else None
        )
        if predecessor is not None:
            predecessor_end = predecessor["valid_until"]
            predecessor_end_instant = _parse_iso_datetime(predecessor_end)
            # Ends created by chain insertion equal this row's old start. Move
            # that boundary with the bridge; preserve an independently declared
            # earlier expiry.
            if (
                predecessor_end_instant is not None
                and old_effective is not None
                and predecessor_end_instant == old_effective
            ):
                predecessor_end = successor_effective_text
            predecessor_status = str(predecessor["status"] or "dynamic")
            if predecessor_status != "pinned":
                bridged_end = _parse_iso_datetime(predecessor_end)
                if (
                    bridged_end is None
                    and predecessor_status == "resolved"
                    and predecessor["valid_until"] is None
                ):
                    # 手动了结的 resolved 原本就没有有效期；桥接没有移除
                    # 任何边界，保留用户意图，不静默复活。
                    pass
                else:
                    predecessor_status = (
                        "resolved"
                        if bridged_end is not None
                        and current_instant is not None
                        and bridged_end <= current_instant
                        else "dynamic"
                    )
            connection.execute(
                """
                UPDATE memories
                SET valid_until = ?, status = ?, superseded_by = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (
                    predecessor_end,
                    predecessor_status,
                    successor_id,
                    now,
                    predecessor_id,
                    user_id,
                ),
            )
        if successor is not None:
            connection.execute(
                """
                UPDATE memories
                SET supersedes = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (predecessor_id, now, successor_id, user_id),
            )

    def _apply_temporal_invalidation(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        new_memory: MemoryRecord,
    ) -> list[str]:
        if not new_memory.temporal_subject or not new_memory.temporal_predicate:
            return []

        effective_at = new_memory.valid_from or new_memory.created_at
        effective_instant = _parse_iso_datetime(effective_at)
        if effective_instant is None:
            return []
        candidate_rows = connection.execute(
            """
            SELECT * FROM memories
            WHERE user_id = ?
              AND archived = 0
              AND id != ?
              AND temporal_subject = ?
              AND temporal_predicate = ?
              AND COALESCE(status, 'dynamic') IN ('dynamic', 'resolved', 'pinned')
            """,
            (
                user_id,
                new_memory.id,
                new_memory.temporal_subject,
                new_memory.temporal_predicate,
            ),
        ).fetchall()
        eligible_rows: list[tuple[datetime, datetime, str, sqlite3.Row]] = []
        successor_rows: list[tuple[datetime, datetime, str, sqlite3.Row]] = []
        for row in candidate_rows:
            starts_at = _parse_iso_datetime(row["valid_from"] or row["created_at"])
            if starts_at is None:
                continue
            updated_at = _parse_iso_datetime(row["updated_at"]) or starts_at
            if starts_at > effective_instant:
                successor_rows.append((starts_at, updated_at, str(row["id"]), row))
                continue
            valid_until = row["valid_until"]
            if valid_until:
                ends_at = _parse_iso_datetime(valid_until)
                if ends_at is None or ends_at < effective_instant:
                    continue
            eligible_rows.append((starts_at, updated_at, str(row["id"]), row))
        eligible_rows.sort(key=lambda item: item[:3], reverse=True)
        successor_rows.sort(key=lambda item: item[:3])
        rows = [item[3] for item in eligible_rows]
        successor = successor_rows[0] if successor_rows else None
        if not rows and successor is None:
            return []

        superseded_ids = [str(row["id"]) for row in rows]
        now = utc_now_iso()
        current_instant = datetime.now(UTC)
        is_effective_now = effective_instant <= current_instant
        primary_superseded_id = superseded_ids[0] if superseded_ids else None
        if superseded_ids:
            placeholders = ", ".join("?" for _ in superseded_ids)
            connection.execute(
                f"""
                UPDATE memories
                SET valid_until = ?,
                    status = CASE
                        WHEN status = 'pinned' THEN 'pinned'
                        WHEN ? THEN 'resolved'
                        ELSE COALESCE(status, 'dynamic')
                    END,
                    superseded_by = ?,
                    updated_at = ?
                WHERE user_id = ?
                  AND archived = 0
                  AND id IN ({placeholders})
                """,
                (
                    effective_at,
                    int(is_effective_now),
                    new_memory.id,
                    now,
                    user_id,
                    *superseded_ids,
                ),
            )

        successor_id: str | None = None
        successor_effective_at: str | None = None
        new_valid_until = new_memory.valid_until
        new_status = new_memory.status
        if successor is not None:
            successor_starts_at, _, successor_id_value, successor_row = successor
            declared_end = _parse_iso_datetime(new_memory.valid_until)
            if declared_end is None or successor_starts_at < declared_end:
                successor_id = successor_id_value
                successor_effective_at = str(
                    successor_row["valid_from"] or successor_row["created_at"]
                )
                new_valid_until = successor_effective_at
                if successor_starts_at <= current_instant and new_status != "pinned":
                    new_status = "resolved"

        connection.execute(
            """
            UPDATE memories
            SET valid_until = ?,
                status = ?,
                supersedes = ?,
                superseded_by = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (
                new_valid_until,
                new_status,
                primary_superseded_id,
                successor_id,
                now,
                new_memory.id,
                user_id,
            ),
        )
        new_memory.supersedes = primary_superseded_id
        new_memory.superseded_by = successor_id
        new_memory.valid_until = new_valid_until
        new_memory.status = new_status
        new_memory.updated_at = now

        if successor_id is not None:
            connection.execute(
                """
                UPDATE memories
                SET supersedes = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (new_memory.id, now, successor_id, user_id),
            )

        self._insert_decision_log(
            connection=connection,
            user_id=user_id,
            conversation_id=None,
            candidate_json=json.dumps(
                {
                    "source": "temporal_invalidation",
                    "temporal_subject": new_memory.temporal_subject,
                    "temporal_predicate": new_memory.temporal_predicate,
                    "effective_at": effective_at,
                    "new_memory_id": new_memory.id,
                    "superseded_memory_ids": superseded_ids,
                    "primary_superseded_id": primary_superseded_id,
                    "successor_memory_id": successor_id,
                    "before": [self._temporal_snapshot(row) for row in rows],
                    "after": [
                        {
                            "id": str(row["id"]),
                            "valid_until": effective_at,
                            "status": (
                                "pinned"
                                if str(row["status"] or "dynamic") == "pinned"
                                else "resolved"
                                if is_effective_now
                                else str(row["status"] or "dynamic")
                            ),
                            "superseded_by": new_memory.id,
                        }
                        for row in rows
                    ],
                    "new_interval_after": {
                        "id": new_memory.id,
                        "valid_from": effective_at,
                        "valid_until": new_valid_until,
                        "status": new_status,
                        "supersedes": primary_superseded_id,
                        "superseded_by": successor_id,
                    },
                    "successor_effective_at": successor_effective_at,
                },
                ensure_ascii=False,
            ),
            decision="update",
            reason="Closed older temporal facts with the same subject and predicate",
        )
        return superseded_ids

    @staticmethod
    def _temporal_snapshot(row: sqlite3.Row) -> dict:
        columns = set(row.keys())
        return {
            "id": row["id"],
            "valid_from": row["valid_from"] if "valid_from" in columns else None,
            "valid_until": row["valid_until"] if "valid_until" in columns else None,
            "temporal_subject": row["temporal_subject"] if "temporal_subject" in columns else None,
            "temporal_predicate": row["temporal_predicate"] if "temporal_predicate" in columns else None,
            "status": row["status"] if "status" in columns else None,
            "supersedes": row["supersedes"] if "supersedes" in columns else None,
            "superseded_by": row["superseded_by"] if "superseded_by" in columns else None,
            "updated_at": row["updated_at"],
        }

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
        log = DecisionLog(
            id=new_memory_id(),
            user_id=user_id,
            conversation_id=conversation_id,
            candidate_json=candidate_json,
            decision=decision,
            reason=reason,
            created_at=utc_now_iso(),
        )
        connection.execute(
            """
            INSERT INTO memory_decision_logs (
                id, user_id, conversation_id, candidate_json, decision, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log.id,
                log.user_id,
                log.conversation_id,
                log.candidate_json,
                log.decision,
                log.reason,
                log.created_at,
            ),
        )
        # 每用户只保留最近 _DECISION_LOG_RETENTION_LIMIT 条，防止日志表无界增长。
        connection.execute(
            """
            DELETE FROM memory_decision_logs
            WHERE user_id = ?
              AND id NOT IN (
                  SELECT id FROM memory_decision_logs
                  WHERE user_id = ?
                  ORDER BY created_at DESC, rowid DESC
                  LIMIT ?
              )
            """,
            (user_id, user_id, _DECISION_LOG_RETENTION_LIMIT),
        )
        return log

    def create_decision_log(
        self,
        *,
        user_id: str = "default",
        conversation_id: str | None,
        candidate_json: str,
        decision: DecisionLogAction,
        reason: str,
    ) -> DecisionLog:
        with self._connect() as connection:
            return self._insert_decision_log(
                connection=connection,
                user_id=user_id,
                conversation_id=conversation_id,
                candidate_json=candidate_json,
                decision=decision,
                reason=reason,
            )

    def list_decision_logs(
        self,
        *,
        user_id: str | None = None,
        conversation_id: str | None = None,
        memory_id: str | None = None,
        limit: int | None = 100,
    ) -> list[DecisionLog]:
        query = "SELECT * FROM memory_decision_logs"
        params: list[object] = []
        conditions: list[str] = []
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if conversation_id:
            conditions.append("conversation_id = ?")
            params.append(conversation_id)
        if memory_id:
            conditions.append("memory_log_references(candidate_json) = 1")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._connect() as connection:
            if memory_id:
                references = {memory_id}
                connection.create_function(
                    "memory_log_references",
                    1,
                    lambda raw_json: int(
                        _decision_log_references_memory_ids(raw_json, references)
                    ),
                    deterministic=True,
                )
            rows = connection.execute(query, params).fetchall()
        return [DecisionLog(**dict(row)) for row in rows]

    def _create_core_memory_section_history(
        self,
        *,
        connection: sqlite3.Connection | None,
        section: CoreMemorySection,
        replaced_at: str,
    ) -> None:
        evidence_json = json.dumps(section.evidence_memory_ids, ensure_ascii=False)
        params = (
            new_memory_id(),
            section.id,
            section.user_id,
            section.section,
            section.content,
            evidence_json,
            section.confidence,
            section.version,
            section.created_at,
            section.updated_at,
            replaced_at,
            section.revision,
        )
        query = """
            INSERT INTO core_memory_section_history (
                id, core_memory_section_id, user_id, section, content,
                evidence_memory_ids_json, confidence, version,
                created_at, updated_at, replaced_at, revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if connection is not None:
            connection.execute(query, params)
            return
        with self._connect() as owned_connection:
            owned_connection.execute(query, params)

    def _insert_memory_row(
        self,
        *,
        connection: sqlite3.Connection,
        memory: MemoryRecord,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memories (
                id, user_id, content, type, importance, confidence,
                valence, arousal,
                source_message, source_conversation_id, origin, embedding_json,
                embedding_space_id,
                last_used_at, usage_count, stability, valid_from, valid_until, review_after,
                sensitivity, evidence_memory_ids_json, topics_json, entities_json,
                temporal_subject, temporal_predicate,
                status, digested, decay_lambda, supersedes, superseded_by,
                created_at, updated_at, archived_at, archived, revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.id,
                memory.user_id,
                memory.content,
                memory.type,
                memory.importance,
                memory.confidence,
                memory.valence,
                memory.arousal,
                memory.source_message,
                memory.source_conversation_id,
                memory.origin,
                memory.embedding_json,
                memory.embedding_space_id,
                memory.last_used_at,
                memory.usage_count,
                memory.stability,
                memory.valid_from,
                memory.valid_until,
                memory.review_after,
                memory.sensitivity,
                json.dumps(memory.evidence_memory_ids, ensure_ascii=False),
                json.dumps(memory.topics, ensure_ascii=False),
                json.dumps(memory.entities, ensure_ascii=False),
                memory.temporal_subject,
                memory.temporal_predicate,
                memory.status,
                int(memory.digested),
                memory.decay_lambda,
                memory.supersedes,
                memory.superseded_by,
                memory.created_at,
                memory.updated_at,
                memory.archived_at,
                memory.archived,
                memory.revision,
            ),
        )

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
        connection.execute(
            """
            UPDATE recent_context_summaries AS older
            SET archived = 1
            WHERE archived = 0
              AND EXISTS (
                SELECT 1
                FROM recent_context_summaries AS newer
                WHERE newer.user_id = older.user_id
                  AND newer.archived = 0
                  AND (
                    newer.conversation_id = older.conversation_id
                    OR (newer.conversation_id IS NULL AND older.conversation_id IS NULL)
                  )
                  AND (
                    newer.updated_at > older.updated_at
                    OR (
                      newer.updated_at = older.updated_at
                      AND newer.created_at > older.created_at
                    )
                    OR (
                      newer.updated_at = older.updated_at
                      AND newer.created_at = older.created_at
                      AND newer.rowid > older.rowid
                    )
                  )
              )
            """
        )

    @staticmethod
    def _ensure_memories_usage_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "last_used_at" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN last_used_at TEXT")
        if "usage_count" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN usage_count REAL DEFAULT 0.0")
        if "valence" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN valence REAL DEFAULT 0.5")
        if "arousal" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN arousal REAL DEFAULT 0.3")
        if "stability" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN stability TEXT DEFAULT 'stable'")
        if "valid_from" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN valid_from TEXT")
        if "valid_until" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN valid_until TEXT")
        if "review_after" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN review_after TEXT")
        if "sensitivity" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN sensitivity TEXT DEFAULT 'normal'")
        if "origin" not in columns:
            connection.execute(
                "ALTER TABLE memories ADD COLUMN origin TEXT DEFAULT 'user_asserted'"
            )
        if "evidence_memory_ids_json" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN evidence_memory_ids_json TEXT")
        if "topics_json" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN topics_json TEXT")
        if "entities_json" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN entities_json TEXT")
        if "archived_at" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN archived_at TEXT")
        if "temporal_subject" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN temporal_subject TEXT")
        if "temporal_predicate" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN temporal_predicate TEXT")
        if "status" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'dynamic'")
        if "digested" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN digested INTEGER DEFAULT 0")
        if "decay_lambda" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN decay_lambda REAL")
        if "supersedes" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN supersedes TEXT")
        if "superseded_by" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT")
        connection.execute(
            """
            UPDATE memories
            SET origin = 'agent_derived'
            WHERE source_message IN (
                'digest_memories:reflection',
                'digest_memories:feel'
            )
            """
        )

        connection.execute(
            """
            UPDATE memories
            SET status = 'dynamic'
            WHERE status IS NULL OR status = ''
            """
        )
        connection.execute(
            """
            UPDATE memories
            SET origin = 'user_asserted'
            WHERE origin IS NULL OR origin = ''
            """
        )
        connection.execute(
            """
            UPDATE memories
            SET type = 'semantic'
            WHERE type IS NULL
               OR type = ''
               OR type IN (
                   'project', 'preference', 'fact', 'learning',
                   'style', 'person', 'relationship'
               )
               OR type NOT IN (
                   'episodic', 'semantic', 'procedural',
                   'emotional', 'reflective'
               )
            """
        )

    @staticmethod
    def _ensure_memories_embedding_space_column(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "embedding_space_id" not in columns:
            # Existing vectors intentionally remain unidentified. Inferring a
            # space from the current model or dimensions could mix vectors
            # generated by a previous provider/model configuration.
            connection.execute(
                "ALTER TABLE memories ADD COLUMN embedding_space_id TEXT"
            )

    @staticmethod
    def _ensure_core_memory_sections_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(core_memory_sections)").fetchall()
        }
        if "version" not in columns:
            connection.execute(
                "ALTER TABLE core_memory_sections ADD COLUMN version INTEGER DEFAULT 1"
            )

    @staticmethod
    def _ensure_revision_columns(connection: sqlite3.Connection) -> None:
        for table_name in (
            "memories",
            "core_memory_sections",
            "core_memory_section_history",
        ):
            columns = {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            if not columns:
                continue
            if "revision" not in columns:
                connection.execute(
                    f"ALTER TABLE {table_name} "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                )
            connection.execute(
                f"UPDATE {table_name} SET revision = 1 "
                "WHERE revision IS NULL OR revision < 1"
            )

    @staticmethod
    def _merge_duplicate_active_core_sections(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(core_memory_sections)"
            ).fetchall()
        }
        required = {
            "id",
            "user_id",
            "section",
            "content",
            "evidence_memory_ids_json",
            "confidence",
            "version",
            "created_at",
            "updated_at",
            "archived",
            "revision",
        }
        if not required.issubset(columns):
            return
        connection.execute(
            """
            UPDATE core_memory_sections
            SET user_id = 'default'
            WHERE user_id IS NULL OR TRIM(user_id) = ''
            """
        )
        history_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(core_memory_section_history)"
            ).fetchall()
        }
        can_write_history = {
            "id",
            "core_memory_section_id",
            "user_id",
            "section",
            "content",
            "evidence_memory_ids_json",
            "confidence",
            "version",
            "created_at",
            "updated_at",
            "replaced_at",
            "revision",
        }.issubset(history_columns)
        groups = connection.execute(
            """
            SELECT user_id, section
            FROM core_memory_sections
            WHERE archived = 0 AND section IS NOT NULL
            GROUP BY user_id, section
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for group in groups:
            rows = connection.execute(
                """
                SELECT rowid AS merge_rowid, *
                FROM core_memory_sections
                WHERE user_id = ? AND section = ? AND archived = 0
                ORDER BY version DESC, updated_at DESC, merge_rowid DESC
                """,
                (group["user_id"], group["section"]),
            ).fetchall()
            if len(rows) < 2:
                continue
            winner, *duplicates = rows
            merged_evidence = _ordered_unique(
                [
                    evidence_id
                    for row in rows
                    for evidence_id in _json_string_list(
                        row["evidence_memory_ids_json"]
                    )
                ]
            )
            merged_evidence_json = json.dumps(merged_evidence, ensure_ascii=False)
            now = utc_now_iso()

            def write_history(row: sqlite3.Row) -> None:
                if not can_write_history:
                    return
                connection.execute(
                    """
                    INSERT INTO core_memory_section_history (
                        id, core_memory_section_id, user_id, section, content,
                        evidence_memory_ids_json, confidence, version,
                        created_at, updated_at, replaced_at, revision
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_memory_id(),
                        row["id"],
                        row["user_id"],
                        row["section"],
                        row["content"],
                        row["evidence_memory_ids_json"],
                        row["confidence"],
                        row["version"],
                        row["created_at"],
                        row["updated_at"],
                        now,
                        row["revision"],
                    ),
                )

            for duplicate in duplicates:
                write_history(duplicate)
                connection.execute(
                    """
                    UPDATE core_memory_sections
                    SET archived = 1, updated_at = ?, revision = revision + 1
                    WHERE id = ? AND archived = 0
                    """,
                    (now, duplicate["id"]),
                )
            if merged_evidence_json != str(winner["evidence_memory_ids_json"] or "[]"):
                write_history(winner)
                max_version = max(int(row["version"] or 1) for row in rows)
                max_revision = max(int(row["revision"] or 1) for row in rows)
                connection.execute(
                    """
                    UPDATE core_memory_sections
                    SET evidence_memory_ids_json = ?, version = ?, revision = ?,
                        updated_at = ?
                    WHERE id = ? AND archived = 0
                    """,
                    (
                        merged_evidence_json,
                        max_version + 1,
                        max_revision + 1,
                        now,
                        winner["id"],
                    ),
                )

    @staticmethod
    def _ensure_recent_context_summary_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(recent_context_summaries)"
            ).fetchall()
        }
        if "compressed_summary" not in columns:
            connection.execute(
                "ALTER TABLE recent_context_summaries "
                "ADD COLUMN compressed_summary TEXT DEFAULT ''"
            )
        if "recent_turns_json" not in columns:
            connection.execute(
                "ALTER TABLE recent_context_summaries "
                "ADD COLUMN recent_turns_json TEXT DEFAULT '[]'"
            )
        if "turn_count" not in columns:
            connection.execute(
                "ALTER TABLE recent_context_summaries "
                "ADD COLUMN turn_count INTEGER DEFAULT 0"
            )

    @staticmethod
    def _ensure_decision_logs_user_id(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(memory_decision_logs)").fetchall()
        }
        if "user_id" not in columns:
            connection.execute(
                "ALTER TABLE memory_decision_logs ADD COLUMN user_id TEXT DEFAULT 'default'"
            )

    def _space_ids_for_memory_ids(
        self,
        *,
        user_id: str,
        memory_ids: list[str],
    ) -> dict[str, list[str]]:
        with self._connect() as connection:
            return self._space_ids_for_memory_ids_on_connection(
                connection=connection,
                user_id=user_id,
                memory_ids=memory_ids,
            )

    @staticmethod
    def _space_ids_for_memory_ids_on_connection(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory_ids: list[str],
    ) -> dict[str, list[str]]:
        unique_ids = _ordered_unique(memory_ids)
        if not unique_ids:
            return {}
        result = {memory_id: [] for memory_id in unique_ids}
        for offset in range(0, len(unique_ids), 500):
            batch = unique_ids[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT memory_id, space_id
                FROM memory_space_links
                WHERE user_id = ? AND memory_id IN ({placeholders})
                ORDER BY created_at ASC, rowid ASC
                """,
                (user_id, *batch),
            ).fetchall()
            for row in rows:
                result.setdefault(str(row["memory_id"]), []).append(
                    str(row["space_id"])
                )
        return result

    @staticmethod
    def _replace_memory_space_links(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        memory_id: str,
        space_ids: list[str],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            DELETE FROM memory_space_links
            WHERE user_id = ? AND memory_id = ?
            """,
            (user_id, memory_id),
        )
        for space_id in _ordered_unique(space_ids):
            connection.execute(
                """
                INSERT OR IGNORE INTO memory_space_links (
                    user_id, memory_id, space_id, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (user_id, memory_id, space_id, created_at),
            )

    @staticmethod
    def _filter_existing_space_ids(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        space_ids: list[str],
    ) -> list[str]:
        unique_ids = _ordered_unique(space_ids)
        if not unique_ids:
            return []
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = connection.execute(
            f"""
            SELECT id FROM memory_spaces
            WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
            """,
            (user_id, *unique_ids),
        ).fetchall()
        existing = {str(row["id"]) for row in rows}
        return [space_id for space_id in unique_ids if space_id in existing]

    @staticmethod
    def _validate_space_ids(
        *,
        connection: sqlite3.Connection,
        user_id: str,
        space_ids: list[str],
    ) -> None:
        unique_ids = _ordered_unique(space_ids)
        if not unique_ids:
            return
        existing = set(
            MemoryStore._filter_existing_space_ids(
                connection=connection,
                user_id=user_id,
                space_ids=unique_ids,
            )
        )
        missing = [space_id for space_id in unique_ids if space_id not in existing]
        if missing:
            raise ValueError(f"空间不存在或不属于当前用户：{', '.join(missing)}")

    def _rows_to_memories(self, rows: list[sqlite3.Row]) -> list[MemoryRecord]:
        if not rows:
            return []
        with self._connect() as connection:
            return self._rows_to_memories_on_connection(
                connection=connection,
                rows=rows,
            )

    def _rows_to_memories_on_connection(
        self,
        *,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> list[MemoryRecord]:
        if not rows:
            return []
        space_ids_by_memory = self._space_ids_for_memory_ids_on_connection(
            connection=connection,
            user_id=str(rows[0]["user_id"]),
            memory_ids=[str(row["id"]) for row in rows],
        )
        return [
            self._row_to_memory(row, space_ids=space_ids_by_memory.get(str(row["id"]), []))
            for row in rows
        ]

    def _row_to_memory(
        self,
        row: sqlite3.Row,
        *,
        space_ids: list[str] | None = None,
    ) -> MemoryRecord:
        data = dict(row)
        raw_evidence = data.pop("evidence_memory_ids_json", None)
        raw_topics = data.pop("topics_json", None)
        raw_entities = data.pop("entities_json", None)
        data["evidence_memory_ids"] = _json_string_list(raw_evidence)
        data["topics"] = _json_string_list(raw_topics)
        data["entities"] = _json_string_list(raw_entities)
        data["type"] = normalize_memory_type(data.get("type") or "semantic")
        data["origin"] = data.get("origin") or "user_asserted"
        data.setdefault("embedding_space_id", None)
        if not data.get("embedding_json"):
            data["embedding_space_id"] = None
        data["usage_count"] = float(data.get("usage_count") or 0)
        data["digested"] = bool(data.get("digested"))
        data["temporal_subject"] = normalize_optional_text(data.get("temporal_subject"))
        data["temporal_predicate"] = normalize_optional_text(data.get("temporal_predicate"))
        if bool(data["temporal_subject"]) != bool(data["temporal_predicate"]):
            # Pre-validation databases could contain a half-key. Treat it as
            # unkeyed instead of letting one corrupt row break all recall.
            data["temporal_subject"] = None
            data["temporal_predicate"] = None
        data.setdefault("valid_from", None)
        data.setdefault("status", "dynamic")
        data.setdefault("decay_lambda", None)
        for field_name in ("valid_from", "valid_until"):
            try:
                data[field_name] = normalize_iso_text(data.get(field_name))
            except ValueError:
                data[field_name] = None
        starts_at = _parse_iso_datetime(data.get("valid_from"))
        ends_at = _parse_iso_datetime(data.get("valid_until"))
        if starts_at is not None and ends_at is not None and starts_at > ends_at:
            # Preserve the expiry (the conservative current-view boundary) and
            # discard the impossible start on legacy corrupt data.
            data["valid_from"] = None
        try:
            decay_lambda = float(data["decay_lambda"])
        except (TypeError, ValueError):
            decay_lambda = None
        if (
            decay_lambda is None
            or not math.isfinite(decay_lambda)
            or not 0.0 <= decay_lambda <= 10.0
        ):
            decay_lambda = None
        data["decay_lambda"] = decay_lambda
        data.setdefault("supersedes", None)
        data.setdefault("superseded_by", None)
        data["space_ids"] = (
            space_ids
            if space_ids is not None
            else self._space_ids_for_memory_ids(
                user_id=str(data["user_id"]),
                memory_ids=[str(data["id"])],
            ).get(str(data["id"]), [])
        )
        return MemoryRecord(**data)

    @staticmethod
    def _row_to_memory_space(row: sqlite3.Row) -> MemorySpace:
        return MemorySpace(**dict(row))

    @staticmethod
    def _row_to_core_memory_section(row: sqlite3.Row) -> CoreMemorySection:
        data = dict(row)
        raw_evidence = data.pop("evidence_memory_ids_json", None)
        data["evidence_memory_ids"] = _json_string_list(raw_evidence)
        return CoreMemorySection(**data)

    @staticmethod
    def _row_to_core_memory_section_history(row: sqlite3.Row) -> CoreMemorySectionHistory:
        data = dict(row)
        raw_evidence = data.pop("evidence_memory_ids_json", None)
        data["evidence_memory_ids"] = _json_string_list(raw_evidence)
        return CoreMemorySectionHistory(**data)

    @staticmethod
    def _row_to_recent_context_summary(row: sqlite3.Row) -> RecentContextSummary:
        data = dict(row)
        raw_turns = data.pop("recent_turns_json", None)
        try:
            parsed_turns = json.loads(raw_turns) if raw_turns else []
        except json.JSONDecodeError:
            parsed_turns = []
        data["recent_turns"] = parsed_turns if isinstance(parsed_turns, list) else []
        return RecentContextSummary(**data)

    @staticmethod
    def _row_to_conversation_branch_node(
        row: sqlite3.Row,
    ) -> ConversationBranchNode:
        data = dict(row)
        raw_turns = data.pop("recent_turns_json", None)
        try:
            parsed_turns = json.loads(raw_turns) if raw_turns else []
        except json.JSONDecodeError:
            parsed_turns = []
        data["recent_turns"] = parsed_turns if isinstance(parsed_turns, list) else []
        return ConversationBranchNode(**data)


def _json_string_list(raw_value: str | None) -> list[str]:
    try:
        values = json.loads(raw_value) if raw_value else []
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if value]


def _build_batch_purge_snapshot(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    requested_memory_ids: list[str],
) -> _BatchPurgeSnapshot:
    requested_ids = sorted(_ordered_unique(requested_memory_ids))
    if not requested_ids:
        raise PurgePreviewConflictError(
            code="purge_targets_missing",
            message="至少选择一条回收站记忆。",
        )

    all_memory_rows = connection.execute(
        "SELECT * FROM memories WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    rows_by_id = {str(row["id"]): row for row in all_memory_rows}
    missing_ids = [
        memory_id
        for memory_id in requested_ids
        if memory_id not in rows_by_id or int(rows_by_id[memory_id]["archived"] or 0) != 1
    ]
    if missing_ids:
        raise PurgePreviewConflictError(
            code="purge_targets_missing",
            message="部分所选记忆已不存在、不属于当前用户或已离开回收站。",
            missing_memory_ids=missing_ids,
        )

    dependents_by_evidence_id: dict[str, list[str]] = {}
    for row in all_memory_rows:
        memory_id = str(row["id"])
        for evidence_id in set(_json_string_list(row["evidence_memory_ids_json"])):
            dependents_by_evidence_id.setdefault(evidence_id, []).append(memory_id)

    affected_ids = set(requested_ids)
    frontier = deque(requested_ids)
    while frontier:
        evidence_id = frontier.popleft()
        for dependent_id in dependents_by_evidence_id.get(evidence_id, []):
            if dependent_id in affected_ids:
                continue
            affected_ids.add(dependent_id)
            frontier.append(dependent_id)

    purge_ids = sorted(affected_ids)
    dependent_ids = sorted(affected_ids.difference(requested_ids))
    affected_memory_rows = [rows_by_id[memory_id] for memory_id in purge_ids]
    affected_core_rows = [
        row
        for row in connection.execute(
            "SELECT * FROM core_memory_sections WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        if affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"]))
    ]
    affected_core_rows.sort(key=lambda row: (str(row["section"]), str(row["id"])))
    affected_core_history_rows = [
        row
        for row in connection.execute(
            "SELECT * FROM core_memory_section_history WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        if affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"]))
    ]
    affected_core_history_rows.sort(key=lambda row: str(row["id"]))

    affected_conversation_ids = {
        str(row["source_conversation_id"])
        for row in affected_memory_rows
        if row["source_conversation_id"]
    }
    sensitive_fragments = {
        fragment
        for row in affected_memory_rows
        for fragment in (
            str(row["id"]),
            str(row["content"] or ""),
            str(row["source_message"] or ""),
        )
        if fragment
    }
    affected_decision_log_rows = _purge_relevant_decision_log_rows(
        connection,
        user_id=user_id,
        affected_ids=affected_ids,
        affected_conversation_ids=affected_conversation_ids,
        sensitive_fragments=sensitive_fragments,
    )
    temporal_repairs = _purged_temporal_reference_plan(
        all_memory_rows,
        affected_ids=affected_ids,
    )
    affected_space_link_rows = [
        row
        for row in connection.execute(
            """
            SELECT memory_id, space_id, created_at
            FROM memory_space_links
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
        if str(row["memory_id"]) in affected_ids
    ]
    affected_space_link_rows.sort(
        key=lambda row: (str(row["memory_id"]), str(row["space_id"]))
    )

    effects = {
        "requested_memories_deleted": len(requested_ids),
        "dependent_memories_deleted": len(dependent_ids),
        "memories_deleted": len(purge_ids),
        "space_links_deleted": len(affected_space_link_rows),
        "temporal_references_relinked": len(temporal_repairs),
        "core_sections_scrubbed": len(affected_core_rows),
        "core_history_scrubbed": len(affected_core_history_rows),
        "decision_logs_scrubbed": len(affected_decision_log_rows),
    }
    affected_core_sections = [
        {
            "id": str(row["id"]),
            "section": str(row["section"]),
            "version": int(row["version"] or 1),
            "active": int(row["archived"] or 0) == 0,
        }
        for row in affected_core_rows
    ]
    fingerprint_payload = {
        "version": 1,
        "user_id": user_id,
        "requested_memory_ids": requested_ids,
        "memories": [_purge_memory_fingerprint_row(row) for row in affected_memory_rows],
        "space_links": [
            [str(row["memory_id"]), str(row["space_id"]), str(row["created_at"] or "")]
            for row in affected_space_link_rows
        ],
        "core_sections": [_purge_core_fingerprint_row(row) for row in affected_core_rows],
        "core_history": [
            _purge_core_history_fingerprint_row(row)
            for row in affected_core_history_rows
        ],
        "decision_logs": [
            _purge_decision_log_fingerprint_row(row)
            for row in affected_decision_log_rows
        ],
        "temporal_repairs": temporal_repairs,
        "effects": effects,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return _BatchPurgeSnapshot(
        requested_memory_ids=requested_ids,
        purge_memory_ids=purge_ids,
        dependent_memory_ids=dependent_ids,
        affected_core_memory_sections=affected_core_sections,
        fingerprint=fingerprint,
        effects=effects,
        affected_memory_rows=affected_memory_rows,
        affected_core_rows=affected_core_rows,
        affected_core_history_rows=affected_core_history_rows,
        affected_decision_log_rows=affected_decision_log_rows,
        temporal_repairs=temporal_repairs,
    )


def _purge_relevant_decision_log_rows(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    affected_ids: set[str],
    affected_conversation_ids: set[str],
    sensitive_fragments: set[str],
) -> list[sqlite3.Row]:
    prefilter = _decision_log_scrub_prefilter(
        affected_conversation_ids=affected_conversation_ids,
        fragments=sensitive_fragments,
    )
    if prefilter is None:
        rows = connection.execute(
            """
            SELECT id, conversation_id, candidate_json, reason, decision, created_at
            FROM memory_decision_logs
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            f"""
            SELECT id, conversation_id, candidate_json, reason, decision, created_at
            FROM memory_decision_logs
            WHERE user_id = ? AND ({prefilter[0]})
            """,
            (user_id, *prefilter[1]),
        ).fetchall()
    relevant = [
        row
        for row in rows
        if (
            _decision_log_references_memory_ids(str(row["candidate_json"] or ""), affected_ids)
            or (
                row["conversation_id"]
                and str(row["conversation_id"]) in affected_conversation_ids
            )
            or _decision_log_contains_fragments(
                candidate_json=str(row["candidate_json"] or ""),
                reason=str(row["reason"] or ""),
                fragments=sensitive_fragments,
            )
        )
    ]
    relevant.sort(key=lambda row: str(row["id"]))
    return relevant


def _purged_temporal_reference_plan(
    rows: list[sqlite3.Row],
    *,
    affected_ids: set[str],
) -> list[tuple[str, str | None, str | None]]:
    links = {
        str(row["id"]): (
            str(row["supersedes"]) if row["supersedes"] else None,
            str(row["superseded_by"]) if row["superseded_by"] else None,
        )
        for row in rows
    }

    def follow(start_id: str | None, *, index: int, survivor_id: str) -> str | None:
        current = start_id
        visited: set[str] = set()
        while current in affected_ids:
            if current in visited:
                return None
            visited.add(current)
            current_links = links.get(current)
            if current_links is None:
                return None
            current = current_links[index]
        if current is None or current == survivor_id or current not in links:
            return None
        return current

    repairs: list[tuple[str, str | None, str | None]] = []
    for row in rows:
        memory_id = str(row["id"])
        if memory_id in affected_ids:
            continue
        previous_id = str(row["supersedes"]) if row["supersedes"] else None
        next_id = str(row["superseded_by"]) if row["superseded_by"] else None
        repaired_previous = (
            follow(previous_id, index=0, survivor_id=memory_id)
            if previous_id in affected_ids
            else previous_id
        )
        repaired_next = (
            follow(next_id, index=1, survivor_id=memory_id)
            if next_id in affected_ids
            else next_id
        )
        if repaired_previous != previous_id or repaired_next != next_id:
            repairs.append((memory_id, repaired_previous, repaired_next))
    repairs.sort(key=lambda item: item[0])
    return repairs


def _purge_memory_fingerprint_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "archived": int(row["archived"] or 0),
        "archived_at": row["archived_at"],
        "revision": int(row["revision"] or 1),
        "updated_at": row["updated_at"],
        "origin": row["origin"],
        "type": row["type"],
        "sensitivity": row["sensitivity"],
        "evidence_memory_ids": sorted(_json_string_list(row["evidence_memory_ids_json"])),
        "source_conversation_id": row["source_conversation_id"],
        "supersedes": row["supersedes"],
        "superseded_by": row["superseded_by"],
        "content_sha256": _purge_text_sha256(row["content"]),
        "source_message_sha256": _purge_text_sha256(row["source_message"]),
    }


def _purge_core_fingerprint_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "section": str(row["section"]),
        "version": int(row["version"] or 1),
        "revision": int(row["revision"] or 1),
        "archived": int(row["archived"] or 0),
        "updated_at": row["updated_at"],
        "evidence_memory_ids": sorted(_json_string_list(row["evidence_memory_ids_json"])),
        "content_sha256": _purge_text_sha256(row["content"]),
    }


def _purge_core_history_fingerprint_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "core_memory_section_id": row["core_memory_section_id"],
        "section": row["section"],
        "version": row["version"],
        "updated_at": row["updated_at"],
        "replaced_at": row["replaced_at"],
        "evidence_memory_ids": sorted(_json_string_list(row["evidence_memory_ids_json"])),
        "content_sha256": _purge_text_sha256(row["content"]),
    }


def _purge_decision_log_fingerprint_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "conversation_id": row["conversation_id"],
        "decision": row["decision"],
        "created_at": row["created_at"],
        "candidate_sha256": _purge_text_sha256(row["candidate_json"]),
        "reason_sha256": _purge_text_sha256(row["reason"]),
    }


def _purge_text_sha256(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _apply_batch_purge_snapshot(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    snapshot: _BatchPurgeSnapshot,
    purged_at: str,
) -> dict[str, int]:
    temporal_references_relinked = 0
    for memory_id, supersedes, superseded_by in snapshot.temporal_repairs:
        cursor = connection.execute(
            """
            UPDATE memories
            SET supersedes = ?, superseded_by = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (supersedes, superseded_by, purged_at, memory_id, user_id),
        )
        temporal_references_relinked += max(0, int(cursor.rowcount))

    core_sections_scrubbed = 0
    for row in snapshot.affected_core_rows:
        cursor = connection.execute(
            """
            UPDATE core_memory_sections
            SET content = ?, evidence_memory_ids_json = '[]', archived = 1, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            ("[redacted: purged evidence]", purged_at, row["id"], user_id),
        )
        core_sections_scrubbed += max(0, int(cursor.rowcount))

    core_history_scrubbed = 0
    for row in snapshot.affected_core_history_rows:
        cursor = connection.execute(
            """
            UPDATE core_memory_section_history
            SET content = ?, evidence_memory_ids_json = '[]', updated_at = ?, replaced_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                "[redacted: purged evidence]",
                purged_at,
                purged_at,
                row["id"],
                user_id,
            ),
        )
        core_history_scrubbed += max(0, int(cursor.rowcount))

    replacement = json.dumps(
        {
            "source": "purged_memory_history",
            "memory_ids": snapshot.requested_memory_ids,
            "redacted": True,
            "purged_at": purged_at,
        },
        ensure_ascii=False,
    )
    decision_logs_scrubbed = 0
    for row in snapshot.affected_decision_log_rows:
        cursor = connection.execute(
            """
            UPDATE memory_decision_logs
            SET candidate_json = ?, reason = ?
            WHERE id = ? AND user_id = ?
            """,
            (replacement, "历史记录因永久删除已脱敏", row["id"], user_id),
        )
        decision_logs_scrubbed += max(0, int(cursor.rowcount))

    space_links_deleted = 0
    memories_deleted = 0
    for offset in range(0, len(snapshot.purge_memory_ids), 500):
        batch = snapshot.purge_memory_ids[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        link_cursor = connection.execute(
            f"""
            DELETE FROM memory_space_links
            WHERE user_id = ? AND memory_id IN ({placeholders})
            """,
            (user_id, *batch),
        )
        space_links_deleted += max(0, int(link_cursor.rowcount))
        memory_cursor = connection.execute(
            f"""
            DELETE FROM memories
            WHERE user_id = ? AND id IN ({placeholders})
            """,
            (user_id, *batch),
        )
        memories_deleted += max(0, int(memory_cursor.rowcount))

    effects = {
        "requested_memories_deleted": len(snapshot.requested_memory_ids),
        "dependent_memories_deleted": len(snapshot.dependent_memory_ids),
        "memories_deleted": memories_deleted,
        "space_links_deleted": space_links_deleted,
        "temporal_references_relinked": temporal_references_relinked,
        "core_sections_scrubbed": core_sections_scrubbed,
        "core_history_scrubbed": core_history_scrubbed,
        "decision_logs_scrubbed": decision_logs_scrubbed,
    }
    if effects != snapshot.effects:
        raise RuntimeError("Batch purge effects drifted inside the locked transaction.")
    return effects


def _insert_batch_purge_audit(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    snapshot: _BatchPurgeSnapshot,
    effects: dict[str, int],
    fingerprint: str,
    purged_at: str,
    call_source: str,
) -> DecisionLog:
    log = DecisionLog(
        id=new_memory_id(),
        user_id=user_id,
        conversation_id=None,
        candidate_json=json.dumps(
            {
                "source": "permanent_purge_batch",
                "requested_memory_ids": snapshot.requested_memory_ids,
                "purged_memory_ids": snapshot.purge_memory_ids,
                "affected_core_sections": snapshot.affected_core_memory_sections,
                "fingerprint": fingerprint,
                "effects": effects,
                "purged_at": purged_at,
                "call_source": call_source,
            },
            ensure_ascii=False,
        ),
        decision="purge",
        reason="批量永久删除回收站记忆",
        created_at=purged_at,
    )
    connection.execute(
        """
        INSERT INTO memory_decision_logs (
            id, user_id, conversation_id, candidate_json, decision, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            log.id,
            log.user_id,
            log.conversation_id,
            log.candidate_json,
            log.decision,
            log.reason,
            log.created_at,
        ),
    )
    connection.execute(
        """
        DELETE FROM memory_decision_logs
        WHERE user_id = ?
          AND id NOT IN (
              SELECT id FROM memory_decision_logs
              WHERE user_id = ?
              ORDER BY created_at DESC, rowid DESC
              LIMIT ?
          )
        """,
        (user_id, user_id, _DECISION_LOG_RETENTION_LIMIT),
    )
    return log


def _derived_memory_dependency_closure(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    root_memory_id: str,
) -> tuple[set[str], list[sqlite3.Row]]:
    """Return every user-scoped memory transitively backed by a root.

    Evidence provenance, rather than ``origin``, is the deletion boundary.  A
    merge keeps the primary row's ``user_asserted`` origin while recording the
    merged fragments as evidence, so filtering this scan to ``agent_derived``
    would leave both copied content and dangling evidence IDs after a purge.
    """
    evidence_rows = connection.execute(
        """
        SELECT id, content, source_message, source_conversation_id,
               evidence_memory_ids_json
        FROM memories
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchall()
    dependents_by_evidence_id: dict[str, list[sqlite3.Row]] = {}
    for row in evidence_rows:
        for evidence_id in set(_json_string_list(row["evidence_memory_ids_json"])):
            dependents_by_evidence_id.setdefault(evidence_id, []).append(row)

    affected_ids = {root_memory_id}
    dependent_by_id: dict[str, sqlite3.Row] = {}
    frontier = deque([root_memory_id])
    while frontier:
        evidence_id = frontier.popleft()
        for row in dependents_by_evidence_id.get(evidence_id, []):
            dependent_id = str(row["id"])
            if dependent_id in affected_ids:
                continue
            affected_ids.add(dependent_id)
            dependent_by_id[dependent_id] = row
            frontier.append(dependent_id)
    return affected_ids, list(dependent_by_id.values())


def _repair_purged_temporal_references(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    affected_ids: set[str],
    updated_at: str,
) -> int:
    """Bridge temporal links around rows that will be permanently removed."""
    rows = connection.execute(
        """
        SELECT id, supersedes, superseded_by
        FROM memories
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchall()
    links = {
        str(row["id"]): (
            str(row["supersedes"]) if row["supersedes"] else None,
            str(row["superseded_by"]) if row["superseded_by"] else None,
        )
        for row in rows
    }

    def follow(start_id: str | None, *, index: int, survivor_id: str) -> str | None:
        current = start_id
        visited: set[str] = set()
        while current in affected_ids:
            if current in visited:
                return None
            visited.add(current)
            current_links = links.get(current)
            if current_links is None:
                return None
            current = current_links[index]
        if current is None or current == survivor_id or current not in links:
            return None
        return current

    updated_count = 0
    for row in rows:
        memory_id = str(row["id"])
        if memory_id in affected_ids:
            continue
        previous_id = str(row["supersedes"]) if row["supersedes"] else None
        next_id = str(row["superseded_by"]) if row["superseded_by"] else None
        repaired_previous = (
            follow(previous_id, index=0, survivor_id=memory_id)
            if previous_id in affected_ids
            else previous_id
        )
        repaired_next = (
            follow(next_id, index=1, survivor_id=memory_id)
            if next_id in affected_ids
            else next_id
        )
        if repaired_previous == previous_id and repaired_next == next_id:
            continue
        cursor = connection.execute(
            """
            UPDATE memories
            SET supersedes = ?, superseded_by = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (repaired_previous, repaired_next, updated_at, memory_id, user_id),
        )
        updated_count += max(0, int(cursor.rowcount))
    return updated_count


def _scrub_purged_memory_artifacts(
    connection: sqlite3.Connection,
    *,
    memory: MemoryRecord,
    purged_at: str,
) -> dict[str, object]:
    affected_ids, dependent_rows = _derived_memory_dependency_closure(
        connection,
        user_id=memory.user_id,
        root_memory_id=memory.id,
    )
    dependent_ids = [str(row["id"]) for row in dependent_rows]
    temporal_references_relinked = _repair_purged_temporal_references(
        connection,
        user_id=memory.user_id,
        affected_ids=affected_ids,
        updated_at=purged_at,
    )
    affected_conversation_ids = {
        conversation_id
        for conversation_id in (
            memory.source_conversation_id,
            *(str(row["source_conversation_id"] or "") for row in dependent_rows),
        )
        if conversation_id
    }

    core_scrubbed = 0
    core_rows = connection.execute(
        """
        SELECT id, section, version, evidence_memory_ids_json
        FROM core_memory_sections
        WHERE user_id = ?
        """,
        (memory.user_id,),
    ).fetchall()
    affected_core_sections: list[dict] = []
    for row in core_rows:
        if not affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"])):
            continue
        cursor = connection.execute(
            """
            UPDATE core_memory_sections
            SET content = ?, evidence_memory_ids_json = '[]', archived = 1, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            ("[redacted: purged evidence]", purged_at, row["id"], memory.user_id),
        )
        core_scrubbed += max(0, int(cursor.rowcount))
        affected_core_sections.append(
            {
                "id": str(row["id"]),
                "section": str(row["section"]),
                "version": row["version"],
            }
        )

    history_scrubbed = 0
    history_rows = connection.execute(
        """
        SELECT id, evidence_memory_ids_json
        FROM core_memory_section_history
        WHERE user_id = ?
        """,
        (memory.user_id,),
    ).fetchall()
    for row in history_rows:
        if not affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"])):
            continue
        cursor = connection.execute(
            """
            UPDATE core_memory_section_history
            SET content = ?, evidence_memory_ids_json = '[]', updated_at = ?, replaced_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                "[redacted: purged evidence]",
                purged_at,
                purged_at,
                row["id"],
                memory.user_id,
            ),
        )
        history_scrubbed += max(0, int(cursor.rowcount))

    log_scrubbed = 0
    sensitive_fragments = {
        memory.id,
        memory.content,
        memory.source_message or "",
        *dependent_ids,
        *(str(row["content"] or "") for row in dependent_rows),
        *(str(row["source_message"] or "") for row in dependent_rows),
    }
    sensitive_fragments.discard("")
    prefilter = _decision_log_scrub_prefilter(
        affected_conversation_ids=affected_conversation_ids,
        fragments=sensitive_fragments,
    )
    if prefilter is None:
        log_rows = connection.execute(
            """
            SELECT id, conversation_id, candidate_json, reason
            FROM memory_decision_logs
            WHERE user_id = ?
            """,
            (memory.user_id,),
        ).fetchall()
    else:
        log_rows = connection.execute(
            f"""
            SELECT id, conversation_id, candidate_json, reason
            FROM memory_decision_logs
            WHERE user_id = ? AND ({prefilter[0]})
            """,
            (memory.user_id, *prefilter[1]),
        ).fetchall()
    for row in log_rows:
        candidate_json = str(row["candidate_json"] or "")
        reason = str(row["reason"] or "")
        if not (
            _decision_log_references_memory_ids(candidate_json, affected_ids)
            or (
                row["conversation_id"]
                and str(row["conversation_id"]) in affected_conversation_ids
            )
            or _decision_log_contains_fragments(
                candidate_json=candidate_json,
                reason=reason,
                fragments=sensitive_fragments,
            )
        ):
            continue
        replacement = json.dumps(
            {
                "source": "purged_memory_history",
                "memory_id": memory.id,
                "redacted": True,
                "purged_at": purged_at,
            },
            ensure_ascii=False,
        )
        cursor = connection.execute(
            """
            UPDATE memory_decision_logs
            SET candidate_json = ?, reason = ?
            WHERE id = ? AND user_id = ?
            """,
            (replacement, "历史记录因永久删除已脱敏", row["id"], memory.user_id),
        )
        log_scrubbed += max(0, int(cursor.rowcount))

    dependent_deleted = 0
    for offset in range(0, len(dependent_ids), 500):
        batch = dependent_ids[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        connection.execute(
            f"DELETE FROM memory_space_links WHERE user_id = ? AND memory_id IN ({placeholders})",
            (memory.user_id, *batch),
        )
        cursor = connection.execute(
            f"DELETE FROM memories WHERE user_id = ? AND id IN ({placeholders})",
            (memory.user_id, *batch),
        )
        dependent_deleted += max(0, int(cursor.rowcount))

    return {
        "dependent_memories_deleted": dependent_deleted,
        # Backward-compatible audit field retained for existing consumers.
        "derived_memories_deleted": dependent_deleted,
        "temporal_references_relinked": temporal_references_relinked,
        "core_sections_scrubbed": core_scrubbed,
        "core_history_scrubbed": history_scrubbed,
        "decision_logs_scrubbed": log_scrubbed,
        "affected_core_sections": affected_core_sections,
    }


_DECISION_LOG_MEMORY_REFERENCE_KEYS = {
    "allowed_memory_ids",
    "archived_memory_ids",
    "evidence_memory_ids",
    "memory_id",
    "memory_ids",
    "new_memory_id",
    "previous_superseded_by",
    "primary_superseded_id",
    "resolved_ids",
    "source_ids",
    "superseded_by",
    "superseded_memory_ids",
    "supersedes",
    "target_memory_id",
}


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _json_like_safe(value: str) -> bool:
    # decision log 的 candidate_json 用 ensure_ascii=False 存储，片段中只有
    # 引号、反斜杠和控制字符会被 JSON 转义，导致 LIKE 无法匹配原文。
    return '"' not in value and "\\" not in value and all(
        ord(char) >= 0x20 for char in value
    )


def _decision_log_scrub_prefilter(
    *,
    affected_conversation_ids: set[str],
    fragments: set[str],
) -> tuple[str, list[object]] | None:
    """构造 SQL 层超集预过滤，只保留可能命中 Python 侧引用/片段判定的日志行。

    片段同时覆盖 memory id（引用判定要求 id 作为 JSON 叶子出现，raw 文本必含该 id）。
    返回 None 表示片段含 JSON 转义字符、无法安全下推 LIKE，调用方退化为按用户全量扫描。
    """
    if len(affected_conversation_ids) + (2 * len(fragments)) > 500:
        return None
    clauses: list[str] = []
    params: list[object] = []
    if affected_conversation_ids:
        placeholders = ", ".join("?" for _ in affected_conversation_ids)
        clauses.append(f"conversation_id IN ({placeholders})")
        params.extend(sorted(affected_conversation_ids))
    for fragment in sorted(fragments):
        if not _json_like_safe(fragment):
            return None
        escaped = _like_escape(fragment)
        if len(fragment) >= 12:
            # 长片段命中条件是 substring 匹配，raw JSON / reason 必包含原文。
            clauses.append("candidate_json LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
            clauses.append("reason LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        else:
            # 短片段只在 Python 侧做叶子等值匹配，raw JSON 中表现为带引号的完整串。
            clauses.append("candidate_json LIKE ? ESCAPE '\\'")
            params.append(f'%"{escaped}"%')
            clauses.append("reason = ?")
            params.append(fragment)
    if not clauses:
        return None
    return " OR ".join(f"({clause})" for clause in clauses), params


def _decision_log_references_memory_ids(raw_json: str, memory_ids: set[str]) -> bool:
    try:
        payload = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return False
    return _payload_references_memory_ids(payload, memory_ids=memory_ids)


def _payload_references_memory_ids(
    value: object,
    *,
    memory_ids: set[str],
    reference_context: bool = False,
) -> bool:
    if reference_context:
        if isinstance(value, str):
            return value in memory_ids
        if isinstance(value, list):
            return any(
                _payload_references_memory_ids(
                    item,
                    memory_ids=memory_ids,
                    reference_context=True,
                )
                for item in value
            )
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).casefold()
            is_reference = (
                key in _DECISION_LOG_MEMORY_REFERENCE_KEYS
                or key.endswith("_memory_id")
                or key.endswith("_memory_ids")
            )
            if _payload_references_memory_ids(
                item,
                memory_ids=memory_ids,
                reference_context=is_reference,
            ):
                return True
    elif isinstance(value, list):
        return any(
            _payload_references_memory_ids(item, memory_ids=memory_ids)
            for item in value
        )
    return False


def _decision_log_contains_fragments(
    *,
    candidate_json: str,
    reason: str,
    fragments: set[str],
) -> bool:
    try:
        payload = json.loads(candidate_json)
    except (json.JSONDecodeError, TypeError):
        payload = candidate_json
    values = [*_json_leaf_strings(payload), reason]
    return any(
        value == fragment or (len(fragment) >= 12 and fragment in value)
        for value in values
        for fragment in fragments
    )


def _json_leaf_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            leaf
            for item in value.values()
            for leaf in _json_leaf_strings(item)
        ]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _json_leaf_strings(item)]
    return []


def _time_ripple_anchor(row: sqlite3.Row):
    return _parse_iso_datetime(row["valid_from"] or row["created_at"])


def _time_ripple_profiles(rows: list[sqlite3.Row]) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for row in rows:
        anchor = _time_ripple_anchor(row)
        if anchor is None:
            continue
        profiles[str(row["id"])] = {
            "anchor": anchor,
            "topics": _casefold_set(_json_string_list(row["topics_json"])),
            "spaces": set(),
        }
    return profiles


def _casefold_set(values: list[str]) -> set[str]:
    return {value.casefold() for value in values if value}


def normalize_classification_name(value: str, *, field_name: str) -> str:
    normalized = " ".join(str(value).strip().split())
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    if len(normalized) > 40:
        raise ValueError(f"{field_name} 不能超过 40 个字符")
    return normalized


def normalize_classification_names(
    values: list[str],
    *,
    max_items: int,
    field_name: str,
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        raw = " ".join(str(value).strip().split())
        if not raw:
            continue
        if len(raw) > 40:
            raise ValueError(f"{field_name} 单项不能超过 40 个字符")
        key = raw.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(raw)
    if len(normalized) > max_items:
        raise ValueError(f"{field_name} 最多 {max_items} 个")
    return normalized


def _coerce_string_list(raw_value: object) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    return [str(value) for value in raw_value if value]


def _coerce_int(raw_value: object, *, default: int) -> int:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _coerce_float(raw_value: object, *, default: float) -> float:
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return default


def _coerce_float_or_none(raw_value: object) -> float | None:
    if raw_value in (None, ""):
        return None
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def _bounded_float(raw_value: object, *, default: float) -> float:
    value = _coerce_float(raw_value, default=default)
    return max(0.0, min(1.0, value))


def _average_float(values: list[float], *, default: float) -> float:
    if not values:
        return default
    return round(sum(values) / len(values), 3)


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _core_section_audit_summaries(sections: list[dict]) -> list[dict]:
    summaries: list[dict] = []
    for section in sections:
        section_name = section.get("section")
        if not section_name:
            continue
        summary = {"section": str(section_name)}
        section_id = section.get("id")
        if section_id:
            summary["id"] = str(section_id)
        version = section.get("version")
        if version is not None:
            summary["version"] = version
        summaries.append(summary)
    return summaries


def _merge_core_section_audit_summaries(*section_groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for sections in section_groups:
        for summary in _core_section_audit_summaries(sections):
            identity = (
                str(summary.get("id") or ""),
                str(summary.get("section") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(summary)
    return merged


def _join_memory_contents(memories: list[MemoryRecord]) -> str:
    parts = []
    for memory in memories:
        normalized = memory.content.strip().rstrip("。.!?！？")
        if normalized:
            parts.append(normalized)
    if not parts:
        return ""
    return "；".join(_ordered_unique(parts)) + "。"


def _merged_type(memories: list[MemoryRecord]) -> MemoryType:
    types = {memory.type for memory in memories}
    return memories[0].type if len(types) == 1 else "semantic"


def _merged_stability(memories: list[MemoryRecord]) -> MemoryStability:
    values = {memory.stability for memory in memories}
    for stability in ("stable", "medium", "temporary"):
        if stability in values:
            return stability
    return "stable"


def _merged_sensitivity(memories: list[MemoryRecord]) -> MemorySensitivity:
    values = {memory.sensitivity for memory in memories}
    for sensitivity in ("sensitive", "private", "normal"):
        if sensitivity in values:
            return sensitivity
    return "normal"


def _shared_value(values: list[str | None]) -> str | None:
    non_empty = {value for value in values if value}
    return next(iter(non_empty)) if len(non_empty) == 1 else None


def _earliest_datetime_text(values: list[str]) -> str | None:
    if not values:
        return None
    return min(values)


# ---------------------------------------------------------------------------
# Schema migrations (PRAGMA user_version)
#
# 每次启动只执行尚未应用的版本。v1 汇总了历史遗留的一次性修复；v2
# 为向量增加显式空间标识，且故意不回填旧向量；v3 增加持久化
# revision，并在建立 Core Memory 单活跃唯一索引前合并历史重复行；v4
# 增加跨 worker/进程重试仍有效、且不保存正文的聊天副作用 claim。


def _memory_migration_v1(connection: sqlite3.Connection) -> None:
    MemoryStore._ensure_memories_usage_columns(connection)
    MemoryStore._ensure_decision_logs_user_id(connection)
    MemoryStore._ensure_core_memory_sections_columns(connection)
    MemoryStore._ensure_recent_context_summary_columns(connection)
    MemoryStore._archive_duplicate_recent_context_summaries(connection)


def _memory_migration_v2(connection: sqlite3.Connection) -> None:
    MemoryStore._ensure_memories_embedding_space_column(connection)


def _memory_migration_v3(connection: sqlite3.Connection) -> None:
    MemoryStore._ensure_revision_columns(connection)
    MemoryStore._merge_duplicate_active_core_sections(connection)
    core_columns = connection.execute(
        "PRAGMA table_info(core_memory_sections)"
    ).fetchall()
    if core_columns:
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_core_memory_user_section_active
            ON core_memory_sections(user_id, section)
            WHERE archived = 0
            """
        )


def _memory_migration_v4(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_side_effect_claims (
            kind TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (kind, key_hash)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_side_effect_claims_expiry
        ON chat_side_effect_claims(expires_at)
        """
    )


_MEMORY_SCHEMA_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _memory_migration_v1),
    (2, _memory_migration_v2),
    (3, _memory_migration_v3),
    (4, _memory_migration_v4),
]
