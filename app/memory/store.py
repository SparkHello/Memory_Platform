from pathlib import Path
import sqlite3

from app.memory.models import (
    DecisionLog,
    MemoryRecord,
    MemoryType,
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
                    created_at TEXT,
                    updated_at TEXT,
                    archived INTEGER DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_user_archived ON memories(user_id, archived)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_decision_logs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    candidate_json TEXT,
                    decision TEXT,
                    reason TEXT,
                    created_at TEXT
                )
                """
            )

    def create_memory(
        self,
        *,
        user_id: str,
        content: str,
        type: MemoryType = "other",
        importance: int = 1,
        confidence: float = 0.7,
        source_message: str | None = None,
        source_conversation_id: str | None = None,
        embedding_json: str | None = None,
    ) -> MemoryRecord:
        now = utc_now_iso()
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
            created_at=now,
            updated_at=now,
            archived=0,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memories (
                    id, user_id, content, type, importance, confidence,
                    source_message, source_conversation_id, embedding_json,
                    created_at, updated_at, archived
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    memory.created_at,
                    memory.updated_at,
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
    ) -> MemoryRecord | None:
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories
                SET content = ?, type = ?, importance = ?, confidence = ?,
                    source_message = ?, source_conversation_id = ?,
                    embedding_json = ?, updated_at = ?
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

    def archive_memory(self, *, memory_id: str, user_id: str) -> bool:
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories
                SET archived = 1, updated_at = ?
                WHERE id = ? AND user_id = ? AND archived = 0
                """,
                (now, memory_id, user_id),
            )
        return cursor.rowcount > 0

    def create_decision_log(
        self,
        *,
        conversation_id: str | None,
        candidate_json: str,
        decision: str,
        reason: str,
    ) -> DecisionLog:
        log = DecisionLog(
            id=new_memory_id(),
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
                    id, conversation_id, candidate_json, decision, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    log.id,
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
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[DecisionLog]:
        query = "SELECT * FROM memory_decision_logs"
        params: list[object] = []
        if conversation_id:
            query += " WHERE conversation_id = ?"
            params.append(conversation_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [DecisionLog(**dict(row)) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(**dict(row))

