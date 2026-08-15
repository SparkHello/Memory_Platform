"""Core memory section persistence helpers."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
from typing import Any

from app.memory.models import (
    CoreMemorySection,
    CoreMemorySectionHistory,
    CoreMemorySectionName,
    MemoryAction,
    new_memory_id,
    utc_now_iso,
)
from app.memory.store.errors import RevisionConflictError
from app.memory.store.helpers import (
    ConnectionProvider,
    _json_string_list,
    _ordered_unique,
    _row_to_core_memory_section,
    _row_to_core_memory_section_history,
)

def list_core_memory_sections(
    store: ConnectionProvider,
    *,
    user_id: str,
) -> list[CoreMemorySection]:
    with store._connect() as connection:
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
    return [_row_to_core_memory_section(row) for row in rows]

def get_core_memory_section(
    store: ConnectionProvider,
    *,
    user_id: str,
    section: CoreMemorySectionName,
) -> CoreMemorySection | None:
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM core_memory_sections
            WHERE user_id = ? AND section = ? AND archived = 0
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (user_id, section),
        ).fetchone()
    return _row_to_core_memory_section(row) if row else None

def upsert_core_memory_section(
    store: ConnectionProvider,
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
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM core_memory_sections
            WHERE user_id = ? AND section = ? AND archived = 0
            LIMIT 1
            """,
            (user_id, section),
        ).fetchone()
        existing = _row_to_core_memory_section(row) if row else None
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
        _create_core_memory_section_history(
            store,
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
        return "update", _row_to_core_memory_section(updated_row)

def archive_core_memory_section(
    store: ConnectionProvider,
    *,
    user_id: str,
    section: CoreMemorySectionName,
    expected_revision: int | None = None,
) -> bool:
    now = utc_now_iso()
    with store._connect() as connection:
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
    store: ConnectionProvider,
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
    with store._connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [_row_to_core_memory_section_history(row) for row in rows]

def _create_core_memory_section_history(
    store: ConnectionProvider,
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
    with store._connect() as owned_connection:
        owned_connection.execute(query, params)

def _ensure_core_memory_sections_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(core_memory_sections)").fetchall()
    }
    if "version" not in columns:
        connection.execute(
            "ALTER TABLE core_memory_sections ADD COLUMN version INTEGER DEFAULT 1"
        )

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
