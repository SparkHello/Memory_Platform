"""Archived memory purge entrypoints (API-facing)."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from typing import Any

from app.memory.models import (
    CoreMemorySection,
    DecisionLog,
    MemoryRecord,
    new_memory_id,
    utc_now_iso,
)
from app.memory.purge_preview import purge_memory_ids_digest
from app.memory.store.helpers import (
    _ConnectableStore,
    _json_string_list,
    _merge_core_section_audit_summaries,
    _ordered_unique,
    _row_to_core_memory_section,
    _row_to_memory,
)
from app.memory.store.purge_ops import (
    PurgePreviewConflictError,
    _apply_batch_purge_snapshot,
    _build_batch_purge_snapshot,
    _derived_memory_dependency_closure,
    _insert_batch_purge_audit,
    _scrub_purged_memory_artifacts,
)

def preview_archived_memory_purge(
    store: _ConnectableStore,
    *,
    memory_ids: list[str],
    user_id: str,
) -> dict[str, object]:
    """Build a repeatable, user-scoped purge plan without writing data."""

    requested_ids = sorted(_ordered_unique(memory_ids))
    with store._connect() as connection:
        connection.execute("BEGIN")
        snapshot = _build_batch_purge_snapshot(
            connection,
            user_id=user_id,
            requested_memory_ids=requested_ids,
        )
    return {
        "requested_memory_ids": snapshot.requested_memory_ids,
        "purge_memory_ids": snapshot.purge_memory_ids,
        "dependent_memory_ids": snapshot.dependent_memory_ids,
        "affected_core_memory_sections": snapshot.affected_core_memory_sections,
        "fingerprint": snapshot.fingerprint,
        "effects": snapshot.effects,
    }

def commit_archived_memory_purge(
    store: _ConnectableStore,
    *,
    memory_ids: list[str],
    user_id: str,
    expected_purge_memory_ids_digest: str,
    expected_purge_memory_count: int,
    expected_fingerprint: str,
    call_source: str = "rest_api",
) -> tuple[dict[str, object], DecisionLog]:
    """Revalidate and apply one batch purge in one immediate transaction."""

    requested_ids = sorted(_ordered_unique(memory_ids))
    purged_at = utc_now_iso()
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        snapshot = _build_batch_purge_snapshot(
            connection,
            user_id=user_id,
            requested_memory_ids=requested_ids,
        )
        if (
            len(snapshot.purge_memory_ids) != expected_purge_memory_count
            or purge_memory_ids_digest(snapshot.purge_memory_ids)
            != expected_purge_memory_ids_digest
            or snapshot.fingerprint != expected_fingerprint
        ):
            raise PurgePreviewConflictError(
                code="purge_preview_stale",
                message="永久删除预览已过期：所选记忆、依赖闭包或 Core 影响已变化。",
            )
        effects = _apply_batch_purge_snapshot(
            connection,
            user_id=user_id,
            snapshot=snapshot,
            purged_at=purged_at,
        )
        log = _insert_batch_purge_audit(
            connection,
            user_id=user_id,
            snapshot=snapshot,
            effects=effects,
            fingerprint=expected_fingerprint,
            purged_at=purged_at,
            call_source=call_source,
        )
    return (
        {
            "requested_memory_ids": snapshot.requested_memory_ids,
            "purged_memory_ids": snapshot.purge_memory_ids,
            "dependent_memory_ids": snapshot.dependent_memory_ids,
            "affected_core_memory_sections": snapshot.affected_core_memory_sections,
            "fingerprint": snapshot.fingerprint,
            "effects": effects,
        },
        log,
    )

def purge_archived_memory(
    store: _ConnectableStore,
    *,
    memory_id: str,
    user_id: str,
    affected_core_sections: list[dict] | None = None,
    call_source: str = "rest_api",
) -> tuple[MemoryRecord, DecisionLog] | None:
    purged_at = utc_now_iso()
    with store._connect() as connection:
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
        memory = _row_to_memory(
            row,
            space_ids=[str(space_row["space_id"]) for space_row in space_rows],
        )
        scrubbed_artifacts = _scrub_purged_memory_artifacts(
            connection,
            memory=memory,
            purged_at=purged_at,
        )
        internally_affected_core = scrubbed_artifacts.pop(
            "affected_core_sections",
            [],
        )
        affected_core_audit = _merge_core_section_audit_summaries(
            affected_core_sections or [],
            internally_affected_core,
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
                    "affected_core_sections": affected_core_audit,
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

def list_purge_affected_core_sections(
    store: _ConnectableStore,
    *,
    memory_id: str,
    user_id: str,
) -> list[CoreMemorySection]:
    """Preview active core sections affected by a user-scoped purge closure."""
    with store._connect() as connection:
        target = connection.execute(
            """
            SELECT id FROM memories
            WHERE id = ? AND user_id = ? AND archived = 1
            """,
            (memory_id, user_id),
        ).fetchone()
        if target is None:
            return []
        affected_ids, _ = _derived_memory_dependency_closure(
            connection,
            user_id=user_id,
            root_memory_id=memory_id,
        )
        rows = connection.execute(
            """
            SELECT * FROM core_memory_sections
            WHERE user_id = ? AND archived = 0
            """,
            (user_id,),
        ).fetchall()
    return [
        _row_to_core_memory_section(row)
        for row in rows
        if affected_ids.intersection(
            _json_string_list(row["evidence_memory_ids_json"])
        )
    ]

