"""Export / import / restore helpers for MemoryStore."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
from typing import Any

import hashlib

from pydantic import ValidationError

from app.memory.models import (
    ConversationBranchNode,
    DecisionLog,
    MemoryRecord,
    MemorySpace,
    RecentContextSummary,
    RecentContextTurn,
    normalize_memory_type,
    normalize_optional_text,
    new_memory_id,
    utc_now_iso,
)
from app.memory.classification import (
    normalize_classification_name,
    normalize_classification_names,
)
from app.memory.store.constants import _CONVERSATION_BRANCH_NODE_RETENTION_LIMIT
from app.memory.store.helpers import (
    ConnectionProvider,
    _bounded_float,
    _coerce_float,
    _coerce_float_or_none,
    _coerce_int,
    _coerce_string_list,
    _insert_memory_row,
    _json_string_list,
    _ordered_unique,
    _row_to_conversation_branch_node,
    _row_to_core_memory_section,
    _row_to_core_memory_section_history,
    _row_to_memory,
    _row_to_memory_space,
    _row_to_recent_context_summary,
    _rows_to_memories_on_connection,
    _sensitivity_with_floor,
    _space_ids_for_memory_ids_on_connection,
)
from app.memory.store.spaces import (
    _filter_existing_space_ids,
    _replace_memory_space_links,
)
from app.memory.store.temporal import _rebuild_temporal_key
from app.memory.utils import _parse_iso_datetime

def list_all_memories_for_export(
    store: ConnectionProvider,
    *,
    user_id: str,
    archived: bool,
    page_size: int = 500,
) -> list[MemoryRecord]:
    """Read every user row in bounded pages for a complete backup export."""
    bounded_page_size = max(1, min(int(page_size), 1000))
    with store._connect() as connection:
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
        return _rows_to_memories_on_connection(
            connection=connection,
            rows=rows,
        )

def read_memory_export_snapshot(
    store: ConnectionProvider,
    *,
    user_id: str,
    include_deleted: bool = True,
    page_size: int = 500,
) -> dict[str, list[object]]:
    """Read every export partition from one SQLite snapshot."""
    bounded_page_size = max(1, min(int(page_size), 1000))
    with store._connect() as connection:
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

        memories = _rows_to_memories_on_connection(
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
            "memory_spaces": [_row_to_memory_space(row) for row in space_rows],
            "memories": [memory for memory in memories if not memory.archived],
            "deleted_memories": [memory for memory in memories if memory.archived],
            "core_memory_sections": [
                _row_to_core_memory_section(row) for row in core_rows
            ],
            "core_memory_section_history": [
                _row_to_core_memory_section_history(row)
                for row in core_history_rows
            ],
            "recent_context_summaries": [
                _row_to_recent_context_summary(row)
                for row in recent_context_rows
            ],
            "conversation_branch_nodes": [
                _row_to_conversation_branch_node(row) for row in branch_rows
            ],
            "decision_logs": [DecisionLog(**dict(row)) for row in decision_rows],
        }

def read_memory_selection_export_snapshot(
    store: ConnectionProvider,
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
    with store._connect() as connection:
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
        memories = _rows_to_memories_on_connection(
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
                    str(row["id"]): _row_to_memory_space(row)
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

def prepare_memory_space_import(
    store: ConnectionProvider,
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
    try:
        sort_order = int(data.get("sort_order") or 0)
    except (TypeError, ValueError):
        sort_order = 0
    color_raw = data.get("color")
    description_raw = data.get("description")
    return {
        "source_id": source_id,
        "requested_id": source_id or new_memory_id(),
        "name": display_name,
        "normalized_name": display_name.casefold(),
        "created_at": str(data.get("created_at") or now),
        "archived": 1 if data.get("archived") else 0,
        "color": str(color_raw).strip() if color_raw else None,
        "description": str(description_raw).strip() if description_raw else None,
        "sort_order": max(0, min(9999, sort_order)),
    }

def import_memory_space(
    store: ConnectionProvider,
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
    with store._connect() as connection:
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
            return "updated", _row_to_memory_space(updated), old_id or existing_name["id"]

        existing_same_id = connection.execute(
            "SELECT * FROM memory_spaces WHERE id = ? AND user_id = ?",
            (space_id, user_id),
        ).fetchone()
        if existing_same_id is not None:
            if not overwrite:
                return "skipped", _row_to_memory_space(existing_same_id), old_id or space_id
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
            return "updated", _row_to_memory_space(updated), old_id or space_id

        try:
            sort_order = int(data.get("sort_order") or 0)
        except (TypeError, ValueError):
            sort_order = 0
        color = data.get("color")
        description = data.get("description")
        space = MemorySpace(
            id=space_id,
            user_id=user_id,
            name=display_name,
            normalized_name=normalized_name,
            created_at=str(data.get("created_at") or now),
            updated_at=now,
            archived=1 if data.get("archived") else 0,
            color=str(color).strip() if color else None,
            description=str(description).strip() if description else None,
            sort_order=max(0, min(9999, sort_order)),
        )
        connection.execute(
            """
            INSERT INTO memory_spaces (
                id, user_id, name, normalized_name, created_at, updated_at, archived,
                color, description, sort_order
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                space.id,
                space.user_id,
                space.name,
                space.normalized_name,
                space.created_at,
                space.updated_at,
                space.archived,
                space.color,
                space.description,
                space.sort_order,
            ),
        )
    return "created", space, old_id or space_id

def plan_memory_import_ids(
    store: ConnectionProvider,
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
    with store._connect() as connection:
        return _plan_memory_import_ids_on_connection(
            connection=connection,
            user_id=user_id,
            source_ids=ordered_ids,
            rebind_all=rebind_all,
        )

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
    store: ConnectionProvider,
    *,
    user_id: str,
    memory_ids: list[str],
) -> set[str]:
    """Return IDs that currently belong to the user, including archived rows."""
    ordered_ids = _ordered_unique(
        [str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()]
    )
    with store._connect() as connection:
        return _filter_existing_memory_ids_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=ordered_ids,
        )

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
    store: ConnectionProvider,
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
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _prune_dangling_memory_references_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=target_ids,
        )

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
    store: ConnectionProvider,
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
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")

        # Finish every database-dependent mapping before the first write.
        space_plans, space_id_map = _plan_memory_space_imports_on_connection(
            connection=connection,
            user_id=user_id,
            prepared_spaces=prepared_spaces,
            overwrite=overwrite,
        )
        memory_id_map = _plan_memory_import_ids_on_connection(
            connection=connection,
            user_id=user_id,
            source_ids=source_memory_ids,
            rebind_all=bool(exported_user_id and exported_user_id != user_id),
        )
        preserve_existing_references = (
            not exported_user_id or exported_user_id == user_id
        )
        allowed_existing_ids = (
            _filter_existing_memory_ids_on_connection(
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
            _apply_memory_space_import_plan_on_connection(
                connection=connection,
                user_id=user_id,
                plan=plan,
            )

        memory_results: list[tuple[str, MemoryRecord | None]] = []
        imported_memory_ids: list[str] = []
        for memory in mapped_memories:
            action, persisted = _import_prepared_memory_record_on_connection(
                connection=connection,
                user_id=user_id,
                memory=memory,
                overwrite=overwrite,
                rebind_on_conflict=False,
            )
            memory_results.append((action, persisted))
            if persisted is not None and action in {"created", "updated"}:
                imported_memory_ids.append(persisted.id)

        dangling_removed = _prune_dangling_memory_references_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=imported_memory_ids,
        )
        final_existing_ids = _filter_existing_memory_ids_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=[*memory_id_map.values(), *referenced_source_ids],
        )

        recent_context_actions = [
            _restore_recent_context_on_connection(
                connection=connection,
                user_id=user_id,
                prepared=prepared,
                overwrite=overwrite,
            )
            for prepared in recent_contexts
        ]
        branch_node_actions = [
            _restore_branch_node_on_connection(
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
        str(row["id"]): _row_to_memory_space(row)
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
                        "color": prepared.get("color"),
                        "description": prepared.get("description"),
                        "sort_order": int(prepared.get("sort_order") or 0),
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
                    color=prepared.get("color"),
                    description=prepared.get("description"),
                    sort_order=int(prepared.get("sort_order") or 0),
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
                id, user_id, name, normalized_name, created_at, updated_at, archived,
                color, description, sort_order
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                space.id,
                space.user_id,
                space.name,
                space.normalized_name,
                space.created_at,
                space.updated_at,
                space.archived,
                space.color,
                space.description,
                space.sort_order,
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
            SET name = ?, normalized_name = ?, updated_at = ?, archived = ?,
                color = ?, description = ?, sort_order = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                space.name,
                space.normalized_name,
                space.updated_at,
                space.archived,
                space.color,
                space.description,
                space.sort_order,
                space.id,
                user_id,
            ),
        )

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
    store: ConnectionProvider,
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
    store: ConnectionProvider,
    *,
    user_id: str,
    data: dict,
    overwrite: bool = False,
    archived: int | None = None,
    space_id_map: dict[str, str] | None = None,
    rebind_on_conflict: bool = True,
) -> tuple[str, MemoryRecord | None]:
    memory = prepare_memory_import_record(
        store,
        user_id=user_id,
        data=data,
        archived=archived,
        space_id_map=space_id_map,
    )
    if memory is None:
        return "invalid", None

    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _import_prepared_memory_record_on_connection(
            connection=connection,
            user_id=user_id,
            memory=memory,
            overwrite=overwrite,
            rebind_on_conflict=rebind_on_conflict,
        )

def _import_prepared_memory_record_on_connection(
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
    memory.space_ids = _filter_existing_space_ids(
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
        _row_to_memory(row, space_ids=[])
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
        _replace_memory_space_links(
            connection=connection,
            user_id=user_id,
            memory_id=memory.id,
            space_ids=memory.space_ids,
            created_at=now,
        )
        action = "updated"
    else:
        _insert_memory_row(connection=connection, memory=memory)
        _replace_memory_space_links(
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
        _rebuild_temporal_key(
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
    persisted_space_ids = _space_ids_for_memory_ids_on_connection(
        connection=connection,
        user_id=user_id,
        memory_ids=[memory.id],
    ).get(memory.id, [])
    persisted = _row_to_memory(
        persisted_row,
        space_ids=persisted_space_ids,
    )
    return action, persisted

