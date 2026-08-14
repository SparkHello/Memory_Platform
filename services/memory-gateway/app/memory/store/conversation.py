"""Conversation branch and recent-context helpers."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from typing import Any

from app.memory.models import (
    ConversationBranchNode,
    RecentContextSummary,
    RecentContextTurn,
    new_memory_id,
    utc_now_iso,
)
from app.memory.store.constants import _CONVERSATION_BRANCH_NODE_RETENTION_LIMIT
from app.memory.store.helpers import (
    _ConnectableStore,
    _json_string_list,
    _row_to_conversation_branch_node,
    _row_to_recent_context_summary,
)

def get_recent_context_summary(
    store: _ConnectableStore,
    *,
    user_id: str,
    conversation_id: str | None = None,
) -> RecentContextSummary | None:
    if conversation_id is not None:
        return get_recent_context_summary_for_conversation(
            store,
            user_id=user_id,
            conversation_id=conversation_id,
        )
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM recent_context_summaries
            WHERE user_id = ? AND archived = 0
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return _row_to_recent_context_summary(row) if row else None

def get_recent_context_summary_for_conversation(
    store: _ConnectableStore,
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
    with store._connect() as connection:
        row = connection.execute(query, params).fetchone()
    return _row_to_recent_context_summary(row) if row else None

def list_recent_context_summaries(
    store: _ConnectableStore,
    *,
    user_id: str,
    limit: int | None = 20,
) -> list[RecentContextSummary]:
    bounded_limit = None if limit is None else max(1, int(limit))
    with store._connect() as connection:
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
    return [_row_to_recent_context_summary(row) for row in rows]

def upsert_recent_context_summary(
    store: _ConnectableStore,
    *,
    user_id: str,
    conversation_id: str | None,
    summary: str,
) -> RecentContextSummary:
    return upsert_recent_context_state(
        store,
        user_id=user_id,
        conversation_id=conversation_id,
        summary=summary,
        compressed_summary=summary,
        recent_turns=[],
        turn_count=0,
    )

def upsert_recent_context_state(
    store: _ConnectableStore,
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
    existing = get_recent_context_summary_for_conversation(
        store,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    now = utc_now_iso()
    if existing:
        with store._connect() as connection:
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
        updated = get_recent_context_summary_for_conversation(
            store,
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
        with store._connect() as connection:
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
        return upsert_recent_context_state(
        store,
            user_id=user_id,
            conversation_id=conversation_id,
            summary=normalized_summary,
            compressed_summary=normalized_compressed_summary,
            recent_turns=recent_turns,
            turn_count=max(0, turn_count),
        )

def get_conversation_branch_node(
    store: _ConnectableStore,
    *,
    user_id: str,
    history_fingerprint: str,
) -> ConversationBranchNode | None:
    normalized = history_fingerprint.strip()
    if not normalized:
        return None
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM conversation_branch_nodes
            WHERE user_id = ? AND history_fingerprint = ? AND archived = 0
            LIMIT 1
            """,
            (user_id, normalized),
        ).fetchone()
    return _row_to_conversation_branch_node(row) if row else None

def list_conversation_branch_nodes(
    store: _ConnectableStore,
    *,
    user_id: str,
    limit: int = 5000,
    archived: bool = False,
) -> list[ConversationBranchNode]:
    with store._connect() as connection:
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
    return [_row_to_conversation_branch_node(row) for row in rows]

def count_conversation_branch_nodes(
    store: _ConnectableStore,
    *,
    user_id: str,
    archived: bool = False,
) -> int:
    with store._connect() as connection:
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
    store: _ConnectableStore,
    *,
    node_id: str,
    user_id: str,
) -> int:
    """Soft-delete one branch node and every active descendant."""

    now = utc_now_iso()
    with store._connect() as connection:
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
    store: _ConnectableStore,
    *,
    node_id: str,
    user_id: str,
) -> int:
    """Restore one archived branch node and every archived descendant."""

    now = utc_now_iso()
    with store._connect() as connection:
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
    store: _ConnectableStore,
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
    with store._connect() as connection:
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
    return _row_to_conversation_branch_node(row)

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
