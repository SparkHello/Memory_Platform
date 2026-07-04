from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.memory.classification import classify_memory, normalize_classification_values
from app.memory.models import CandidateMemory, MemoryRecord, new_memory_id, utc_now_iso
from app.memory.store import MemoryStore, normalize_classification_name


SOURCE = "classification_backfill"


def run_backfill(
    *,
    database: str = "data/memory.db",
    user_id: str = "default",
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    if not database_path.exists():
        raise FileNotFoundError(f"database not found: {database_path}")

    backup_path: Path | None = None
    if not dry_run:
        backup_path = _backup_database(database_path)

    store = MemoryStore(str(database_path))
    if not dry_run:
        store.init_db()

    memories = _load_memories(
        database_path=database_path,
        store=store,
        user_id=user_id,
        limit=limit,
    )
    decisions = [
        _build_decision(memory=memory)
        for memory in memories
    ]
    decisions = [decision for decision in decisions if decision["changed"]]

    result: dict[str, Any] = {
        "database": str(database_path),
        "user_id": user_id,
        "dry_run": dry_run,
        "scanned_count": len(memories),
        "would_update_count": len(decisions),
        "updated_count": 0,
        "skipped_count": len(memories) - len(decisions),
        "backup_path": str(backup_path) if backup_path else None,
    }
    if dry_run or not decisions:
        return result

    now = utc_now_iso()
    with store._connect() as connection:
        for decision in decisions:
            memory = decision["memory"]
            space_ids = list(memory.space_ids)
            for space_name in decision["classification"].space_names:
                space_id = _upsert_space(
                    connection=connection,
                    user_id=user_id,
                    name=space_name,
                    now=now,
                )
                if space_id and space_id not in space_ids:
                    space_ids.append(space_id)
            space_ids = space_ids[:10]
            topics_json = json.dumps(decision["topics"], ensure_ascii=False)
            entities_json = json.dumps(decision["entities"], ensure_ascii=False)
            connection.execute(
                """
                UPDATE memories
                SET topics_json = ?, entities_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (topics_json, entities_json, now, memory.id, user_id),
            )
            _replace_space_links(
                connection=connection,
                user_id=user_id,
                memory_id=memory.id,
                space_ids=space_ids,
                created_at=now,
            )
            _insert_decision_log(
                connection=connection,
                user_id=user_id,
                memory=memory,
                before={
                    "topics": memory.topics,
                    "entities_count": len(memory.entities),
                    "space_ids_count": len(memory.space_ids),
                },
                after={
                    "topics": decision["topics"],
                    "entities_count": len(decision["entities"]),
                    "space_ids_count": len(space_ids),
                    "space_names": decision["classification"].space_names,
                },
                reason=decision["classification"].reason,
            )
            result["updated_count"] += 1
    return result


def _backup_database(database_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    backup_path = database_path.with_name(
        f"{database_path.stem}.backup.{timestamp}{database_path.suffix}"
    )
    with sqlite3.connect(str(database_path)) as source:
        with sqlite3.connect(str(backup_path)) as backup:
            source.backup(backup)
    return backup_path


def _load_memories(
    *,
    database_path: Path,
    store: MemoryStore,
    user_id: str,
    limit: int | None,
) -> list[MemoryRecord]:
    query = """
        SELECT *
        FROM memories
        WHERE user_id = ?
        ORDER BY archived ASC, updated_at DESC, created_at DESC
    """
    params: list[Any] = [user_id]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    with sqlite3.connect(str(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
        memory_ids = [str(row["id"]) for row in rows]
        space_ids_by_memory = _space_ids_by_memory(
            connection=connection,
            user_id=user_id,
            memory_ids=memory_ids,
        )
    return [
        store._row_to_memory(row, space_ids=space_ids_by_memory.get(str(row["id"]), []))
        for row in rows
    ]


def _space_ids_by_memory(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    memory_ids: list[str],
) -> dict[str, list[str]]:
    if not memory_ids:
        return {}
    placeholders = ", ".join("?" for _ in memory_ids)
    rows = connection.execute(
        f"""
        SELECT memory_id, space_id
        FROM memory_space_links
        WHERE user_id = ? AND memory_id IN ({placeholders})
        ORDER BY created_at ASC, rowid ASC
        """,
        (user_id, *memory_ids),
    ).fetchall()
    result = {memory_id: [] for memory_id in memory_ids}
    for row in rows:
        result.setdefault(str(row["memory_id"]), []).append(str(row["space_id"]))
    return result


def _build_decision(*, memory: MemoryRecord) -> dict[str, Any]:
    candidate = CandidateMemory(
        action="create",
        memory=memory.content,
        type=memory.type,
        importance=memory.importance,
        confidence=memory.confidence,
        valence=memory.valence,
        arousal=memory.arousal,
        stability=memory.stability,
        valid_from=memory.valid_from,
        valid_until=memory.valid_until,
        review_after=memory.review_after,
        sensitivity=memory.sensitivity,
        temporal_subject=memory.temporal_subject,
        temporal_predicate=memory.temporal_predicate,
        topics=memory.topics,
        entities=memory.entities,
    )
    classification = classify_memory(
        candidate,
        source_text=memory.source_message or memory.content,
        existing_topics=memory.topics,
    )
    topics = normalize_classification_values(
        [*memory.topics, *classification.topics],
        max_items=20,
        field_name="topics",
    )
    if memory.sensitivity in {"private", "sensitive"}:
        entities = memory.entities
    else:
        entities = normalize_classification_values(
            [*memory.entities, *classification.entities],
            max_items=20,
            field_name="entities",
        )
    needs_space = bool(classification.space_names and not memory.space_ids)
    changed = topics != memory.topics or entities != memory.entities or needs_space
    return {
        "memory": memory,
        "classification": classification,
        "topics": topics,
        "entities": entities,
        "changed": changed,
    }


def _upsert_space(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    name: str,
    now: str,
) -> str | None:
    try:
        display_name = normalize_classification_name(name, field_name="space")
    except ValueError:
        return None
    normalized_name = display_name.casefold()
    row = connection.execute(
        """
        SELECT id FROM memory_spaces
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
        return str(row["id"])

    space_id = new_memory_id()
    connection.execute(
        """
        INSERT INTO memory_spaces (
            id, user_id, name, normalized_name, created_at, updated_at, archived
        )
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (space_id, user_id, display_name, normalized_name, now, now),
    )
    return space_id


def _replace_space_links(
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
    seen: set[str] = set()
    for space_id in space_ids:
        if not space_id or space_id in seen:
            continue
        seen.add(space_id)
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_space_links (
                user_id, memory_id, space_id, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (user_id, memory_id, space_id, created_at),
        )


def _insert_decision_log(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    memory: MemoryRecord,
    before: dict[str, Any],
    after: dict[str, Any],
    reason: str,
) -> None:
    candidate_json = json.dumps(
        {
            "source": SOURCE,
            "memory_id": memory.id,
            "archived": bool(memory.archived),
            "content_length": len(memory.content),
            "content_sha256": hashlib.sha256(memory.content.encode("utf-8")).hexdigest(),
            "before": before,
            "after": after,
        },
        ensure_ascii=False,
    )
    connection.execute(
        """
        INSERT INTO memory_decision_logs (
            id, user_id, conversation_id, candidate_json, decision, reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_memory_id(),
            user_id,
            None,
            candidate_json,
            "update",
            f"{SOURCE}: {reason}",
            utc_now_iso(),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill memory topics, entities, and spaces.")
    parser.add_argument("--database", default="data/memory.db")
    parser.add_argument("--user-id", default="default")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run_backfill(
        database=args.database,
        user_id=args.user_id,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
