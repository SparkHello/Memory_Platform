"""Memory CRUD, listing, archive/restore and row mapping helpers."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import math
import sqlite3
from typing import Any

from app.memory.classification import (
    normalize_classification_name,
    normalize_classification_names,
)
from app.memory.models import (
    MemoryAction,
    MemoryOrigin,
    MemoryRecord,
    MemorySensitivity,
    MemorySourceExplanation,
    MemoryStability,
    MemoryType,
    normalize_iso_text,
    normalize_memory_type,
    normalize_optional_text,
    new_memory_id,
    utc_now_iso,
)
from app.memory.store.constants import _UNSET
from app.memory.store.errors import RevisionConflictError
from app.memory.store.helpers import (
    _ConnectableStore,
    _bounded_float,
    _coerce_float,
    _coerce_float_or_none,
    _coerce_int,
    _insert_memory_row,
    _json_string_list,
    _ordered_unique,
    _row_to_memory,
    _rows_to_memories,
    _rows_to_memories_on_connection,
    _sensitivity_with_floor,
    _space_ids_for_memory_ids_on_connection,
)
from app.memory.store.core_memory import (
    list_core_memory_sections as _list_core_memory_sections,
)
from app.memory.store.spaces import (
    _replace_memory_space_links,
    _upsert_memory_space_on_connection,
    _validate_space_ids,
)
from app.memory.store.temporal import (
    _apply_temporal_invalidation,
    _apply_time_ripple,
    _detach_temporal_position,
    _rebuild_temporal_key,
)
from app.memory.utils import _parse_iso_datetime

def create_memory(
    store: _ConnectableStore,
    *,
    user_id: str,
    content: str,
    type: MemoryType = "semantic",
    importance: int = 1,
    confidence: float = 0.7,
    valence: float = 0.5,
    arousal: float = 0.3,
    source_message: str | None = None,
    source_conversation_id: str | None = None,
    origin: MemoryOrigin = "user_asserted",
    embedding_json: str | None = None,
    embedding_space_id: str | None = None,
    stability: MemoryStability = "stable",
    valid_from: str | None = None,
    valid_until: str | None = None,
    review_after: str | None = None,
    sensitivity: MemorySensitivity = "normal",
    evidence_memory_ids: list[str] | None = None,
    topics: list[str] | None = None,
    entities: list[str] | None = None,
    temporal_subject: str | None = None,
    temporal_predicate: str | None = None,
    space_ids: list[str] | None = None,
    decay_lambda: float | None = None,
    final_matcher: Callable[[list[MemoryRecord]], MemoryRecord | None] | None = None,
) -> MemoryRecord:
    now = utc_now_iso()
    evidence_memory_ids = evidence_memory_ids or []
    topics = normalize_classification_names(topics or [], max_items=20, field_name="topics")
    entities = normalize_classification_names(entities or [], max_items=20, field_name="entities")
    sensitivity = _sensitivity_with_floor(
        declared=sensitivity,
        content=content,
        source_message=source_message,
        entities=entities,
    )
    temporal_subject = normalize_optional_text(temporal_subject)
    temporal_predicate = normalize_optional_text(temporal_predicate)
    space_ids = _ordered_unique([str(space_id).strip() for space_id in (space_ids or []) if str(space_id).strip()])
    if len(space_ids) > 10:
        raise ValueError("space_ids 最多 10 个")
    memory = MemoryRecord(
        id=new_memory_id(),
        user_id=user_id,
        content=content,
        type=type,
        importance=importance,
        confidence=confidence,
        valence=valence,
        arousal=arousal,
        source_message=source_message,
        source_conversation_id=source_conversation_id,
        origin=origin,
        embedding_json=embedding_json,
        embedding_space_id=embedding_space_id,
        last_used_at=None,
        usage_count=0,
        stability=stability,
        valid_from=valid_from,
        valid_until=valid_until,
        review_after=review_after,
        sensitivity=sensitivity,
        evidence_memory_ids=evidence_memory_ids,
        topics=topics,
        entities=entities,
        temporal_subject=temporal_subject,
        temporal_predicate=temporal_predicate,
        space_ids=space_ids,
        decay_lambda=decay_lambda,
        created_at=now,
        updated_at=now,
        archived_at=None,
        archived=0,
    )
    with store._connect() as connection:
        if final_matcher is not None:
            connection.execute("BEGIN IMMEDIATE")
            latest_rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND archived = 0
                  AND (status IS NULL OR status != 'archived')
                ORDER BY importance DESC, updated_at DESC
                """,
                (user_id,),
            ).fetchall()
            matched = final_matcher(
                _rows_to_memories_on_connection(
                    connection=connection,
                    rows=latest_rows,
                )
            )
            if matched is not None:
                return matched
        _validate_space_ids(
            connection=connection,
            user_id=user_id,
            space_ids=space_ids,
        )
        _insert_memory_row(connection=connection, memory=memory)
        _replace_memory_space_links(
            connection=connection,
            user_id=user_id,
            memory_id=memory.id,
            space_ids=space_ids,
            created_at=now,
        )
        _apply_temporal_invalidation(
            connection=connection,
            user_id=user_id,
            new_memory=memory,
        )
    return memory

def update_memory(
    store: _ConnectableStore,
    *,
    memory_id: str,
    user_id: str,
    content: str,
    type: MemoryType,
    importance: int,
    confidence: float,
    valence: float,
    arousal: float,
    source_message: str | None = None,
    source_conversation_id: str | None = None,
    embedding_json: str | None = None,
    embedding_space_id: object = _UNSET,
    stability: MemoryStability = "stable",
    valid_from: object = _UNSET,
    valid_until: object = _UNSET,
    review_after: str | None = None,
    sensitivity: MemorySensitivity = "normal",
    evidence_memory_ids: list[str] | None = None,
    topics: list[str] | None = None,
    entities: list[str] | None = None,
    temporal_subject: object = _UNSET,
    temporal_predicate: object = _UNSET,
    status: str | None = None,
    decay_lambda: object = _UNSET,
    expected_revision: int | None = None,
    replacement_space_ids: list[str] | None = None,
    replacement_space_names: list[str] | None = None,
) -> MemoryRecord | None:
    valid_until_was_unset = valid_until is _UNSET
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        live_row = connection.execute(
            """
            SELECT * FROM memories
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (memory_id, user_id),
        ).fetchone()
        if live_row is None:
            return None

        current_revision = max(1, int(live_row["revision"] or 1))
        if (
            expected_revision is not None
            and int(expected_revision) != current_revision
        ):
            raise RevisionConflictError(
                resource="memory",
                resource_id=memory_id,
                expected_revision=int(expected_revision),
                current_revision=current_revision,
            )

        existing_space_ids = _space_ids_for_memory_ids_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=[memory_id],
        ).get(memory_id, [])
        existing = _row_to_memory(live_row, space_ids=existing_space_ids)
        if replacement_space_ids is not None or replacement_space_names is not None:
            replacement_space_ids = _ordered_unique(replacement_space_ids or [])
            replacement_space_names = replacement_space_names or []
            if len(replacement_space_ids) + len(replacement_space_names) > 10:
                raise ValueError("space_ids 最多 10 个")
            created_spaces = [
                _upsert_memory_space_on_connection(
                    connection=connection,
                    user_id=user_id,
                    display_name=normalize_classification_name(
                        name,
                        field_name="space",
                    ),
                )
                for name in replacement_space_names
            ]
            replacement_space_ids = _ordered_unique(
                [
                    *replacement_space_ids,
                    *(space.id for space in created_spaces),
                ]
            )
            _validate_space_ids(
                connection=connection,
                user_id=user_id,
                space_ids=replacement_space_ids,
            )
        if evidence_memory_ids is None:
            evidence_memory_ids = existing.evidence_memory_ids
        if topics is None:
            topics = existing.topics
        if entities is None:
            entities = existing.entities
        if valid_from is _UNSET:
            valid_from = existing.valid_from
        if valid_until is _UNSET:
            valid_until = existing.valid_until
        if temporal_subject is _UNSET:
            temporal_subject = existing.temporal_subject
        if temporal_predicate is _UNSET:
            temporal_predicate = existing.temporal_predicate
        if decay_lambda is _UNSET:
            decay_lambda = existing.decay_lambda
        content_changed = content != existing.content
        if content_changed:
            embedding_json = None
            embedding_space_id = None
        elif embedding_json is None:
            embedding_space_id = None
        elif embedding_space_id is _UNSET:
            embedding_space_id = (
                existing.embedding_space_id
                if embedding_json == existing.embedding_json
                else None
            )
        if (
            valid_until_was_unset
            and existing.superseded_by
            and (
                normalize_iso_text(valid_from) != existing.valid_from
                or normalize_optional_text(temporal_subject)
                != existing.temporal_subject
                or normalize_optional_text(temporal_predicate)
                != existing.temporal_predicate
            )
        ):
            # This boundary was synthesized from the old successor. A moved
            # row must let its new chain position choose the next boundary.
            valid_until = None
            if status is None and existing.status == "resolved":
                # The resolved state was derived from that synthesized
                # boundary; a moved row re-enters its new key as live.
                status = "dynamic"

        topics = normalize_classification_names(
            topics,
            max_items=20,
            field_name="topics",
        )
        entities = normalize_classification_names(
            entities,
            max_items=20,
            field_name="entities",
        )
        sensitivity = _sensitivity_with_floor(
            declared=sensitivity,
            content=content,
            source_message=source_message,
            entities=entities,
        )
        now = utc_now_iso()
        prospective = MemoryRecord(
            **{
                **existing.model_dump(),
                "content": content,
                "type": type,
                "importance": importance,
                "confidence": confidence,
                "valence": valence,
                "arousal": arousal,
                "source_message": source_message,
                "source_conversation_id": source_conversation_id,
                "embedding_json": embedding_json,
                "embedding_space_id": embedding_space_id,
                "stability": stability,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "review_after": review_after,
                "sensitivity": sensitivity,
                "evidence_memory_ids": evidence_memory_ids,
                "topics": topics,
                "entities": entities,
                "temporal_subject": temporal_subject,
                "temporal_predicate": temporal_predicate,
                "status": status if status is not None else existing.status,
                "decay_lambda": decay_lambda,
                "updated_at": now,
                "revision": current_revision + 1,
            }
        )
        topology_changed = any(
            getattr(existing, field_name) != getattr(prospective, field_name)
            for field_name in (
                "valid_from",
                "valid_until",
                "temporal_subject",
                "temporal_predicate",
            )
        )
        if topology_changed:
            prospective.supersedes = None
            prospective.superseded_by = None

        evidence_json = json.dumps(
            prospective.evidence_memory_ids,
            ensure_ascii=False,
        )
        topics_json = json.dumps(prospective.topics, ensure_ascii=False)
        entities_json = json.dumps(prospective.entities, ensure_ascii=False)
        if topology_changed:
            _detach_temporal_position(
                connection=connection,
                user_id=user_id,
                memory=existing,
            )
        cursor = connection.execute(
            """
            UPDATE memories
            SET content = ?, type = ?, importance = ?, confidence = ?,
                valence = ?, arousal = ?, source_message = ?, source_conversation_id = ?,
                embedding_json = ?, embedding_space_id = ?,
                stability = ?, valid_from = ?, valid_until = ?,
                review_after = ?, sensitivity = ?,
                evidence_memory_ids_json = ?, topics_json = ?, entities_json = ?,
                temporal_subject = ?, temporal_predicate = ?, status = ?,
                decay_lambda = ?, supersedes = ?, superseded_by = ?, updated_at = ?,
                revision = ?
            WHERE id = ? AND user_id = ? AND archived = 0 AND revision = ?
            """,
            (
                prospective.content,
                prospective.type,
                prospective.importance,
                prospective.confidence,
                prospective.valence,
                prospective.arousal,
                prospective.source_message,
                prospective.source_conversation_id,
                prospective.embedding_json,
                prospective.embedding_space_id,
                prospective.stability,
                prospective.valid_from,
                prospective.valid_until,
                prospective.review_after,
                prospective.sensitivity,
                evidence_json,
                topics_json,
                entities_json,
                prospective.temporal_subject,
                prospective.temporal_predicate,
                prospective.status,
                prospective.decay_lambda,
                prospective.supersedes,
                prospective.superseded_by,
                now,
                prospective.revision,
                memory_id,
                user_id,
                current_revision,
            ),
        )
        if cursor.rowcount == 0:
            raise RuntimeError("Memory revision changed while holding the write lock.")
        if replacement_space_ids is not None:
            _replace_memory_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=memory_id,
                space_ids=replacement_space_ids,
                created_at=now,
            )
            existing_space_ids = replacement_space_ids
        if (
            topology_changed
            and prospective.status != "archived"
            and prospective.temporal_subject
            and prospective.temporal_predicate
        ):
            _apply_temporal_invalidation(
                connection=connection,
                user_id=user_id,
                new_memory=prospective,
            )
        if topology_changed:
            temporal_keys = {
                key
                for key in (
                    (
                        existing.temporal_subject,
                        existing.temporal_predicate,
                    ),
                    (
                        prospective.temporal_subject,
                        prospective.temporal_predicate,
                    ),
                )
                if key[0] is not None and key[1] is not None
            }
            for subject, predicate in temporal_keys:
                _rebuild_temporal_key(
                    connection=connection,
                    user_id=user_id,
                    temporal_subject=subject,
                    temporal_predicate=predicate,
                )
        updated_row = connection.execute(
            """
            SELECT * FROM memories
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (memory_id, user_id),
        ).fetchone()
        if updated_row is None:
            raise RuntimeError("Memory update did not persist.")
        return _row_to_memory(updated_row, space_ids=existing_space_ids)

def get_memory(store: _ConnectableStore, *, memory_id: str, user_id: str) -> MemoryRecord | None:
    with store._connect() as connection:
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
    return _row_to_memory(row, space_ids=space_ids)

def list_memory_timeline(
    store: _ConnectableStore,
    *,
    user_id: str,
    subject: str,
    predicate: str | None = None,
    include_archived: bool = False,
) -> list[MemoryRecord]:
    temporal_subject = normalize_optional_text(subject)
    temporal_predicate = normalize_optional_text(predicate)
    if temporal_subject is None:
        return []

    conditions = [
        "user_id = ?",
        "archived = 0",
        "temporal_subject = ?",
    ]
    params: list[object] = [user_id, temporal_subject]
    if temporal_predicate is not None:
        conditions.append("temporal_predicate = ?")
        params.append(temporal_predicate)
    if not include_archived:
        conditions.append("(status IS NULL OR status != 'archived')")

    query = f"""
        SELECT * FROM memories
        WHERE {' AND '.join(conditions)}
        ORDER BY COALESCE(valid_from, created_at) ASC, created_at ASC
    """
    with store._connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return _rows_to_memories(store, rows)

def list_memories(
    store: _ConnectableStore,
    *,
    user_id: str,
    limit: int = 200,
    status: str | None = None,
    include_lifecycle_archived: bool = False,
) -> list[MemoryRecord]:
    if status and status != "all":
        sql = """SELECT * FROM memories
                 WHERE user_id = ? AND archived = 0 AND status = ?
                 ORDER BY importance DESC, updated_at DESC
                 LIMIT ?"""
        params: tuple = (user_id, status, limit)
    elif include_lifecycle_archived or status == "all":
        sql = """SELECT * FROM memories
                 WHERE user_id = ? AND archived = 0
                 ORDER BY importance DESC, updated_at DESC
                 LIMIT ?"""
        params = (user_id, limit)
    else:
        sql = """SELECT * FROM memories
                 WHERE user_id = ? AND archived = 0
                   AND (status IS NULL OR status != 'archived')
                 ORDER BY importance DESC, updated_at DESC
                 LIMIT ?"""
        params = (user_id, limit)
    with store._connect() as connection:
        rows = connection.execute(sql, params).fetchall()
    return _rows_to_memories(store, rows)

def list_memories_for_resolution(store: _ConnectableStore, *, user_id: str) -> list[MemoryRecord]:
    """Return the complete active candidate set used for write deduplication.

    Resolver correctness must not depend on importance ordering: an exact
    duplicate below an arbitrary top-N cutoff is still a duplicate.
    """
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM memories
            WHERE user_id = ? AND archived = 0
              AND (status IS NULL OR status != 'archived')
            ORDER BY importance DESC, updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    return _rows_to_memories(store, rows)

@contextmanager
def memory_recall_snapshot(
    store: _ConnectableStore,
    *,
    user_id: str,
    page_size: int = 500,
) -> Iterator[Callable[[], Iterator[list[MemoryRecord]]]]:
    """Expose repeatable, bounded page scans from one SQLite snapshot.

    Search needs two passes because keyword IDF is corpus-wide.  Returning a
    fresh page iterator for each pass keeps the read transaction (and thus
    the visible corpus) stable without ever materializing the whole library.
    """
    bounded_page_size = max(1, min(int(page_size), 1000))
    with store._connect() as connection:
        connection.execute("BEGIN")

        def read_pages() -> Iterator[list[MemoryRecord]]:
            last_rowid: int | None = None
            while True:
                if last_rowid is None:
                    rows = connection.execute(
                        """
                        SELECT rowid AS recall_rowid, *
                        FROM memories
                        WHERE user_id = ? AND archived = 0
                          AND (status IS NULL OR status != 'archived')
                        ORDER BY rowid ASC
                        LIMIT ?
                        """,
                        (user_id, bounded_page_size),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT rowid AS recall_rowid, *
                        FROM memories
                        WHERE user_id = ? AND archived = 0
                          AND (status IS NULL OR status != 'archived')
                          AND rowid > ?
                        ORDER BY rowid ASC
                        LIMIT ?
                        """,
                        (user_id, last_rowid, bounded_page_size),
                    ).fetchall()
                if not rows:
                    return
                last_rowid = int(rows[-1]["recall_rowid"])
                yield _rows_to_memories_on_connection(
                    connection=connection,
                    rows=rows,
                )

        yield read_pages

def get_memories_max_updated_at(store: _ConnectableStore, *, user_id: str) -> str | None:
    """返回该用户所有活跃记忆的最新 updated_at，用于缓存失效比对。"""
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT MAX(updated_at) FROM memories
            WHERE user_id = ? AND archived = 0
              AND (status IS NULL OR status != 'archived')
            """,
            (user_id,),
        ).fetchone()
    return row[0] if row and row[0] else None

def get_active_memory_count(store: _ConnectableStore, *, user_id: str) -> int:
    """返回该用户活跃记忆的数量，用于缓存失效比对。"""
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) FROM memories
            WHERE user_id = ? AND archived = 0
              AND (status IS NULL OR status != 'archived')
            """,
            (user_id,),
        ).fetchone()
    return int(row[0]) if row else 0

def list_archived_memories(
    store: _ConnectableStore,
    *,
    user_id: str,
    limit: int = 200,
) -> list[MemoryRecord]:
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM memories
            WHERE user_id = ? AND archived = 1
            ORDER BY archived_at DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return _rows_to_memories(store, rows)

def explain_memory_source(
    store: _ConnectableStore,
    *,
    memory_id: str,
    user_id: str,
) -> MemorySourceExplanation | None:
    memory = get_memory(store, memory_id=memory_id, user_id=user_id)
    if memory is None:
        return None
    core_sections = [
        section.section
        for section in _list_core_memory_sections(store, user_id=user_id)
        if memory.id in section.evidence_memory_ids
    ]
    return MemorySourceExplanation(
        memory_id=memory.id,
        content=memory.content,
        source_excerpt=memory.source_message,
        source_conversation_id=memory.source_conversation_id,
        saved_at=memory.created_at,
        updated_at=memory.updated_at,
        confidence=memory.confidence,
        is_core_memory_evidence=bool(core_sections),
        core_memory_sections=core_sections,
        evidence_memory_ids=memory.evidence_memory_ids,
    )

def archive_memory(
    store: _ConnectableStore,
    *,
    memory_id: str,
    user_id: str,
    expected_revision: int | None = None,
    return_revision: bool = False,
) -> bool | int:
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
            return False
        current_revision = max(1, int(row["revision"] or 1))
        if (
            expected_revision is not None
            and int(expected_revision) != current_revision
        ):
            raise RevisionConflictError(
                resource="memory",
                resource_id=memory_id,
                expected_revision=int(expected_revision),
                current_revision=current_revision,
            )
        source = _row_to_memory(row, space_ids=[])
        now = utc_now_iso()
        cursor = connection.execute(
            """
            UPDATE memories
            SET archived = 1, archived_at = ?, updated_at = ?,
                revision = revision + 1
            WHERE id = ? AND user_id = ? AND archived = 0 AND revision = ?
            """,
            (now, now, memory_id, user_id, current_revision),
        )
        if cursor.rowcount == 0:
            return False
        if source.temporal_subject and source.temporal_predicate:
            _rebuild_temporal_key(
                connection=connection,
                user_id=user_id,
                temporal_subject=source.temporal_subject,
                temporal_predicate=source.temporal_predicate,
            )
        return current_revision + 1 if return_revision else True

def restore_memory(store: _ConnectableStore, *, memory_id: str, user_id: str) -> MemoryRecord | None:
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
        space_ids = _space_ids_for_memory_ids_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=[memory_id],
        ).get(memory_id, [])
        source = _row_to_memory(row, space_ids=space_ids)
        has_temporal_key = bool(
            source.temporal_subject and source.temporal_predicate
        )
        restored_status = (
            "pinned" if source.status == "pinned" else "dynamic"
        ) if has_temporal_key else source.status
        now = utc_now_iso()
        cursor = connection.execute(
            """
            UPDATE memories
            SET archived = 0, archived_at = NULL, status = ?, updated_at = ?,
                revision = revision + 1
            WHERE id = ? AND user_id = ? AND archived = 1
            """,
            (restored_status, now, memory_id, user_id),
        )
        if cursor.rowcount == 0:
            return None
        if has_temporal_key:
            _rebuild_temporal_key(
                connection=connection,
                user_id=user_id,
                temporal_subject=source.temporal_subject,
                temporal_predicate=source.temporal_predicate,
            )
        restored_row = connection.execute(
            """
            SELECT * FROM memories
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (memory_id, user_id),
        ).fetchone()
        if restored_row is None:
            raise RuntimeError("Memory restore did not persist.")
        return _row_to_memory(restored_row, space_ids=space_ids)

def update_memory_embedding(
    store: _ConnectableStore,
    *,
    memory_id: str,
    user_id: str,
    embedding_json: str,
    embedding_space_id: str,
) -> bool:
    """仅更新活跃记忆的 embedding，用于 re-embed 流程。"""
    normalized_space_id = " ".join(embedding_space_id.strip().split())
    if not normalized_space_id:
        raise ValueError("embedding_space_id 不能为空")
    if len(normalized_space_id) > 300:
        raise ValueError("embedding_space_id 最多 300 个字符")
    now = utc_now_iso()
    with store._connect() as connection:
        cursor = connection.execute(
            """
            UPDATE memories
            SET embedding_json = ?, embedding_space_id = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (embedding_json, normalized_space_id, now, memory_id, user_id),
        )
    return cursor.rowcount > 0

def archive_expired_memories(store: _ConnectableStore, *, user_id: str) -> int:
    """Archive expired temporary memories without erasing version history."""
    now_iso = utc_now_iso()
    now = _parse_iso_datetime(now_iso)
    if now is None:
        return 0
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT id, valid_until
            FROM memories
            WHERE user_id = ? AND archived = 0 AND valid_until IS NOT NULL
              AND temporal_subject IS NULL
              AND temporal_predicate IS NULL
              AND supersedes IS NULL
              AND superseded_by IS NULL
            """,
            (user_id,),
        ).fetchall()
        expired_ids = [
            str(row["id"])
            for row in rows
            if (expires := _parse_iso_datetime(row["valid_until"])) is not None
            and expires < now
        ]
        if not expired_ids:
            return 0
        placeholders = ", ".join("?" for _ in expired_ids)
        cursor = connection.execute(
            f"""
            UPDATE memories
            SET archived = 1, archived_at = ?, updated_at = ?
            WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
            """,
            (now_iso, now_iso, user_id, *expired_ids),
        )
    return int(cursor.rowcount)

def mark_memories_used(
    store: _ConnectableStore,
    *,
    memory_ids: list[str],
    user_id: str,
    time_ripple_delta: float = 0.0,
    time_ripple_window_hours: int = 48,
) -> str | None:
    unique_ids = _ordered_unique([str(memory_id) for memory_id in memory_ids if memory_id])
    if not unique_ids:
        return None
    now = utc_now_iso()
    placeholders = ", ".join("?" for _ in unique_ids)
    with store._connect() as connection:
        connection.execute(
            f"""
            UPDATE memories
            SET usage_count = COALESCE(usage_count, 0) + 1,
                last_used_at = ?
            WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
            """,
            (now, user_id, *unique_ids),
        )
        _apply_time_ripple(
            connection=connection,
            user_id=user_id,
            seed_ids=unique_ids,
            used_at=now,
            delta=time_ripple_delta,
            window_hours=time_ripple_window_hours,
        )
    return now

def touch_memory(
    store: _ConnectableStore,
    *,
    memory_id: str,
    user_id: str,
    time_ripple_delta: float = 0.0,
    time_ripple_window_hours: int = 48,
) -> None:
    """单条记忆 touch：递增 usage_count 并刷新 last_used_at。"""
    mark_memories_used(
        store,
        memory_ids=[memory_id],
        user_id=user_id,
        time_ripple_delta=time_ripple_delta,
        time_ripple_window_hours=time_ripple_window_hours,
    )

def update_memory_statuses(
    store: _ConnectableStore,
    *,
    memory_ids: list[str],
    user_id: str,
    status: str,
) -> int:
    """Update lifecycle status for active memories and return affected row count."""
    if status not in {"dynamic", "resolved", "archived", "pinned"}:
        raise ValueError("status must be dynamic, resolved, archived, or pinned")
    unique_ids = _ordered_unique(memory_ids)
    if not unique_ids:
        return 0
    placeholders = ", ".join("?" for _ in unique_ids)
    now = utc_now_iso()
    with store._connect() as connection:
        cursor = connection.execute(
            f"""
            UPDATE memories
            SET status = ?, updated_at = ?
            WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
            """,
            (status, now, user_id, *unique_ids),
        )
    return int(cursor.rowcount)

