"""Memory space membership helpers."""
from __future__ import annotations

from datetime import UTC, datetime
import re
import sqlite3
from typing import TYPE_CHECKING, Any

from app.memory.classification import normalize_classification_name
from app.memory.models import MemoryRecord, MemorySpace, new_memory_id, utc_now_iso
from app.memory.store.errors import RevisionConflictError
from app.memory.store.helpers import _ordered_unique

if TYPE_CHECKING:
    from app.memory.store._monolith import MemoryStore

_SPACE_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SPACE_DESCRIPTION_MAX = 500
_SPACE_SORT_ORDER_MAX = 9999


def normalize_space_color(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _SPACE_COLOR_RE.fullmatch(text):
        raise ValueError("color 须为 #RRGGBB 十六进制颜色，或留空")
    return text.upper()


def normalize_space_description(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > _SPACE_DESCRIPTION_MAX:
        raise ValueError(f"description 最多 {_SPACE_DESCRIPTION_MAX} 个字符")
    return text


def normalize_space_sort_order(value: int | None) -> int:
    if value is None:
        return 0
    order = int(value)
    if order < 0 or order > _SPACE_SORT_ORDER_MAX:
        raise ValueError(f"sort_order 须在 0..{_SPACE_SORT_ORDER_MAX}")
    return order


def upsert_memory_space(store: MemoryStore, *, user_id: str, name: str) -> MemorySpace:
    display_name = normalize_classification_name(name, field_name="space")
    with store._connect() as connection:
        return store._upsert_memory_space_on_connection(
            connection=connection,
            user_id=user_id,
            display_name=display_name,
        )


def _upsert_memory_space_on_connection(
    store: MemoryStore,
    *,
    connection: sqlite3.Connection,
    user_id: str,
    display_name: str,
) -> MemorySpace:
    normalized_name = display_name.casefold()
    now = utc_now_iso()
    row = connection.execute(
        """
        SELECT * FROM memory_spaces
        WHERE user_id = ? AND normalized_name = ?
        """,
        (user_id, normalized_name),
    ).fetchone()
    if row is not None:
        connection.execute(
            """
            UPDATE memory_spaces
            SET name = ?, updated_at = ?, archived = 0
            WHERE id = ? AND user_id = ?
            """,
            (display_name, now, row["id"], user_id),
        )
        updated = connection.execute(
            "SELECT * FROM memory_spaces WHERE id = ? AND user_id = ?",
            (row["id"], user_id),
        ).fetchone()
        return store._row_to_memory_space(updated)

    space = MemorySpace(
        id=new_memory_id(),
        user_id=user_id,
        name=display_name,
        normalized_name=normalized_name,
        created_at=now,
        updated_at=now,
        archived=0,
        color=None,
        description=None,
        sort_order=0,
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
    return space


def create_memory_space(
    store: MemoryStore,
    *,
    user_id: str,
    name: str,
    color: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
) -> MemorySpace:
    display_name = normalize_classification_name(name, field_name="space")
    color_value = normalize_space_color(color)
    description_value = normalize_space_description(description)
    order_value = normalize_space_sort_order(sort_order)
    now = utc_now_iso()
    with store._connect() as connection:
        existing = connection.execute(
            """
            SELECT id FROM memory_spaces
            WHERE user_id = ? AND normalized_name = ?
            """,
            (user_id, display_name.casefold()),
        ).fetchone()
        if existing is not None:
            raise ValueError("同名空间已存在")
        space = MemorySpace(
            id=new_memory_id(),
            user_id=user_id,
            name=display_name,
            normalized_name=display_name.casefold(),
            created_at=now,
            updated_at=now,
            archived=0,
            color=color_value,
            description=description_value,
            sort_order=order_value,
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
        return space


def update_memory_space(
    store: MemoryStore,
    *,
    user_id: str,
    space_id: str,
    name: str | None = None,
    color: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
    update_name: bool = False,
    update_color: bool = False,
    update_description: bool = False,
    update_sort_order: bool = False,
) -> MemorySpace | None:
    """Update space metadata. Only fields with update_*=True are written."""
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM memory_spaces
            WHERE user_id = ? AND id = ?
            """,
            (user_id, space_id),
        ).fetchone()
        if row is None:
            return None
        current = store._row_to_memory_space(row)
        display_name = current.name
        normalized_name = current.normalized_name
        if update_name:
            if name is None or not str(name).strip():
                raise ValueError("name 不能为空")
            display_name = normalize_classification_name(name, field_name="space")
            normalized_name = display_name.casefold()
            clash = connection.execute(
                """
                SELECT id FROM memory_spaces
                WHERE user_id = ? AND normalized_name = ? AND id != ?
                """,
                (user_id, normalized_name, space_id),
            ).fetchone()
            if clash is not None:
                raise ValueError("同名空间已存在")
        next_color = (
            normalize_space_color(color) if update_color else current.color
        )
        next_description = (
            normalize_space_description(description)
            if update_description
            else current.description
        )
        next_order = (
            normalize_space_sort_order(sort_order)
            if update_sort_order
            else current.sort_order
        )
        now = utc_now_iso()
        connection.execute(
            """
            UPDATE memory_spaces
            SET name = ?, normalized_name = ?, color = ?, description = ?,
                sort_order = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                display_name,
                normalized_name,
                next_color,
                next_description,
                next_order,
                now,
                space_id,
                user_id,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM memory_spaces WHERE id = ? AND user_id = ?",
            (space_id, user_id),
        ).fetchone()
        return store._row_to_memory_space(updated) if updated else None


def set_memory_space_archived(
    store: MemoryStore,
    *,
    user_id: str,
    space_id: str,
    archived: bool,
) -> MemorySpace | None:
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT id FROM memory_spaces
            WHERE user_id = ? AND id = ?
            """,
            (user_id, space_id),
        ).fetchone()
        if row is None:
            return None
        now = utc_now_iso()
        connection.execute(
            """
            UPDATE memory_spaces
            SET archived = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (1 if archived else 0, now, space_id, user_id),
        )
        updated = connection.execute(
            "SELECT * FROM memory_spaces WHERE id = ? AND user_id = ?",
            (space_id, user_id),
        ).fetchone()
        return store._row_to_memory_space(updated) if updated else None


def delete_memory_space(
    store: MemoryStore,
    *,
    user_id: str,
    space_id: str,
) -> str:
    """Delete an empty space. Returns 'deleted', 'not_found', or 'not_empty'."""
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT id FROM memory_spaces
            WHERE user_id = ? AND id = ?
            """,
            (user_id, space_id),
        ).fetchone()
        if row is None:
            return "not_found"
        link_count = connection.execute(
            """
            SELECT COUNT(*) AS n FROM memory_space_links
            WHERE user_id = ? AND space_id = ?
            """,
            (user_id, space_id),
        ).fetchone()
        if int(link_count["n"] if link_count else 0) > 0:
            return "not_empty"
        connection.execute(
            "DELETE FROM memory_spaces WHERE user_id = ? AND id = ?",
            (user_id, space_id),
        )
        return "deleted"


def list_memory_spaces(
    store: MemoryStore,
    *,
    user_id: str,
    include_archived: bool = False,
) -> list[MemorySpace]:
    query = "SELECT * FROM memory_spaces WHERE user_id = ?"
    params: list[object] = [user_id]
    if not include_archived:
        query += " AND archived = 0"
    query += " ORDER BY sort_order ASC, name ASC, updated_at DESC"
    with store._connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [store._row_to_memory_space(row) for row in rows]


def list_memory_space_summaries(
    store: MemoryStore,
    *,
    user_id: str,
    include_archived: bool = False,
) -> list[dict]:
    archived_clause = "" if include_archived else "AND s.archived = 0"
    with store._connect() as connection:
        rows = connection.execute(
            f"""
            SELECT
                s.*,
                COUNT(m.id) AS active_memory_count,
                MAX(m.updated_at) AS last_memory_updated_at
            FROM memory_spaces AS s
            LEFT JOIN memory_space_links AS l
                ON l.user_id = s.user_id AND l.space_id = s.id
            LEFT JOIN memories AS m
                ON m.user_id = s.user_id
                AND m.id = l.memory_id
                AND m.archived = 0
            WHERE s.user_id = ? {archived_clause}
            GROUP BY s.id
            ORDER BY s.sort_order ASC, active_memory_count DESC, s.name ASC
            """,
            (user_id,),
        ).fetchall()
    summaries: list[dict] = []
    for row in rows:
        space = store._row_to_memory_space(row)
        payload = space.model_dump()
        payload["active_memory_count"] = int(row["active_memory_count"] or 0)
        payload["last_memory_updated_at"] = row["last_memory_updated_at"]
        summaries.append(payload)
    return summaries


def get_memory_space(
    store: MemoryStore,
    *,
    user_id: str,
    space_id: str,
    include_archived: bool = False,
) -> MemorySpace | None:
    with store._connect() as connection:
        if include_archived:
            row = connection.execute(
                """
                SELECT * FROM memory_spaces
                WHERE user_id = ? AND id = ?
                """,
                (user_id, space_id),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT * FROM memory_spaces
                WHERE user_id = ? AND id = ? AND archived = 0
                """,
                (user_id, space_id),
            ).fetchone()
    return store._row_to_memory_space(row) if row else None


def list_memories_for_space(
    store: MemoryStore,
    *,
    user_id: str,
    space_id: str,
    limit: int = 200,
) -> list[MemoryRecord]:
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT m.*
            FROM memories AS m
            INNER JOIN memory_space_links AS l
                ON l.user_id = m.user_id AND l.memory_id = m.id
            WHERE m.user_id = ?
                AND l.space_id = ?
                AND m.archived = 0
            ORDER BY m.importance DESC, m.updated_at DESC
            LIMIT ?
            """,
            (user_id, space_id, limit),
        ).fetchall()
    return store._rows_to_memories(rows)


def replace_memory_spaces(
    store: MemoryStore,
    *,
    memory_id: str,
    user_id: str,
    space_ids: list[str],
    create_space_names: list[str] | None = None,
    expected_revision: int | None = None,
) -> MemoryRecord | None:
    normalized_space_ids = _ordered_unique(
        [str(space_id).strip() for space_id in space_ids if str(space_id).strip()]
    )
    create_space_names = create_space_names or []
    if len(normalized_space_ids) + len(create_space_names) > 10:
        raise ValueError("space_ids 最多 10 个")
    now = utc_now_iso()
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        memory_exists = connection.execute(
            """
            SELECT id, revision FROM memories
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (memory_id, user_id),
        ).fetchone()
        if memory_exists is None:
            return None
        current_revision = max(1, int(memory_exists["revision"] or 1))
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
        created_spaces = [
            store._upsert_memory_space_on_connection(
                connection=connection,
                user_id=user_id,
                display_name=normalize_classification_name(name, field_name="space"),
            )
            for name in create_space_names
        ]
        normalized_space_ids = _ordered_unique(
            [*normalized_space_ids, *(space.id for space in created_spaces)]
        )
        if len(normalized_space_ids) > 10:
            raise ValueError("space_ids 最多 10 个")
        store._validate_space_ids(
            connection=connection,
            user_id=user_id,
            space_ids=normalized_space_ids,
        )
        store._replace_memory_space_links(
            connection=connection,
            user_id=user_id,
            memory_id=memory_id,
            space_ids=normalized_space_ids,
            created_at=now,
        )
        connection.execute(
            """
            UPDATE memories
            SET updated_at = ?, revision = revision + 1
            WHERE id = ? AND user_id = ? AND archived = 0 AND revision = ?
            """,
            (now, memory_id, user_id, current_revision),
        )
        updated_row = connection.execute(
            """
            SELECT * FROM memories
            WHERE id = ? AND user_id = ? AND archived = 0
            """,
            (memory_id, user_id),
        ).fetchone()
        if updated_row is None:
            raise RuntimeError("Memory space update did not persist.")
        return store._row_to_memory(
            updated_row,
            space_ids=normalized_space_ids,
        )


def _space_ids_for_memory_ids(
    store: MemoryStore,
    *,
    user_id: str,
    memory_ids: list[str],
) -> dict[str, list[str]]:
    with store._connect() as connection:
        return store._space_ids_for_memory_ids_on_connection(
            connection=connection,
            user_id=user_id,
            memory_ids=memory_ids,
        )


def _space_ids_for_memory_ids_on_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    memory_ids: list[str],
) -> dict[str, list[str]]:
    unique_ids = _ordered_unique(memory_ids)
    if not unique_ids:
        return {}
    result = {memory_id: [] for memory_id in unique_ids}
    for offset in range(0, len(unique_ids), 500):
        batch = unique_ids[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT memory_id, space_id
            FROM memory_space_links
            WHERE user_id = ? AND memory_id IN ({placeholders})
            ORDER BY created_at ASC, rowid ASC
            """,
            (user_id, *batch),
        ).fetchall()
        for row in rows:
            result.setdefault(str(row["memory_id"]), []).append(
                str(row["space_id"])
            )
    return result


def _replace_memory_space_links(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    memory_id: str,
    space_ids: list[str],
    created_at: str,
) -> None:
    connection.execute(
        """
        DELETE FROM memory_space_links
        WHERE user_id = ? AND memory_id = ?
        """,
        (user_id, memory_id),
    )
    for space_id in _ordered_unique(space_ids):
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_space_links (
                user_id, memory_id, space_id, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (user_id, memory_id, space_id, created_at),
        )


def _filter_existing_space_ids(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    space_ids: list[str],
) -> list[str]:
    unique_ids = _ordered_unique(space_ids)
    if not unique_ids:
        return []
    placeholders = ", ".join("?" for _ in unique_ids)
    rows = connection.execute(
        f"""
        SELECT id FROM memory_spaces
        WHERE user_id = ? AND archived = 0 AND id IN ({placeholders})
        """,
        (user_id, *unique_ids),
    ).fetchall()
    existing = {str(row["id"]) for row in rows}
    return [space_id for space_id in unique_ids if space_id in existing]


def _validate_space_ids(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    space_ids: list[str],
) -> None:
    unique_ids = _ordered_unique(space_ids)
    if not unique_ids:
        return
    existing = set(
        _filter_existing_space_ids(
            connection=connection,
            user_id=user_id,
            space_ids=unique_ids,
        )
    )
    missing = [space_id for space_id in unique_ids if space_id not in existing]
    if missing:
        raise ValueError(f"空间不存在或不属于当前用户：{', '.join(missing)}")


def _row_to_memory_space(row: sqlite3.Row) -> MemorySpace:
    payload = dict(row)
    # Tolerate pre-migration rows and NULL metadata.
    if payload.get("color") is not None:
        payload["color"] = str(payload["color"]) or None
    if payload.get("description") is not None:
        text = str(payload["description"]).strip()
        payload["description"] = text or None
    try:
        payload["sort_order"] = int(payload.get("sort_order") or 0)
    except (TypeError, ValueError):
        payload["sort_order"] = 0
    return MemorySpace(**payload)


