"""Batch purge planning and artifact scrubbing helpers."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any

from app.memory.models import DecisionLog, MemoryRecord
from app.memory.purge_preview import purge_memory_ids_digest
from app.memory.store.decision_logs import (
    _decision_log_references_memory_ids,
    _insert_decision_log,
)
from app.memory.store.helpers import (
    _core_section_audit_summaries,
    _json_like_safe,
    _json_string_list,
    _like_escape,
    _ordered_unique,
)


class PurgePreviewConflictError(RuntimeError):
    """A batch purge preview cannot be safely created or committed."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        missing_memory_ids: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.missing_memory_ids = missing_memory_ids or []


@dataclass
class _BatchPurgeSnapshot:
    requested_memory_ids: list[str]
    purge_memory_ids: list[str]
    dependent_memory_ids: list[str]
    affected_core_memory_sections: list[dict[str, object]]
    fingerprint: str
    effects: dict[str, int]
    affected_memory_rows: list[sqlite3.Row]
    affected_core_rows: list[sqlite3.Row]
    affected_core_history_rows: list[sqlite3.Row]
    affected_decision_log_rows: list[sqlite3.Row]
    temporal_repairs: list[tuple[str, str | None, str | None]]


def _build_batch_purge_snapshot(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    requested_memory_ids: list[str],
) -> _BatchPurgeSnapshot:
    requested_ids = sorted(_ordered_unique(requested_memory_ids))
    if not requested_ids:
        raise PurgePreviewConflictError(
            code="purge_targets_missing",
            message="至少选择一条回收站记忆。",
        )

    all_memory_rows = connection.execute(
        "SELECT * FROM memories WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    rows_by_id = {str(row["id"]): row for row in all_memory_rows}
    missing_ids = [
        memory_id
        for memory_id in requested_ids
        if memory_id not in rows_by_id or int(rows_by_id[memory_id]["archived"] or 0) != 1
    ]
    if missing_ids:
        raise PurgePreviewConflictError(
            code="purge_targets_missing",
            message="部分所选记忆已不存在、不属于当前用户或已离开回收站。",
            missing_memory_ids=missing_ids,
        )

    dependents_by_evidence_id: dict[str, list[str]] = {}
    for row in all_memory_rows:
        memory_id = str(row["id"])
        for evidence_id in set(_json_string_list(row["evidence_memory_ids_json"])):
            dependents_by_evidence_id.setdefault(evidence_id, []).append(memory_id)

    affected_ids = set(requested_ids)
    frontier = deque(requested_ids)
    while frontier:
        evidence_id = frontier.popleft()
        for dependent_id in dependents_by_evidence_id.get(evidence_id, []):
            if dependent_id in affected_ids:
                continue
            affected_ids.add(dependent_id)
            frontier.append(dependent_id)

    purge_ids = sorted(affected_ids)
    dependent_ids = sorted(affected_ids.difference(requested_ids))
    affected_memory_rows = [rows_by_id[memory_id] for memory_id in purge_ids]
    affected_core_rows = [
        row
        for row in connection.execute(
            "SELECT * FROM core_memory_sections WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        if affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"]))
    ]
    affected_core_rows.sort(key=lambda row: (str(row["section"]), str(row["id"])))
    affected_core_history_rows = [
        row
        for row in connection.execute(
            "SELECT * FROM core_memory_section_history WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        if affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"]))
    ]
    affected_core_history_rows.sort(key=lambda row: str(row["id"]))

    affected_conversation_ids = {
        str(row["source_conversation_id"])
        for row in affected_memory_rows
        if row["source_conversation_id"]
    }
    sensitive_fragments = {
        fragment
        for row in affected_memory_rows
        for fragment in (
            str(row["id"]),
            str(row["content"] or ""),
            str(row["source_message"] or ""),
        )
        if fragment
    }
    affected_decision_log_rows = _purge_relevant_decision_log_rows(
        connection,
        user_id=user_id,
        affected_ids=affected_ids,
        affected_conversation_ids=affected_conversation_ids,
        sensitive_fragments=sensitive_fragments,
    )
    temporal_repairs = _purged_temporal_reference_plan(
        all_memory_rows,
        affected_ids=affected_ids,
    )
    affected_space_link_rows = [
        row
        for row in connection.execute(
            """
            SELECT memory_id, space_id, created_at
            FROM memory_space_links
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
        if str(row["memory_id"]) in affected_ids
    ]
    affected_space_link_rows.sort(
        key=lambda row: (str(row["memory_id"]), str(row["space_id"]))
    )

    effects = {
        "requested_memories_deleted": len(requested_ids),
        "dependent_memories_deleted": len(dependent_ids),
        "memories_deleted": len(purge_ids),
        "space_links_deleted": len(affected_space_link_rows),
        "temporal_references_relinked": len(temporal_repairs),
        "core_sections_scrubbed": len(affected_core_rows),
        "core_history_scrubbed": len(affected_core_history_rows),
        "decision_logs_scrubbed": len(affected_decision_log_rows),
    }
    affected_core_sections = [
        {
            "id": str(row["id"]),
            "section": str(row["section"]),
            "version": int(row["version"] or 1),
            "active": int(row["archived"] or 0) == 0,
        }
        for row in affected_core_rows
    ]
    fingerprint_payload = {
        "version": 1,
        "user_id": user_id,
        "requested_memory_ids": requested_ids,
        "memories": [_purge_memory_fingerprint_row(row) for row in affected_memory_rows],
        "space_links": [
            [str(row["memory_id"]), str(row["space_id"]), str(row["created_at"] or "")]
            for row in affected_space_link_rows
        ],
        "core_sections": [_purge_core_fingerprint_row(row) for row in affected_core_rows],
        "core_history": [
            _purge_core_history_fingerprint_row(row)
            for row in affected_core_history_rows
        ],
        "decision_logs": [
            _purge_decision_log_fingerprint_row(row)
            for row in affected_decision_log_rows
        ],
        "temporal_repairs": temporal_repairs,
        "effects": effects,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return _BatchPurgeSnapshot(
        requested_memory_ids=requested_ids,
        purge_memory_ids=purge_ids,
        dependent_memory_ids=dependent_ids,
        affected_core_memory_sections=affected_core_sections,
        fingerprint=fingerprint,
        effects=effects,
        affected_memory_rows=affected_memory_rows,
        affected_core_rows=affected_core_rows,
        affected_core_history_rows=affected_core_history_rows,
        affected_decision_log_rows=affected_decision_log_rows,
        temporal_repairs=temporal_repairs,
    )


def _purge_relevant_decision_log_rows(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    affected_ids: set[str],
    affected_conversation_ids: set[str],
    sensitive_fragments: set[str],
) -> list[sqlite3.Row]:
    prefilter = _decision_log_scrub_prefilter(
        affected_conversation_ids=affected_conversation_ids,
        fragments=sensitive_fragments,
    )
    if prefilter is None:
        rows = connection.execute(
            """
            SELECT id, conversation_id, candidate_json, reason, decision, created_at
            FROM memory_decision_logs
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            f"""
            SELECT id, conversation_id, candidate_json, reason, decision, created_at
            FROM memory_decision_logs
            WHERE user_id = ? AND ({prefilter[0]})
            """,
            (user_id, *prefilter[1]),
        ).fetchall()
    relevant = [
        row
        for row in rows
        if (
            _decision_log_references_memory_ids(str(row["candidate_json"] or ""), affected_ids)
            or (
                row["conversation_id"]
                and str(row["conversation_id"]) in affected_conversation_ids
            )
            or _decision_log_contains_fragments(
                candidate_json=str(row["candidate_json"] or ""),
                reason=str(row["reason"] or ""),
                fragments=sensitive_fragments,
            )
        )
    ]
    relevant.sort(key=lambda row: str(row["id"]))
    return relevant


def _purged_temporal_reference_plan(
    rows: list[sqlite3.Row],
    *,
    affected_ids: set[str],
) -> list[tuple[str, str | None, str | None]]:
    links = {
        str(row["id"]): (
            str(row["supersedes"]) if row["supersedes"] else None,
            str(row["superseded_by"]) if row["superseded_by"] else None,
        )
        for row in rows
    }

    def follow(start_id: str | None, *, index: int, survivor_id: str) -> str | None:
        current = start_id
        visited: set[str] = set()
        while current in affected_ids:
            if current in visited:
                return None
            visited.add(current)
            current_links = links.get(current)
            if current_links is None:
                return None
            current = current_links[index]
        if current is None or current == survivor_id or current not in links:
            return None
        return current

    repairs: list[tuple[str, str | None, str | None]] = []
    for row in rows:
        memory_id = str(row["id"])
        if memory_id in affected_ids:
            continue
        previous_id = str(row["supersedes"]) if row["supersedes"] else None
        next_id = str(row["superseded_by"]) if row["superseded_by"] else None
        repaired_previous = (
            follow(previous_id, index=0, survivor_id=memory_id)
            if previous_id in affected_ids
            else previous_id
        )
        repaired_next = (
            follow(next_id, index=1, survivor_id=memory_id)
            if next_id in affected_ids
            else next_id
        )
        if repaired_previous != previous_id or repaired_next != next_id:
            repairs.append((memory_id, repaired_previous, repaired_next))
    repairs.sort(key=lambda item: item[0])
    return repairs


def _purge_memory_fingerprint_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "archived": int(row["archived"] or 0),
        "archived_at": row["archived_at"],
        "revision": int(row["revision"] or 1),
        "updated_at": row["updated_at"],
        "origin": row["origin"],
        "type": row["type"],
        "sensitivity": row["sensitivity"],
        "evidence_memory_ids": sorted(_json_string_list(row["evidence_memory_ids_json"])),
        "source_conversation_id": row["source_conversation_id"],
        "supersedes": row["supersedes"],
        "superseded_by": row["superseded_by"],
        "content_sha256": _purge_text_sha256(row["content"]),
        "source_message_sha256": _purge_text_sha256(row["source_message"]),
    }


def _purge_core_fingerprint_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "section": str(row["section"]),
        "version": int(row["version"] or 1),
        "revision": int(row["revision"] or 1),
        "archived": int(row["archived"] or 0),
        "updated_at": row["updated_at"],
        "evidence_memory_ids": sorted(_json_string_list(row["evidence_memory_ids_json"])),
        "content_sha256": _purge_text_sha256(row["content"]),
    }


def _purge_core_history_fingerprint_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "core_memory_section_id": row["core_memory_section_id"],
        "section": row["section"],
        "version": row["version"],
        "updated_at": row["updated_at"],
        "replaced_at": row["replaced_at"],
        "evidence_memory_ids": sorted(_json_string_list(row["evidence_memory_ids_json"])),
        "content_sha256": _purge_text_sha256(row["content"]),
    }


def _purge_decision_log_fingerprint_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "conversation_id": row["conversation_id"],
        "decision": row["decision"],
        "created_at": row["created_at"],
        "candidate_sha256": _purge_text_sha256(row["candidate_json"]),
        "reason_sha256": _purge_text_sha256(row["reason"]),
    }


def _purge_text_sha256(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _apply_batch_purge_snapshot(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    snapshot: _BatchPurgeSnapshot,
    purged_at: str,
) -> dict[str, int]:
    temporal_references_relinked = 0
    for memory_id, supersedes, superseded_by in snapshot.temporal_repairs:
        cursor = connection.execute(
            """
            UPDATE memories
            SET supersedes = ?, superseded_by = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (supersedes, superseded_by, purged_at, memory_id, user_id),
        )
        temporal_references_relinked += max(0, int(cursor.rowcount))

    core_sections_scrubbed = 0
    for row in snapshot.affected_core_rows:
        cursor = connection.execute(
            """
            UPDATE core_memory_sections
            SET content = ?, evidence_memory_ids_json = '[]', archived = 1, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            ("[redacted: purged evidence]", purged_at, row["id"], user_id),
        )
        core_sections_scrubbed += max(0, int(cursor.rowcount))

    core_history_scrubbed = 0
    for row in snapshot.affected_core_history_rows:
        cursor = connection.execute(
            """
            UPDATE core_memory_section_history
            SET content = ?, evidence_memory_ids_json = '[]', updated_at = ?, replaced_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                "[redacted: purged evidence]",
                purged_at,
                purged_at,
                row["id"],
                user_id,
            ),
        )
        core_history_scrubbed += max(0, int(cursor.rowcount))

    replacement = json.dumps(
        {
            "source": "purged_memory_history",
            "memory_ids": snapshot.requested_memory_ids,
            "redacted": True,
            "purged_at": purged_at,
        },
        ensure_ascii=False,
    )
    decision_logs_scrubbed = 0
    for row in snapshot.affected_decision_log_rows:
        cursor = connection.execute(
            """
            UPDATE memory_decision_logs
            SET candidate_json = ?, reason = ?
            WHERE id = ? AND user_id = ?
            """,
            (replacement, "历史记录因永久删除已脱敏", row["id"], user_id),
        )
        decision_logs_scrubbed += max(0, int(cursor.rowcount))

    space_links_deleted = 0
    memories_deleted = 0
    for offset in range(0, len(snapshot.purge_memory_ids), 500):
        batch = snapshot.purge_memory_ids[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        link_cursor = connection.execute(
            f"""
            DELETE FROM memory_space_links
            WHERE user_id = ? AND memory_id IN ({placeholders})
            """,
            (user_id, *batch),
        )
        space_links_deleted += max(0, int(link_cursor.rowcount))
        memory_cursor = connection.execute(
            f"""
            DELETE FROM memories
            WHERE user_id = ? AND id IN ({placeholders})
            """,
            (user_id, *batch),
        )
        memories_deleted += max(0, int(memory_cursor.rowcount))

    effects = {
        "requested_memories_deleted": len(snapshot.requested_memory_ids),
        "dependent_memories_deleted": len(snapshot.dependent_memory_ids),
        "memories_deleted": memories_deleted,
        "space_links_deleted": space_links_deleted,
        "temporal_references_relinked": temporal_references_relinked,
        "core_sections_scrubbed": core_sections_scrubbed,
        "core_history_scrubbed": core_history_scrubbed,
        "decision_logs_scrubbed": decision_logs_scrubbed,
    }
    if effects != snapshot.effects:
        raise RuntimeError("Batch purge effects drifted inside the locked transaction.")
    return effects


def _insert_batch_purge_audit(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    snapshot: _BatchPurgeSnapshot,
    effects: dict[str, int],
    fingerprint: str,
    purged_at: str,
    call_source: str,
) -> DecisionLog:
    # 统一走 _insert_decision_log：批量 purge 审计同样按用户裁剪保留条数。
    return _insert_decision_log(
        connection=connection,
        user_id=user_id,
        conversation_id=None,
        candidate_json=json.dumps(
            {
                "source": "permanent_purge_batch",
                "requested_memory_ids": snapshot.requested_memory_ids,
                "purged_memory_ids": snapshot.purge_memory_ids,
                "affected_core_sections": snapshot.affected_core_memory_sections,
                "fingerprint": fingerprint,
                "effects": effects,
                "purged_at": purged_at,
                "call_source": call_source,
            },
            ensure_ascii=False,
        ),
        decision="purge",
        reason="批量永久删除回收站记忆",
        created_at=purged_at,
    )


def _derived_memory_dependency_closure(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    root_memory_id: str,
) -> tuple[set[str], list[sqlite3.Row]]:
    """Return every user-scoped memory transitively backed by a root.

    Evidence provenance, rather than ``origin``, is the deletion boundary.  A
    merge keeps the primary row's ``user_asserted`` origin while recording the
    merged fragments as evidence, so filtering this scan to ``agent_derived``
    would leave both copied content and dangling evidence IDs after a purge.
    """
    evidence_rows = connection.execute(
        """
        SELECT id, content, source_message, source_conversation_id,
               evidence_memory_ids_json
        FROM memories
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchall()
    dependents_by_evidence_id: dict[str, list[sqlite3.Row]] = {}
    for row in evidence_rows:
        for evidence_id in set(_json_string_list(row["evidence_memory_ids_json"])):
            dependents_by_evidence_id.setdefault(evidence_id, []).append(row)

    affected_ids = {root_memory_id}
    dependent_by_id: dict[str, sqlite3.Row] = {}
    frontier = deque([root_memory_id])
    while frontier:
        evidence_id = frontier.popleft()
        for row in dependents_by_evidence_id.get(evidence_id, []):
            dependent_id = str(row["id"])
            if dependent_id in affected_ids:
                continue
            affected_ids.add(dependent_id)
            dependent_by_id[dependent_id] = row
            frontier.append(dependent_id)
    return affected_ids, list(dependent_by_id.values())


def _repair_purged_temporal_references(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    affected_ids: set[str],
    updated_at: str,
) -> int:
    """Bridge temporal links around rows that will be permanently removed."""
    rows = connection.execute(
        """
        SELECT id, supersedes, superseded_by
        FROM memories
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchall()
    links = {
        str(row["id"]): (
            str(row["supersedes"]) if row["supersedes"] else None,
            str(row["superseded_by"]) if row["superseded_by"] else None,
        )
        for row in rows
    }

    def follow(start_id: str | None, *, index: int, survivor_id: str) -> str | None:
        current = start_id
        visited: set[str] = set()
        while current in affected_ids:
            if current in visited:
                return None
            visited.add(current)
            current_links = links.get(current)
            if current_links is None:
                return None
            current = current_links[index]
        if current is None or current == survivor_id or current not in links:
            return None
        return current

    updated_count = 0
    for row in rows:
        memory_id = str(row["id"])
        if memory_id in affected_ids:
            continue
        previous_id = str(row["supersedes"]) if row["supersedes"] else None
        next_id = str(row["superseded_by"]) if row["superseded_by"] else None
        repaired_previous = (
            follow(previous_id, index=0, survivor_id=memory_id)
            if previous_id in affected_ids
            else previous_id
        )
        repaired_next = (
            follow(next_id, index=1, survivor_id=memory_id)
            if next_id in affected_ids
            else next_id
        )
        if repaired_previous == previous_id and repaired_next == next_id:
            continue
        cursor = connection.execute(
            """
            UPDATE memories
            SET supersedes = ?, superseded_by = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (repaired_previous, repaired_next, updated_at, memory_id, user_id),
        )
        updated_count += max(0, int(cursor.rowcount))
    return updated_count


def _scrub_purged_memory_artifacts(
    connection: sqlite3.Connection,
    *,
    memory: MemoryRecord,
    purged_at: str,
) -> dict[str, object]:
    affected_ids, dependent_rows = _derived_memory_dependency_closure(
        connection,
        user_id=memory.user_id,
        root_memory_id=memory.id,
    )
    dependent_ids = [str(row["id"]) for row in dependent_rows]
    temporal_references_relinked = _repair_purged_temporal_references(
        connection,
        user_id=memory.user_id,
        affected_ids=affected_ids,
        updated_at=purged_at,
    )
    affected_conversation_ids = {
        conversation_id
        for conversation_id in (
            memory.source_conversation_id,
            *(str(row["source_conversation_id"] or "") for row in dependent_rows),
        )
        if conversation_id
    }

    core_scrubbed = 0
    core_rows = connection.execute(
        """
        SELECT id, section, version, evidence_memory_ids_json
        FROM core_memory_sections
        WHERE user_id = ?
        """,
        (memory.user_id,),
    ).fetchall()
    affected_core_sections: list[dict] = []
    for row in core_rows:
        if not affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"])):
            continue
        cursor = connection.execute(
            """
            UPDATE core_memory_sections
            SET content = ?, evidence_memory_ids_json = '[]', archived = 1, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            ("[redacted: purged evidence]", purged_at, row["id"], memory.user_id),
        )
        core_scrubbed += max(0, int(cursor.rowcount))
        affected_core_sections.append(
            {
                "id": str(row["id"]),
                "section": str(row["section"]),
                "version": row["version"],
            }
        )

    history_scrubbed = 0
    history_rows = connection.execute(
        """
        SELECT id, evidence_memory_ids_json
        FROM core_memory_section_history
        WHERE user_id = ?
        """,
        (memory.user_id,),
    ).fetchall()
    for row in history_rows:
        if not affected_ids.intersection(_json_string_list(row["evidence_memory_ids_json"])):
            continue
        cursor = connection.execute(
            """
            UPDATE core_memory_section_history
            SET content = ?, evidence_memory_ids_json = '[]', updated_at = ?, replaced_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                "[redacted: purged evidence]",
                purged_at,
                purged_at,
                row["id"],
                memory.user_id,
            ),
        )
        history_scrubbed += max(0, int(cursor.rowcount))

    log_scrubbed = 0
    sensitive_fragments = {
        memory.id,
        memory.content,
        memory.source_message or "",
        *dependent_ids,
        *(str(row["content"] or "") for row in dependent_rows),
        *(str(row["source_message"] or "") for row in dependent_rows),
    }
    sensitive_fragments.discard("")
    prefilter = _decision_log_scrub_prefilter(
        affected_conversation_ids=affected_conversation_ids,
        fragments=sensitive_fragments,
    )
    if prefilter is None:
        log_rows = connection.execute(
            """
            SELECT id, conversation_id, candidate_json, reason
            FROM memory_decision_logs
            WHERE user_id = ?
            """,
            (memory.user_id,),
        ).fetchall()
    else:
        log_rows = connection.execute(
            f"""
            SELECT id, conversation_id, candidate_json, reason
            FROM memory_decision_logs
            WHERE user_id = ? AND ({prefilter[0]})
            """,
            (memory.user_id, *prefilter[1]),
        ).fetchall()
    for row in log_rows:
        candidate_json = str(row["candidate_json"] or "")
        reason = str(row["reason"] or "")
        if not (
            _decision_log_references_memory_ids(candidate_json, affected_ids)
            or (
                row["conversation_id"]
                and str(row["conversation_id"]) in affected_conversation_ids
            )
            or _decision_log_contains_fragments(
                candidate_json=candidate_json,
                reason=reason,
                fragments=sensitive_fragments,
            )
        ):
            continue
        replacement = json.dumps(
            {
                "source": "purged_memory_history",
                "memory_id": memory.id,
                "redacted": True,
                "purged_at": purged_at,
            },
            ensure_ascii=False,
        )
        cursor = connection.execute(
            """
            UPDATE memory_decision_logs
            SET candidate_json = ?, reason = ?
            WHERE id = ? AND user_id = ?
            """,
            (replacement, "历史记录因永久删除已脱敏", row["id"], memory.user_id),
        )
        log_scrubbed += max(0, int(cursor.rowcount))

    dependent_deleted = 0
    for offset in range(0, len(dependent_ids), 500):
        batch = dependent_ids[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        connection.execute(
            f"DELETE FROM memory_space_links WHERE user_id = ? AND memory_id IN ({placeholders})",
            (memory.user_id, *batch),
        )
        cursor = connection.execute(
            f"DELETE FROM memories WHERE user_id = ? AND id IN ({placeholders})",
            (memory.user_id, *batch),
        )
        dependent_deleted += max(0, int(cursor.rowcount))

    return {
        "dependent_memories_deleted": dependent_deleted,
        # Backward-compatible audit field retained for existing consumers.
        "derived_memories_deleted": dependent_deleted,
        "temporal_references_relinked": temporal_references_relinked,
        "core_sections_scrubbed": core_scrubbed,
        "core_history_scrubbed": history_scrubbed,
        "decision_logs_scrubbed": log_scrubbed,
        "affected_core_sections": affected_core_sections,
    }


def _decision_log_scrub_prefilter(
    *,
    affected_conversation_ids: set[str],
    fragments: set[str],
) -> tuple[str, list[object]] | None:
    """构造 SQL 层超集预过滤，只保留可能命中 Python 侧引用/片段判定的日志行。

    片段同时覆盖 memory id（引用判定要求 id 作为 JSON 叶子出现，raw 文本必含该 id）。
    返回 None 表示片段含 JSON 转义字符、无法安全下推 LIKE，调用方退化为按用户全量扫描。
    """
    if len(affected_conversation_ids) + (2 * len(fragments)) > 500:
        return None
    clauses: list[str] = []
    params: list[object] = []
    if affected_conversation_ids:
        placeholders = ", ".join("?" for _ in affected_conversation_ids)
        clauses.append(f"conversation_id IN ({placeholders})")
        params.extend(sorted(affected_conversation_ids))
    for fragment in sorted(fragments):
        if not _json_like_safe(fragment):
            return None
        escaped = _like_escape(fragment)
        if len(fragment) >= 12:
            # 长片段命中条件是 substring 匹配，raw JSON / reason 必包含原文。
            clauses.append("candidate_json LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
            clauses.append("reason LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        else:
            # 短片段只在 Python 侧做叶子等值匹配，raw JSON 中表现为带引号的完整串。
            clauses.append("candidate_json LIKE ? ESCAPE '\\'")
            params.append(f'%"{escaped}"%')
            clauses.append("reason = ?")
            params.append(fragment)
    if not clauses:
        return None
    return " OR ".join(f"({clause})" for clause in clauses), params


def _decision_log_contains_fragments(
    *,
    candidate_json: str,
    reason: str,
    fragments: set[str],
) -> bool:
    try:
        payload = json.loads(candidate_json)
    except (json.JSONDecodeError, TypeError):
        payload = candidate_json
    values = [*_json_leaf_strings(payload), reason]
    return any(
        value == fragment or (len(fragment) >= 12 and fragment in value)
        for value in values
        for fragment in fragments
    )


def _json_leaf_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            leaf
            for item in value.values()
            for leaf in _json_leaf_strings(item)
        ]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _json_leaf_strings(item)]
    return []


