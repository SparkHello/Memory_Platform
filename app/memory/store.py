from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import hashlib
import json
import sqlite3

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
from app.memory.utils import _parse_iso_datetime
from app.schema_migrations import apply_schema_migrations, validated_schema_version


_UNSET = object()
_TIME_RIPPLE_MAX_CANDIDATES = 100
_SENSITIVITY_RANK = {"normal": 0, "private": 1, "sensitive": 2}
# 每用户决策日志保留上限：超出后按创建时间从旧到新裁剪，避免全库无界增长。
_DECISION_LOG_RETENTION_LIMIT = 5000
# Branch snapshots are an operational context index, not an unlimited transcript
# archive. Old nodes can always fall back to the visible history sent by the client.
_CONVERSATION_BRANCH_NODE_RETENTION_LIMIT = 5000


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

    def init_db(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            validated_schema_version(
                connection,
                _MEMORY_SCHEMA_MIGRATIONS,
                schema_name="memory database",
            )
            self._create_tables(connection)
            self._run_migrations(connection)
            self._create_indexes(connection)

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
                archived INTEGER DEFAULT 0
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
                archived INTEGER DEFAULT 0
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
                replaced_at TEXT
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
            created_at=now,
            updated_at=now,
            archived_at=None,
            archived=0,
        )
        with self._connect() as connection:
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
        stability: MemoryStability = "stable",
        valid_from: object = _UNSET,
        valid_until: str | None = None,
        review_after: str | None = None,
        sensitivity: MemorySensitivity = "normal",
        evidence_memory_ids: list[str] | None = None,
        topics: list[str] | None = None,
        entities: list[str] | None = None,
        temporal_subject: object = _UNSET,
        temporal_predicate: object = _UNSET,
        status: str | None = None,
    ) -> MemoryRecord | None:
        now = utc_now_iso()
        existing = self.get_memory(memory_id=memory_id, user_id=user_id)
        if existing is None:
            return None
        if evidence_memory_ids is None:
            evidence_memory_ids = existing.evidence_memory_ids
        if topics is None or entities is None:
            if topics is None:
                topics = existing.topics
            if entities is None:
                entities = existing.entities
        if valid_from is _UNSET:
            valid_from = existing.valid_from
        if temporal_subject is _UNSET:
            temporal_subject = existing.temporal_subject
        if temporal_predicate is _UNSET:
            temporal_predicate = existing.temporal_predicate
        topics = normalize_classification_names(topics, max_items=20, field_name="topics")
        entities = normalize_classification_names(entities, max_items=20, field_name="entities")
        sensitivity = _sensitivity_with_floor(
            declared=sensitivity,
            content=content,
            source_message=source_message,
            entities=entities,
        )
        valid_from = normalize_iso_text(valid_from)
        valid_until = normalize_iso_text(valid_until)
        review_after = normalize_iso_text(review_after)
        temporal_subject = normalize_optional_text(temporal_subject)
        temporal_predicate = normalize_optional_text(temporal_predicate)
        evidence_json = json.dumps(evidence_memory_ids, ensure_ascii=False)
        topics_json = json.dumps(topics, ensure_ascii=False)
        entities_json = json.dumps(entities, ensure_ascii=False)
        with self._connect() as connection:
            if status is not None:
                cursor = connection.execute(
                    """
                    UPDATE memories
                    SET content = ?, type = ?, importance = ?, confidence = ?,
                        valence = ?, arousal = ?, source_message = ?, source_conversation_id = ?,
                        embedding_json = ?, stability = ?, valid_from = ?, valid_until = ?,
                        review_after = ?, sensitivity = ?,
                        evidence_memory_ids_json = ?, topics_json = ?, entities_json = ?,
                        temporal_subject = ?, temporal_predicate = ?,
                        status = ?, updated_at = ?
                    WHERE id = ? AND user_id = ? AND archived = 0
                    """,
                    (
                        content, type, importance, confidence,
                        valence, arousal, source_message, source_conversation_id,
                        embedding_json, stability, valid_from, valid_until,
                        review_after, sensitivity,
                        evidence_json, topics_json, entities_json,
                        temporal_subject, temporal_predicate,
                        status, now,
                        memory_id, user_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE memories
                    SET content = ?, type = ?, importance = ?, confidence = ?,
                        valence = ?, arousal = ?, source_message = ?, source_conversation_id = ?,
                        embedding_json = ?, stability = ?, valid_from = ?, valid_until = ?,
                        review_after = ?, sensitivity = ?,
                        evidence_memory_ids_json = ?, topics_json = ?, entities_json = ?,
                        temporal_subject = ?, temporal_predicate = ?,
                        updated_at = ?
                    WHERE id = ? AND user_id = ? AND archived = 0
                    """,
                    (
                        content, type, importance, confidence,
                        valence, arousal, source_message, source_conversation_id,
                        embedding_json, stability, valid_from, valid_until,
                        review_after, sensitivity,
                        evidence_json, topics_json, entities_json,
                        temporal_subject, temporal_predicate,
                        now, memory_id, user_id,
                    ),
                )
            if cursor.rowcount == 0:
                return None
        return self.get_memory(memory_id=memory_id, user_id=user_id)

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
            row = connection.execute(
                """
                SELECT * FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if row is None:
                return None

            before = self._temporal_snapshot(row)
            superseded_by = row["superseded_by"] if "superseded_by" in row.keys() else None
            connection.execute(
                """
                UPDATE memories
                SET valid_until = NULL,
                    superseded_by = NULL,
                    status = 'dynamic',
                    updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (now, memory_id, user_id),
            )
            if superseded_by:
                connection.execute(
                    """
                    UPDATE memories
                    SET supersedes = NULL,
                        updated_at = ?
                    WHERE id = ?
                      AND user_id = ?
                      AND archived = 0
                      AND supersedes = ?
                    """,
                    (now, superseded_by, user_id, memory_id),
                )

            self._insert_decision_log(
                connection=connection,
                user_id=user_id,
                conversation_id=None,
                candidate_json=json.dumps(
                    {
                        "source": "temporal_restore",
                        "memory_id": memory_id,
                        "previous_superseded_by": superseded_by,
                        "before": before,
                        "after": {
                            "valid_until": None,
                            "superseded_by": None,
                            "status": "dynamic",
                        },
                    },
                    ensure_ascii=False,
                ),
                decision="update",
                reason="Restored temporal memory validity",
            )
        return self.get_memory(memory_id=memory_id, user_id=user_id)

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
    ) -> tuple[MemoryAction, CoreMemorySection]:
        existing = self.get_core_memory_section(user_id=user_id, section=section)
        evidence_json = json.dumps(evidence_memory_ids, ensure_ascii=False)
        now = utc_now_iso()

        if existing:
            normalized_content = content.strip()
            if (
                existing.content == normalized_content
                and existing.evidence_memory_ids == evidence_memory_ids
                and abs(existing.confidence - confidence) < 0.001
            ):
                return "ignore", existing
            with self._connect() as connection:
                self._create_core_memory_section_history(
                    connection=connection,
                    section=existing,
                    replaced_at=now,
                )
                connection.execute(
                    """
                    UPDATE core_memory_sections
                    SET content = ?, evidence_memory_ids_json = ?,
                        confidence = ?, version = ?, updated_at = ?
                    WHERE id = ? AND user_id = ? AND archived = 0
                    """,
                    (
                        normalized_content,
                        evidence_json,
                        confidence,
                        existing.version + 1,
                        now,
                        existing.id,
                        user_id,
                    ),
                )
            updated = self.get_core_memory_section(user_id=user_id, section=section)
            return "update", updated if updated else existing

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
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO core_memory_sections (
                    id, user_id, section, content, evidence_memory_ids_json,
                    confidence, version, created_at, updated_at, archived
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
        return "create", core_memory

    def archive_core_memory_section(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName,
    ) -> bool:
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE core_memory_sections
                SET archived = 1, updated_at = ?
                WHERE user_id = ? AND section = ? AND archived = 0
                """,
                (now, user_id, section),
            )
        return cursor.rowcount > 0

    def list_core_memory_section_history(
        self,
        *,
        user_id: str,
        section: CoreMemorySectionName | None = None,
        limit: int = 50,
    ) -> list[CoreMemorySectionHistory]:
        query = """
            SELECT * FROM core_memory_section_history
            WHERE user_id = ?
        """
        params: list[object] = [user_id]
        if section is not None:
            query += " AND section = ?"
            params.append(section)
        query += " ORDER BY replaced_at DESC LIMIT ?"
        params.append(limit)
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

        memories = [
            self.get_memory(memory_id=memory_id, user_id=user_id)
            for memory_id in ordered_ids
        ]
        if any(memory is None for memory in memories):
            return MemoryMergeResult(
                action="ignore",
                reason="部分记忆不存在或已删除，无法合并",
            )

        active_memories = [memory for memory in memories if memory is not None]
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
        topics = _ordered_unique(
            [topic for memory in active_memories for topic in memory.topics]
        )
        entities = _ordered_unique(
            [entity for memory in active_memories for entity in memory.entities]
        )
        space_ids = _ordered_unique(
            [space_id for memory in active_memories for space_id in memory.space_ids]
        )
        archived_ids = [memory.id for memory in active_memories[1:]]
        updated = self.update_memory(
            memory_id=target.id,
            user_id=user_id,
            content=merged_content,
            type=_merged_type(active_memories),
            importance=max(memory.importance for memory in active_memories),
            confidence=max(memory.confidence for memory in active_memories),
            valence=_average_float([memory.valence for memory in active_memories], default=0.5),
            arousal=_average_float([memory.arousal for memory in active_memories], default=0.3),
            source_message=target.source_message,
            source_conversation_id=target.source_conversation_id,
            embedding_json=None,
            stability=_merged_stability(active_memories),
            valid_from=_shared_value([memory.valid_from for memory in active_memories]),
            valid_until=_shared_value([memory.valid_until for memory in active_memories]),
            review_after=_earliest_datetime_text(
                [memory.review_after for memory in active_memories if memory.review_after]
            ),
            sensitivity=_merged_sensitivity(active_memories),
            evidence_memory_ids=evidence_memory_ids,
            topics=topics,
            entities=entities,
            temporal_subject=_shared_value([memory.temporal_subject for memory in active_memories]),
            temporal_predicate=_shared_value([memory.temporal_predicate for memory in active_memories]),
        )
        if updated is None:
            return MemoryMergeResult(action="ignore", reason="保留目标记忆不存在或已删除")
        updated = self.replace_memory_spaces(
            memory_id=updated.id,
            user_id=user_id,
            space_ids=space_ids,
        ) or updated

        for memory_id in archived_ids:
            self.archive_memory(memory_id=memory_id, user_id=user_id)

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
        limit: int = 20,
    ) -> list[RecentContextSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recent_context_summaries
                WHERE user_id = ? AND archived = 0
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
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

    def archive_memory(self, *, memory_id: str, user_id: str) -> bool:
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories
                SET archived = 1, archived_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (now, now, memory_id, user_id),
            )
        return cursor.rowcount > 0

    def restore_memory(self, *, memory_id: str, user_id: str) -> MemoryRecord | None:
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories
                SET archived = 0, archived_at = NULL, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 1
                """,
                (now, memory_id, user_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_memory(memory_id=memory_id, user_id=user_id)

    def update_memory_embedding(
        self,
        *,
        memory_id: str,
        user_id: str,
        embedding_json: str,
    ) -> bool:
        """仅更新活跃记忆的 embedding，用于 re-embed 流程。"""
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories
                SET embedding_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (embedding_json, now, memory_id, user_id),
            )
        return cursor.rowcount > 0

    def archive_expired_memories(self, *, user_id: str) -> int:
        """归档所有 valid_until 已过期的活跃记忆，返回归档数量。"""
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
                        "affected_core_sections": _core_section_audit_summaries(
                            affected_core_sections or []
                        ),
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

    def upsert_memory_space(self, *, user_id: str, name: str) -> MemorySpace:
        display_name = normalize_classification_name(name, field_name="space")
        normalized_name = display_name.casefold()
        now = utc_now_iso()
        with self._connect() as connection:
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
    ) -> MemoryRecord | None:
        normalized_space_ids = _ordered_unique(
            [str(space_id).strip() for space_id in space_ids if str(space_id).strip()]
        )
        create_space_names = create_space_names or []
        if len(normalized_space_ids) + len(create_space_names) > 10:
            raise ValueError("space_ids 最多 10 个")
        created_spaces = [
            self.upsert_memory_space(user_id=user_id, name=name)
            for name in create_space_names
        ]
        normalized_space_ids = _ordered_unique(
            [*normalized_space_ids, *(space.id for space in created_spaces)]
        )
        if len(normalized_space_ids) > 10:
            raise ValueError("space_ids 最多 10 个")
        now = utc_now_iso()
        with self._connect() as connection:
            memory_exists = connection.execute(
                """
                SELECT id FROM memories
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (memory_id, user_id),
            ).fetchone()
            if memory_exists is None:
                return None
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
                SET updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (now, memory_id, user_id),
            )
        return self.get_memory(memory_id=memory_id, user_id=user_id)

    def import_memory_record(
        self,
        *,
        user_id: str,
        data: dict,
        overwrite: bool = False,
        archived: int | None = None,
        space_id_map: dict[str, str] | None = None,
    ) -> tuple[str, MemoryRecord | None]:
        content = str(data.get("content") or "").strip()
        if not content:
            return "invalid", None

        memory_id = str(data.get("id") or new_memory_id())
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT user_id FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        if existing is not None:
            if existing["user_id"] != user_id:
                memory_id = new_memory_id()
            elif not overwrite:
                return "skipped", None

        now = utc_now_iso()
        archived_value = int(data.get("archived", 0) if archived is None else archived)
        archived_value = 1 if archived_value else 0
        archived_at = str(data.get("archived_at") or now) if archived_value else None
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
            )
            memory.sensitivity = _sensitivity_with_floor(
                declared=memory.sensitivity,
                content=memory.content,
                source_message=memory.source_message,
                entities=memory.entities,
            )
        except ValidationError:
            return "invalid", None

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
        with self._connect() as connection:
            memory.space_ids = self._filter_existing_space_ids(
                connection=connection,
                user_id=user_id,
                space_ids=memory.space_ids,
            )
            row = connection.execute(
                "SELECT user_id FROM memories WHERE id = ?",
                (memory.id,),
            ).fetchone()
            if row is not None and row["user_id"] == user_id:
                connection.execute(
                    """
                    UPDATE memories
                    SET user_id = ?, content = ?, type = ?, importance = ?,
                        confidence = ?, valence = ?, arousal = ?, source_message = ?,
                        source_conversation_id = ?, origin = ?, embedding_json = ?,
                        last_used_at = ?, usage_count = ?, stability = ?,
                        valid_from = ?, valid_until = ?, review_after = ?, sensitivity = ?,
                        evidence_memory_ids_json = ?, topics_json = ?, entities_json = ?,
                        temporal_subject = ?, temporal_predicate = ?,
                        status = ?, digested = ?, decay_lambda = ?,
                        supersedes = ?, superseded_by = ?,
                        created_at = ?,
                        updated_at = ?, archived_at = ?, archived = ?
                    WHERE id = ?
                    """,
                    params,
                )
                self._replace_memory_space_links(
                    connection=connection,
                    user_id=user_id,
                    memory_id=memory.id,
                    space_ids=memory.space_ids,
                    created_at=now,
                )
                return "updated", memory
            self._insert_memory_row(connection=connection, memory=memory)
            self._replace_memory_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=memory.id,
                space_ids=memory.space_ids,
                created_at=now,
            )
        return "created", memory

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
              AND COALESCE(status, 'dynamic') IN ('dynamic', 'resolved')
            """,
            (
                user_id,
                new_memory.id,
                new_memory.temporal_subject,
                new_memory.temporal_predicate,
            ),
        ).fetchall()
        eligible_rows: list[tuple[datetime, datetime, str, sqlite3.Row]] = []
        for row in candidate_rows:
            starts_at = _parse_iso_datetime(row["valid_from"] or row["created_at"])
            if starts_at is None or starts_at > effective_instant:
                continue
            valid_until = row["valid_until"]
            if valid_until:
                ends_at = _parse_iso_datetime(valid_until)
                if ends_at is None or ends_at < effective_instant:
                    continue
            updated_at = _parse_iso_datetime(row["updated_at"]) or starts_at
            eligible_rows.append((starts_at, updated_at, str(row["id"]), row))
        eligible_rows.sort(key=lambda item: item[:3], reverse=True)
        rows = [item[3] for item in eligible_rows]
        if not rows:
            return []

        superseded_ids = [str(row["id"]) for row in rows]
        placeholders = ", ".join("?" for _ in superseded_ids)
        now = utc_now_iso()
        connection.execute(
            f"""
            UPDATE memories
            SET valid_until = ?,
                status = 'resolved',
                superseded_by = ?,
                updated_at = ?
            WHERE user_id = ?
              AND archived = 0
              AND id IN ({placeholders})
            """,
            (effective_at, new_memory.id, now, user_id, *superseded_ids),
        )

        primary_superseded_id = superseded_ids[0]
        connection.execute(
            """
            UPDATE memories
            SET supersedes = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (primary_superseded_id, now, new_memory.id, user_id),
        )
        new_memory.supersedes = primary_superseded_id
        new_memory.updated_at = now

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
                    "before": [self._temporal_snapshot(row) for row in rows],
                    "after": [
                        {
                            "id": memory_id,
                            "valid_until": effective_at,
                            "status": "resolved",
                            "superseded_by": new_memory.id,
                        }
                        for memory_id in superseded_ids
                    ],
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
        limit: int = 100,
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
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        # 记忆引用保存在 candidate_json 的多种 key 里，无法直接进 WHERE，
        # 先取最近一批，再在 Python 侧用与 purge  scrub 相同的引用判定过滤
        scan_limit = max(limit, 500) if memory_id else limit
        params.append(scan_limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        logs = [DecisionLog(**dict(row)) for row in rows]
        if memory_id:
            references = {memory_id}
            logs = [
                log
                for log in logs
                if _decision_log_references_memory_ids(log.candidate_json, references)
            ][:limit]
        return logs

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
        )
        query = """
            INSERT INTO core_memory_section_history (
                id, core_memory_section_id, user_id, section, content,
                evidence_memory_ids_json, confidence, version,
                created_at, updated_at, replaced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                last_used_at, usage_count, stability, valid_from, valid_until, review_after,
                sensitivity, evidence_memory_ids_json, topics_json, entities_json,
                temporal_subject, temporal_predicate,
                status, digested, decay_lambda, supersedes, superseded_by,
                created_at, updated_at, archived_at, archived
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
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
        unique_ids = _ordered_unique(memory_ids)
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT memory_id, space_id
                FROM memory_space_links
                WHERE user_id = ? AND memory_id IN ({placeholders})
                ORDER BY created_at ASC, rowid ASC
                """,
                (user_id, *unique_ids),
            ).fetchall()
        result = {memory_id: [] for memory_id in unique_ids}
        for row in rows:
            result.setdefault(str(row["memory_id"]), []).append(str(row["space_id"]))
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
        space_ids_by_memory = self._space_ids_for_memory_ids(
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
        data["usage_count"] = float(data.get("usage_count") or 0)
        data["digested"] = bool(data.get("digested"))
        data["temporal_subject"] = normalize_optional_text(data.get("temporal_subject"))
        data["temporal_predicate"] = normalize_optional_text(data.get("temporal_predicate"))
        data.setdefault("valid_from", None)
        data.setdefault("status", "dynamic")
        data.setdefault("decay_lambda", None)
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


def _scrub_purged_memory_artifacts(
    connection: sqlite3.Connection,
    *,
    memory: MemoryRecord,
    purged_at: str,
) -> dict[str, int]:
    derived_rows = connection.execute(
        """
        SELECT id, content, source_message, source_conversation_id,
               evidence_memory_ids_json
        FROM memories
        WHERE user_id = ? AND COALESCE(origin, 'user_asserted') = 'agent_derived'
        """,
        (memory.user_id,),
    ).fetchall()
    dependent_rows = [
        row
        for row in derived_rows
        if memory.id in _json_string_list(row["evidence_memory_ids_json"])
    ]
    dependent_ids = [str(row["id"]) for row in dependent_rows]
    affected_ids = {memory.id, *dependent_ids}
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
        SELECT id, evidence_memory_ids_json
        FROM core_memory_sections
        WHERE user_id = ?
        """,
        (memory.user_id,),
    ).fetchall()
    for row in core_rows:
        if not affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"])):
            continue
        connection.execute(
            """
            UPDATE core_memory_sections
            SET content = ?, evidence_memory_ids_json = '[]', archived = 1, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            ("[redacted: purged evidence]", purged_at, row["id"], memory.user_id),
        )
        core_scrubbed += 1

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
        connection.execute(
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
        history_scrubbed += 1

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
        connection.execute(
            """
            UPDATE memory_decision_logs
            SET candidate_json = ?, reason = ?
            WHERE id = ? AND user_id = ?
            """,
            (replacement, "历史记录因永久删除已脱敏", row["id"], memory.user_id),
        )
        log_scrubbed += 1

    if dependent_ids:
        placeholders = ", ".join("?" for _ in dependent_ids)
        connection.execute(
            f"DELETE FROM memory_space_links WHERE user_id = ? AND memory_id IN ({placeholders})",
            (memory.user_id, *dependent_ids),
        )
        connection.execute(
            f"DELETE FROM memories WHERE user_id = ? AND id IN ({placeholders})",
            (memory.user_id, *dependent_ids),
        )

    return {
        "derived_memories_deleted": len(dependent_ids),
        "core_sections_scrubbed": core_scrubbed,
        "core_history_scrubbed": history_scrubbed,
        "decision_logs_scrubbed": log_scrubbed,
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
# 每次启动只执行尚未应用的版本。v1 汇总了历史遗留的一次性修复：老库缺列
# 补齐与遗留值回填；新库建表已含全部列，v1 对空表运行无副作用。
# 新增 schema 变更时在此追加 (2, _memory_migration_v2) 并递增版本。


def _memory_migration_v1(connection: sqlite3.Connection) -> None:
    MemoryStore._ensure_memories_usage_columns(connection)
    MemoryStore._ensure_decision_logs_user_id(connection)
    MemoryStore._ensure_core_memory_sections_columns(connection)
    MemoryStore._ensure_recent_context_summary_columns(connection)
    MemoryStore._archive_duplicate_recent_context_summaries(connection)


_MEMORY_SCHEMA_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _memory_migration_v1),
]
