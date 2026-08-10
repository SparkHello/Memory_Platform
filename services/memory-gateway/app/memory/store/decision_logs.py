"""Decision log helpers."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from app.memory.models import DecisionLog, DecisionLogAction, new_memory_id, utc_now_iso
from app.memory.store.constants import _DECISION_LOG_RETENTION_LIMIT
from app.memory.store.purge_ops import _decision_log_references_memory_ids

if TYPE_CHECKING:
    from app.memory.store._monolith import MemoryStore

def _insert_decision_log(
    store: MemoryStore,
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
    store: MemoryStore,
    *,
    user_id: str = "default",
    conversation_id: str | None,
    candidate_json: str,
    decision: DecisionLogAction,
    reason: str,
) -> DecisionLog:
    with store._connect() as connection:
        return store._insert_decision_log(
            connection=connection,
            user_id=user_id,
            conversation_id=conversation_id,
            candidate_json=candidate_json,
            decision=decision,
            reason=reason,
        )


def list_decision_logs(
    store: MemoryStore,
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
    with store._connect() as connection:
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


def _ensure_decision_logs_user_id(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(memory_decision_logs)").fetchall()
    }
    if "user_id" not in columns:
        connection.execute(
            "ALTER TABLE memory_decision_logs ADD COLUMN user_id TEXT DEFAULT 'default'"
        )


