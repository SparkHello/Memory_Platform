from pathlib import Path
import json
import sqlite3

from pydantic import ValidationError

from app.memory.models import (
    CoreMemorySection,
    CoreMemorySectionHistory,
    CoreMemorySectionName,
    DecisionLog,
    MemoryAction,
    MemoryMergeResult,
    MemoryRecord,
    MemorySensitivity,
    MemorySourceExplanation,
    MemoryStability,
    MemoryType,
    RecentContextSummary,
    new_memory_id,
    utc_now_iso,
)


class MemoryStore:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def init_db(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    content TEXT,
                    type TEXT,
                    importance INTEGER,
                    confidence REAL,
                    source_message TEXT,
                    source_conversation_id TEXT,
                    embedding_json TEXT,
                    last_used_at TEXT,
                    usage_count INTEGER DEFAULT 0,
                    stability TEXT DEFAULT 'stable',
                    valid_until TEXT,
                    review_after TEXT,
                    sensitivity TEXT DEFAULT 'normal',
                    evidence_memory_ids_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    archived_at TEXT,
                    archived INTEGER DEFAULT 0
                )
                """
            )
            self._ensure_memories_usage_columns(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_user_archived ON memories(user_id, archived)"
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
            self._ensure_decision_logs_user_id(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_decision_logs_user_conversation
                ON memory_decision_logs(user_id, conversation_id, created_at)
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
            self._ensure_core_memory_sections_columns(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_core_memory_sections_user_archived
                ON core_memory_sections(user_id, archived, section)
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
                CREATE INDEX IF NOT EXISTS idx_core_memory_history_user_section
                ON core_memory_section_history(user_id, section, replaced_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recent_context_summaries (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    conversation_id TEXT,
                    summary TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    archived INTEGER DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recent_context_user_updated
                ON recent_context_summaries(user_id, archived, updated_at)
                """
            )

    def create_memory(
        self,
        *,
        user_id: str,
        content: str,
        type: MemoryType = "fact",
        importance: int = 1,
        confidence: float = 0.7,
        source_message: str | None = None,
        source_conversation_id: str | None = None,
        embedding_json: str | None = None,
        stability: MemoryStability = "stable",
        valid_until: str | None = None,
        review_after: str | None = None,
        sensitivity: MemorySensitivity = "normal",
        evidence_memory_ids: list[str] | None = None,
    ) -> MemoryRecord:
        now = utc_now_iso()
        evidence_memory_ids = evidence_memory_ids or []
        memory = MemoryRecord(
            id=new_memory_id(),
            user_id=user_id,
            content=content,
            type=type,
            importance=importance,
            confidence=confidence,
            source_message=source_message,
            source_conversation_id=source_conversation_id,
            embedding_json=embedding_json,
            last_used_at=None,
            usage_count=0,
            stability=stability,
            valid_until=valid_until,
            review_after=review_after,
            sensitivity=sensitivity,
            evidence_memory_ids=evidence_memory_ids,
            created_at=now,
            updated_at=now,
            archived_at=None,
            archived=0,
        )
        evidence_json = json.dumps(evidence_memory_ids, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memories (
                    id, user_id, content, type, importance, confidence,
                    source_message, source_conversation_id, embedding_json,
                    last_used_at, usage_count, stability, valid_until, review_after,
                    sensitivity, evidence_memory_ids_json, created_at, updated_at,
                    archived_at, archived
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory.id,
                    memory.user_id,
                    memory.content,
                    memory.type,
                    memory.importance,
                    memory.confidence,
                    memory.source_message,
                    memory.source_conversation_id,
                    memory.embedding_json,
                    memory.last_used_at,
                    memory.usage_count,
                    memory.stability,
                    memory.valid_until,
                    memory.review_after,
                    memory.sensitivity,
                    evidence_json,
                    memory.created_at,
                    memory.updated_at,
                    memory.archived_at,
                    memory.archived,
                ),
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
        source_message: str | None = None,
        source_conversation_id: str | None = None,
        embedding_json: str | None = None,
        stability: MemoryStability = "stable",
        valid_until: str | None = None,
        review_after: str | None = None,
        sensitivity: MemorySensitivity = "normal",
        evidence_memory_ids: list[str] | None = None,
    ) -> MemoryRecord | None:
        now = utc_now_iso()
        if evidence_memory_ids is None:
            existing = self.get_memory(memory_id=memory_id, user_id=user_id)
            if existing is None:
                return None
            evidence_memory_ids = existing.evidence_memory_ids
        evidence_json = json.dumps(evidence_memory_ids, ensure_ascii=False)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories
                SET content = ?, type = ?, importance = ?, confidence = ?,
                    source_message = ?, source_conversation_id = ?,
                    embedding_json = ?, stability = ?, valid_until = ?,
                    review_after = ?, sensitivity = ?,
                    evidence_memory_ids_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (
                    content,
                    type,
                    importance,
                    confidence,
                    source_message,
                    source_conversation_id,
                    embedding_json,
                    stability,
                    valid_until,
                    review_after,
                    sensitivity,
                    evidence_json,
                    now,
                    memory_id,
                    user_id,
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

    def list_memories(self, *, user_id: str, limit: int = 200) -> list[MemoryRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND archived = 0
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [self._row_to_memory(row) for row in rows]

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
        return [self._row_to_memory(row) for row in rows]

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
            self._create_core_memory_section_history(
                connection=None,
                section=existing,
                replaced_at=now,
            )
            with self._connect() as connection:
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
        archived_ids = [memory.id for memory in active_memories[1:]]
        updated = self.update_memory(
            memory_id=target.id,
            user_id=user_id,
            content=merged_content,
            type=_merged_type(active_memories),
            importance=max(memory.importance for memory in active_memories),
            confidence=max(memory.confidence for memory in active_memories),
            source_message=target.source_message,
            source_conversation_id=target.source_conversation_id,
            embedding_json=None,
            stability=_merged_stability(active_memories),
            valid_until=_shared_value([memory.valid_until for memory in active_memories]),
            review_after=_earliest_datetime_text(
                [memory.review_after for memory in active_memories if memory.review_after]
            ),
            sensitivity=_merged_sensitivity(active_memories),
            evidence_memory_ids=evidence_memory_ids,
        )
        if updated is None:
            return MemoryMergeResult(action="ignore", reason="保留目标记忆不存在或已删除")

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
        if conversation_id is None:
            query = """
                SELECT * FROM recent_context_summaries
                WHERE user_id = ? AND conversation_id IS NULL AND archived = 0
                ORDER BY updated_at DESC
                LIMIT 1
            """
            params = (user_id,)
        else:
            query = """
                SELECT * FROM recent_context_summaries
                WHERE user_id = ? AND conversation_id = ? AND archived = 0
                ORDER BY updated_at DESC
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
        normalized_summary = summary.strip()
        existing = self.get_recent_context_summary(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        now = utc_now_iso()
        if existing:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE recent_context_summaries
                    SET summary = ?, updated_at = ?
                    WHERE id = ? AND user_id = ? AND archived = 0
                    """,
                    (normalized_summary, now, existing.id, user_id),
                )
            updated = self.get_recent_context_summary(
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return updated if updated else existing

        recent_summary = RecentContextSummary(
            id=new_memory_id(),
            user_id=user_id,
            conversation_id=conversation_id,
            summary=normalized_summary,
            created_at=now,
            updated_at=now,
            archived=0,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recent_context_summaries (
                    id, user_id, conversation_id, summary, created_at, updated_at, archived
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recent_summary.id,
                    recent_summary.user_id,
                    recent_summary.conversation_id,
                    recent_summary.summary,
                    recent_summary.created_at,
                    recent_summary.updated_at,
                    recent_summary.archived,
                ),
            )
        return recent_summary

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

    def import_memory_record(
        self,
        *,
        user_id: str,
        data: dict,
        overwrite: bool = False,
        archived: int | None = None,
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

        try:
            memory = MemoryRecord(
                id=memory_id,
                user_id=user_id,
                content=content,
                type=data.get("type", "fact"),
                importance=_coerce_int(data.get("importance"), default=1),
                confidence=_coerce_float(data.get("confidence"), default=0.7),
                source_message=data.get("source_message"),
                source_conversation_id=data.get("source_conversation_id"),
                embedding_json=None,
                last_used_at=data.get("last_used_at"),
                usage_count=max(0, _coerce_int(data.get("usage_count"), default=0)),
                stability=data.get("stability", "stable"),
                valid_until=data.get("valid_until"),
                review_after=data.get("review_after"),
                sensitivity=data.get("sensitivity", "normal"),
                evidence_memory_ids=evidence_memory_ids,
                created_at=str(data.get("created_at") or now),
                updated_at=now,
                archived_at=archived_at,
                archived=archived_value,
            )
        except ValidationError:
            return "invalid", None

        evidence_json = json.dumps(memory.evidence_memory_ids, ensure_ascii=False)
        params = (
            memory.user_id,
            memory.content,
            memory.type,
            memory.importance,
            memory.confidence,
            memory.source_message,
            memory.source_conversation_id,
            memory.embedding_json,
            memory.last_used_at,
            memory.usage_count,
            memory.stability,
            memory.valid_until,
            memory.review_after,
            memory.sensitivity,
            evidence_json,
            memory.created_at,
            memory.updated_at,
            memory.archived_at,
            memory.archived,
            memory.id,
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM memories WHERE id = ?",
                (memory.id,),
            ).fetchone()
            if row is not None and row["user_id"] == user_id:
                connection.execute(
                    """
                    UPDATE memories
                    SET user_id = ?, content = ?, type = ?, importance = ?,
                        confidence = ?, source_message = ?,
                        source_conversation_id = ?, embedding_json = ?,
                        last_used_at = ?, usage_count = ?, stability = ?,
                        valid_until = ?, review_after = ?, sensitivity = ?,
                        evidence_memory_ids_json = ?, created_at = ?,
                        updated_at = ?, archived_at = ?, archived = ?
                    WHERE id = ?
                    """,
                    params,
                )
                return "updated", memory
            connection.execute(
                """
                INSERT INTO memories (
                    user_id, content, type, importance, confidence,
                    source_message, source_conversation_id, embedding_json,
                    last_used_at, usage_count, stability, valid_until,
                    review_after, sensitivity, evidence_memory_ids_json,
                    created_at, updated_at, archived_at, archived, id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
        return "created", memory

    def mark_memories_used(self, *, memory_ids: list[str], user_id: str) -> str | None:
        if not memory_ids:
            return None
        now = utc_now_iso()
        placeholders = ", ".join("?" for _ in memory_ids)
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE memories
                SET usage_count = COALESCE(usage_count, 0) + 1,
                    last_used_at = ?
                WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
                """,
                (now, user_id, *memory_ids),
            )
        return now

    def create_decision_log(
        self,
        *,
        user_id: str = "default",
        conversation_id: str | None,
        candidate_json: str,
        decision: str,
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
        with self._connect() as connection:
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
        return log

    def list_decision_logs(
        self,
        *,
        user_id: str | None = None,
        conversation_id: str | None = None,
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
        params.append(limit)
        with self._connect() as connection:
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _ensure_memories_usage_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "last_used_at" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN last_used_at TEXT")
        if "usage_count" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN usage_count INTEGER DEFAULT 0")
        if "stability" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN stability TEXT DEFAULT 'stable'")
        if "valid_until" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN valid_until TEXT")
        if "review_after" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN review_after TEXT")
        if "sensitivity" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN sensitivity TEXT DEFAULT 'normal'")
        if "evidence_memory_ids_json" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN evidence_memory_ids_json TEXT")
        if "archived_at" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN archived_at TEXT")

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
    def _ensure_decision_logs_user_id(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(memory_decision_logs)").fetchall()
        }
        if "user_id" not in columns:
            connection.execute(
                "ALTER TABLE memory_decision_logs ADD COLUMN user_id TEXT DEFAULT 'default'"
            )

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> MemoryRecord:
        data = dict(row)
        raw_evidence = data.pop("evidence_memory_ids_json", None)
        data["evidence_memory_ids"] = _json_string_list(raw_evidence)
        return MemoryRecord(**data)

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
        return RecentContextSummary(**dict(row))


def _json_string_list(raw_value: str | None) -> list[str]:
    try:
        values = json.loads(raw_value) if raw_value else []
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if value]


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


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


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
    return memories[0].type if len(types) == 1 else "fact"


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
