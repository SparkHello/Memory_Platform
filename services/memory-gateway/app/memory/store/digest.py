"""Memory digest helpers."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
from typing import Any

from app.memory.models import MemoryRecord, MemoryType, new_memory_id, utc_now_iso
from app.memory.store.constants import _SENSITIVITY_RANK
from app.memory.store.helpers import (
    _ConnectableStore,
    _average_float,
    _insert_memory_row,
    _json_string_list,
    _ordered_unique,
    _rows_to_memories,
    _sensitivity_with_floor,
)
from app.memory.utils import _parse_iso_datetime

def list_undigested_memories(
    store: _ConnectableStore, *, user_id: str, limit: int = 10, include_sensitive: bool = False
) -> list[MemoryRecord]:
    """返回近期未消化的记忆，供 digest_memories 使用。"""
    with store._connect() as connection:
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
    memories = _rows_to_memories(store, rows)
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
    store: _ConnectableStore,
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
    with store._connect() as connection:
        rows = _validated_digest_source_rows(
            connection=connection,
            user_id=user_id,
            source_ids=source_ids,
            include_sensitive=include_sensitive,
        )
    return _rows_to_memories(store, rows)

def apply_memory_digest(
    store: _ConnectableStore,
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
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        source_rows = _validated_digest_source_rows(
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
            _insert_memory_row(connection=connection, memory=memory)
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

