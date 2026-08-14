"""Chat side-effect claim and finalize outbox persistence.

Kept out of the MemoryStore facade so the claim/finalize machinery lives beside
its own tables instead of in the orchestration shell.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from app.memory.store.helpers import _ConnectableStore

def claim_chat_side_effect(
    store: _ConnectableStore,
    *,
    kind: str,
    key: str,
    user_id: str,
    ttl_seconds: float,
) -> bool:
    """Atomically claim a retry-sensitive chat side effect.

    Only a hash of the turn key is persisted.  The unique constraint makes
    the guard effective across workers and process restarts; expired claims
    are removed while holding the same SQLite write lock used for insert.
    """
    normalized_kind = str(kind).strip().lower()
    if normalized_kind not in {"activate", "recent_context", "ingest"}:
        raise ValueError("unknown chat side-effect kind")
    normalized_user = str(user_id or "default").strip() or "default"
    if not key:
        raise ValueError("chat side-effect key must not be empty")
    now = datetime.now(UTC)
    expires_at = now + timedelta(
        seconds=max(30.0, min(float(ttl_seconds), 86400.0))
    )
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM chat_side_effect_claims WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO chat_side_effect_claims (
                kind, key_hash, user_id, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                normalized_kind,
                key_hash,
                normalized_user[:200],
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        return cursor.rowcount == 1

def release_chat_side_effect_claim(
    store: _ConnectableStore,
    *,
    kind: str,
    key: str,
    user_id: str,
) -> None:
    normalized_kind = str(kind).strip().lower()
    normalized_user = str(user_id or "default").strip() or "default"
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    with store._connect() as connection:
        connection.execute(
            """
            DELETE FROM chat_side_effect_claims
            WHERE kind = ? AND key_hash = ? AND user_id = ?
            """,
            (normalized_kind, key_hash, normalized_user[:200]),
        )

def enqueue_chat_finalize_job(
    store: _ConnectableStore,
    *,
    job_id: str,
    user_id: str,
    kind: str,
    claim_key: str,
    payload: dict,
) -> bool:
    """Persist finalize intent before background work. Returns True if new."""
    now = datetime.now(UTC).isoformat()
    normalized_user = str(user_id or "default").strip() or "default"
    normalized_kind = str(kind).strip().lower() or "ingest"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(body) > 512_000:
        raise ValueError("chat finalize payload too large")
    with store._connect() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO chat_finalize_jobs (
                id, user_id, kind, claim_key, payload_json, status,
                attempts, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
            """,
            (
                job_id,
                normalized_user[:200],
                normalized_kind,
                claim_key[:500],
                body,
                now,
                now,
            ),
        )
        return cursor.rowcount == 1

def mark_chat_finalize_job(
    store: _ConnectableStore,
    *,
    job_id: str,
    status: str,
    last_error: str | None = None,
    bump_attempts: bool = False,
) -> bool:
    """Transition a finalize job. "done" is terminal: a late duplicate
    delivery can never flip a completed job back and trigger re-ingest.
    Completed jobs also drop their payload copy of the conversation turn.
    Returns True when a row actually changed."""
    if status not in {"pending", "running", "done", "failed"}:
        raise ValueError("invalid finalize job status")
    now = datetime.now(UTC).isoformat()
    attempts_sql = ", attempts = attempts + 1" if bump_attempts else ""
    payload_sql = ", payload_json = ''" if status == "done" else ""
    with store._connect() as connection:
        cursor = connection.execute(
            f"""
            UPDATE chat_finalize_jobs
            SET status = ?, last_error = ?, updated_at = ?
                {attempts_sql}{payload_sql}
            WHERE id = ? AND status != 'done'
            """,
            (status, (last_error or "")[:500] or None, now, job_id),
        )
        return cursor.rowcount == 1

def prune_chat_finalize_jobs(store: _ConnectableStore, *, keep_per_user: int = 5000) -> int:
    """Cap terminal (done/failed) outbox rows per user, newest first."""
    bounded = max(1, int(keep_per_user))
    with store._connect() as connection:
        cursor = connection.execute(
            """
            DELETE FROM chat_finalize_jobs
            WHERE status IN ('done', 'failed')
              AND id NOT IN (
                SELECT id FROM chat_finalize_jobs AS newer
                WHERE newer.user_id = chat_finalize_jobs.user_id
                  AND newer.status IN ('done', 'failed')
                ORDER BY newer.updated_at DESC, newer.id DESC
                LIMIT ?
              )
            """,
            (bounded,),
        )
        return int(cursor.rowcount or 0)

def list_recoverable_chat_finalize_jobs(
    store: _ConnectableStore,
    *,
    limit: int = 20,
    stale_running_seconds: float = 120.0,
) -> list[dict[str, object]]:
    """Return pending jobs and running jobs stuck past the stale window."""
    now = datetime.now(UTC)
    stale_before = (now - timedelta(seconds=max(30.0, stale_running_seconds))).isoformat()
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT id, user_id, kind, claim_key, payload_json, status,
                   attempts, last_error, created_at, updated_at
            FROM chat_finalize_jobs
            WHERE status = 'pending'
               OR (status = 'running' AND updated_at <= ?)
            ORDER BY created_at
            LIMIT ?
            """,
            (stale_before, max(1, min(int(limit), 100))),
        ).fetchall()
    jobs: list[dict[str, object]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        jobs.append(
            {
                "id": str(row["id"]),
                "user_id": str(row["user_id"]),
                "kind": str(row["kind"]),
                "claim_key": str(row["claim_key"]),
                "payload": payload,
                "status": str(row["status"]),
                "attempts": int(row["attempts"] or 0),
                "last_error": row["last_error"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
    return jobs
