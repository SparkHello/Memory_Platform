"""Memory merge helpers for MemoryStore."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
from typing import Any

from app.memory.classification import normalize_classification_names
from app.memory.models import (
    MemoryAction,
    MemoryMergeResult,
    MemoryRecord,
    normalize_iso_text,
    normalize_optional_text,
    new_memory_id,
    utc_now_iso,
)
from app.memory.store.helpers import (
    ConnectionProvider,
    _average_float,
    _casefold_set,
    _earliest_datetime_text,
    _join_memory_contents,
    _merged_sensitivity,
    _merged_stability,
    _merged_type,
    _ordered_unique,
    _row_to_memory,
    _sensitivity_with_floor,
    _shared_value,
)
from app.memory.store.spaces import (
    _replace_memory_space_links,
    _validate_space_ids,
)
from app.memory.utils import _parse_iso_datetime

def merge_memories(
    store: ConnectionProvider,
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

    with store._connect() as connection:
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
            _row_to_memory(
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
        _validate_space_ids(
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
        _replace_memory_space_links(
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
        updated = _row_to_memory(updated_row, space_ids=space_ids)

    return MemoryMergeResult(
        action="update",
        memory=updated,
        merged_memory_ids=ordered_ids,
        archived_memory_ids=archived_ids,
        reason="已合并记忆并保留 evidence ids",
    )

