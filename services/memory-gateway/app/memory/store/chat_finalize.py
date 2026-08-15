"""Chat side-effect claims and the durable finalize outbox.

Activation and recent-context updates use short-lived, body-free claims. Chat
ingest instead uses this outbox as its cross-process authority: workers claim
one row under a SQLite write lock and may finish it only with the matching
lease token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
from uuid import uuid4

from app.memory.store.helpers import ConnectionProvider


CHAT_FINALIZE_LEASE_SECONDS = 300.0
CHAT_FINALIZE_MAX_ATTEMPTS = 8
CHAT_FINALIZE_MAX_AGE_SECONDS = 24 * 60 * 60.0
CHAT_FINALIZE_MAX_NONTERMINAL_PER_USER = 100
CHAT_FINALIZE_MAX_PAYLOAD_CHARS = 512_000


class ChatFinalizeQueueFullError(RuntimeError):
    """The user's durable finalize queue reached its live-row bound."""


def claim_chat_side_effect(
    store: ConnectionProvider,
    *,
    kind: str,
    key: str,
    user_id: str,
    ttl_seconds: float,
) -> bool:
    """Atomically claim an activation or recent-context side effect."""
    normalized_kind = str(kind).strip().lower()
    if normalized_kind not in {"activate", "recent_context"}:
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
    store: ConnectionProvider,
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


def _terminate_ineligible_jobs(
    connection: sqlite3.Connection,
    *,
    now: datetime,
) -> None:
    """Fail live jobs that can no longer be attempted, dropping turn text."""
    now_text = now.isoformat()
    age_cutoff = (
        now - timedelta(seconds=CHAT_FINALIZE_MAX_AGE_SECONDS)
    ).isoformat()
    connection.execute(
        "UPDATE chat_finalize_jobs SET attempts = ? WHERE attempts > ?",
        (CHAT_FINALIZE_MAX_ATTEMPTS, CHAT_FINALIZE_MAX_ATTEMPTS),
    )
    connection.execute(
        """
        UPDATE chat_finalize_jobs
        SET status = 'failed',
            payload_json = '',
            lease_token = NULL,
            lease_expires_at = NULL,
            last_error = COALESCE(last_error, 'max_attempts_exceeded'),
            updated_at = ?
        WHERE status IN ('pending', 'running') AND attempts >= ?
        """,
        (now_text, CHAT_FINALIZE_MAX_ATTEMPTS),
    )
    connection.execute(
        """
        UPDATE chat_finalize_jobs
        SET status = 'failed',
            payload_json = '',
            lease_token = NULL,
            lease_expires_at = NULL,
            last_error = COALESCE(last_error, 'max_age_exceeded'),
            updated_at = ?
        WHERE status IN ('pending', 'running') AND created_at <= ?
        """,
        (now_text, age_cutoff),
    )


def _cap_nonterminal_jobs(
    connection: sqlite3.Connection,
    *,
    now: datetime,
) -> None:
    """Repair legacy/abnormal queues so each user has at most 100 live rows."""
    rows = connection.execute(
        """
        SELECT id, user_id
        FROM chat_finalize_jobs
        WHERE status IN ('pending', 'running')
        ORDER BY user_id, created_at DESC, rowid DESC
        """
    ).fetchall()
    counts: dict[str, int] = {}
    overflow: list[str] = []
    for row in rows:
        user_id = str(row["user_id"])
        counts[user_id] = counts.get(user_id, 0) + 1
        if counts[user_id] > CHAT_FINALIZE_MAX_NONTERMINAL_PER_USER:
            overflow.append(str(row["id"]))
    now_text = now.isoformat()
    for offset in range(0, len(overflow), 500):
        batch = overflow[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        connection.execute(
            f"""
            UPDATE chat_finalize_jobs
            SET status = 'failed',
                payload_json = '',
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = COALESCE(last_error, 'queue_limit_exceeded'),
                updated_at = ?
            WHERE id IN ({placeholders})
              AND status IN ('pending', 'running')
            """,
            (now_text, *batch),
        )


def enqueue_chat_finalize_job(
    store: ConnectionProvider,
    *,
    job_id: str,
    user_id: str,
    kind: str,
    claim_key: str,
    payload: dict,
) -> bool:
    """Persist finalize intent before ingest; return False for a duplicate."""
    normalized_user = str(user_id or "default").strip() or "default"
    normalized_kind = str(kind).strip().lower()
    if normalized_kind != "ingest":
        raise ValueError("unknown chat finalize kind")
    if not job_id or not claim_key:
        raise ValueError("chat finalize id and claim key must not be empty")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(body) > CHAT_FINALIZE_MAX_PAYLOAD_CHARS:
        raise ValueError("chat finalize payload too large")
    now = datetime.now(UTC)
    now_text = now.isoformat()
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _terminate_ineligible_jobs(connection, now=now)
        duplicate = connection.execute(
            """
            SELECT 1 FROM chat_finalize_jobs
            WHERE id = ? OR (kind = ? AND claim_key = ?)
            LIMIT 1
            """,
            (job_id, normalized_kind, claim_key[:500]),
        ).fetchone()
        if duplicate is not None:
            return False
        live_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM chat_finalize_jobs
                WHERE user_id = ? AND status IN ('pending', 'running')
                """,
                (normalized_user[:200],),
            ).fetchone()[0]
        )
        if live_count >= CHAT_FINALIZE_MAX_NONTERMINAL_PER_USER:
            raise ChatFinalizeQueueFullError(
                "chat finalize queue has too many nonterminal jobs"
            )
        connection.execute(
            """
            INSERT INTO chat_finalize_jobs (
                id, user_id, kind, claim_key, payload_json, status,
                attempts, last_error, lease_token, lease_expires_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, ?, ?)
            """,
            (
                job_id,
                normalized_user[:200],
                normalized_kind,
                claim_key[:500],
                body,
                now_text,
                now_text,
            ),
        )
        return True


def claim_chat_finalize_job(
    store: ConnectionProvider,
    *,
    job_id: str | None = None,
    lease_seconds: float = CHAT_FINALIZE_LEASE_SECONDS,
    exclude_job_ids: tuple[str, ...] = (),
) -> dict[str, object] | None:
    """Atomically lease one eligible job and return its payload and token."""
    now = datetime.now(UTC)
    now_text = now.isoformat()
    bounded_lease = max(30.0, min(float(lease_seconds), 3600.0))
    lease_expires_at = (now + timedelta(seconds=bounded_lease)).isoformat()
    legacy_stale_before = (now - timedelta(seconds=bounded_lease)).isoformat()
    token = uuid4().hex
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _terminate_ineligible_jobs(connection, now=now)
        id_sql = "AND id = ?" if job_id is not None else ""
        excluded = tuple(str(value) for value in exclude_job_ids if value)
        exclude_sql = ""
        if excluded:
            placeholders = ", ".join("?" for _ in excluded)
            exclude_sql = f"AND id NOT IN ({placeholders})"
        parameters: list[object] = [now_text, legacy_stale_before]
        if job_id is not None:
            parameters.append(job_id)
        parameters.extend(excluded)
        row = connection.execute(
            f"""
            SELECT id, user_id, kind, claim_key, payload_json, attempts,
                   created_at, updated_at
            FROM chat_finalize_jobs
            WHERE kind = 'ingest'
              AND (
                    status = 'pending'
                    OR (
                        status = 'running'
                        AND (
                            (lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                            OR (lease_expires_at IS NULL AND updated_at <= ?)
                        )
                    )
              )
              {id_sql}
              {exclude_sql}
            ORDER BY created_at ASC, rowid ASC
            LIMIT 1
            """,
            parameters,
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            """
            UPDATE chat_finalize_jobs
            SET status = 'running',
                attempts = attempts + 1,
                last_error = NULL,
                lease_token = ?,
                lease_expires_at = ?,
                updated_at = ?
            WHERE id = ?
              AND kind = 'ingest'
              AND (
                    status = 'pending'
                    OR (
                        status = 'running'
                        AND (
                            (lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                            OR (lease_expires_at IS NULL AND updated_at <= ?)
                        )
                    )
              )
            """,
            (
                token,
                lease_expires_at,
                now_text,
                str(row["id"]),
                now_text,
                legacy_stale_before,
            ),
        )
        if cursor.rowcount != 1:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "id": str(row["id"]),
            "user_id": str(row["user_id"]),
            "kind": str(row["kind"]),
            "claim_key": str(row["claim_key"]),
            "payload": payload,
            "attempts": int(row["attempts"] or 0) + 1,
            "lease_token": token,
            "lease_expires_at": lease_expires_at,
            "created_at": str(row["created_at"]),
            "updated_at": now_text,
        }


def mark_chat_finalize_job(
    store: ConnectionProvider,
    *,
    job_id: str,
    lease_token: str,
    status: str,
    last_error: str | None = None,
) -> bool:
    """Finish or requeue a lease using token compare-and-swap."""
    if status not in {"pending", "done", "failed"}:
        raise ValueError("invalid finalize job status")
    if not lease_token:
        raise ValueError("chat finalize lease token must not be empty")
    now = datetime.now(UTC).isoformat()
    terminal = status in {"done", "failed"}
    payload_sql = ", payload_json = ''" if terminal else ""
    with store._connect() as connection:
        cursor = connection.execute(
            f"""
            UPDATE chat_finalize_jobs
            SET status = ?,
                last_error = ?,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = ?
                {payload_sql}
            WHERE id = ? AND status = 'running' AND lease_token = ?
            """,
            (
                status,
                (last_error or "")[:500] or None,
                now,
                job_id,
                lease_token,
            ),
        )
        return cursor.rowcount == 1


def prune_chat_finalize_jobs(
    store: ConnectionProvider,
    *,
    keep_per_user: int = 5000,
) -> int:
    """Apply live-job bounds, then cap terminal rows per user."""
    bounded = max(1, int(keep_per_user))
    now = datetime.now(UTC)
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _terminate_ineligible_jobs(connection, now=now)
        _cap_nonterminal_jobs(connection, now=now)
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
