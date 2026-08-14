"""Temporal chain and time-ripple helpers for MemoryStore."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3
from typing import Any

from app.memory.models import MemoryRecord, new_memory_id, normalize_optional_text, utc_now_iso
from app.memory.store.constants import _TIME_RIPPLE_MAX_CANDIDATES
from app.memory.store.decision_logs import _insert_decision_log
from app.memory.store.helpers import (
    _ConnectableStore,
    _bounded_float,
    _casefold_set,
    _coerce_int,
    _insert_memory_row,
    _json_string_list,
    _row_to_memory,
    _space_ids_for_memory_ids_on_connection,
    _temporal_snapshot,
    _time_ripple_anchor,
    _time_ripple_profiles,
)
from app.memory.store.spaces import _replace_memory_space_links
from app.memory.utils import _parse_iso_datetime

def restore_temporal_memory(
    store: _ConnectableStore,
    *,
    memory_id: str,
    user_id: str,
) -> MemoryRecord | None:
    now = utc_now_iso()
    with store._connect() as connection:
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

        space_ids = _space_ids_for_memory_ids_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=[memory_id],
        ).get(memory_id, [])
        source = _row_to_memory(row, space_ids=space_ids)
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
        _insert_memory_row(connection=connection, memory=restored)
        _replace_memory_space_links(
            connection=connection,
            user_id=user_id,
            memory_id=restored.id,
            space_ids=space_ids,
            created_at=now,
        )
        _apply_temporal_invalidation(
            connection=connection,
            user_id=user_id,
            new_memory=restored,
        )

        _insert_decision_log(
            connection=connection,
            user_id=user_id,
            conversation_id=None,
            candidate_json=json.dumps(
                {
                    "source": "temporal_restore",
                    "source_memory_id": memory_id,
                    "restored_memory_id": restored.id,
                    "before": _temporal_snapshot(row),
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
    return store.get_memory(memory_id=restored.id, user_id=user_id)

def get_next_temporal_boundary(
    store: _ConnectableStore,
    *,
    user_id: str,
    after: datetime,
) -> datetime | None:
    """Return the next validity boundary that can change recall eligibility."""
    current = after if after.tzinfo is not None else after.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    with store._connect() as connection:
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

def _apply_time_ripple(
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

def _rebuild_temporal_key(
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
    memories = [_row_to_memory(row, space_ids=[]) for row in rows]
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
        changed += _rebuild_temporal_key(
            connection=connection,
            user_id=user_id,
            temporal_subject=row["temporal_subject"],
            temporal_predicate=row["temporal_predicate"],
        )
    return changed

def _detach_temporal_position(
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

    _insert_decision_log(
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
                "before": [_temporal_snapshot(row) for row in rows],
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
