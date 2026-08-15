"""Versioned SQLite migrations for memory.db."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import sqlite3

from app.memory.store import conversation
from app.memory.store import core_memory
from app.memory.store import decision_logs
from app.memory.store import schema_ensure
from app.schema_versions import MEMORY_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Schema migrations (PRAGMA user_version)
#
# 每次启动只执行尚未应用的版本。v1 汇总了历史遗留的一次性修复；v2
# 为向量增加显式空间标识，且故意不回填旧向量；v3 增加持久化
# revision，并在建立 Core Memory 单活跃唯一索引前合并历史重复行；v4
# 增加跨 worker/进程重试仍有效、且不保存正文的聊天副作用 claim。


def _memory_migration_v1(connection: sqlite3.Connection) -> None:
    schema_ensure._ensure_memories_usage_columns(connection)
    decision_logs._ensure_decision_logs_user_id(connection)
    core_memory._ensure_core_memory_sections_columns(connection)
    conversation._ensure_recent_context_summary_columns(connection)
    conversation._archive_duplicate_recent_context_summaries(connection)


def _memory_migration_v2(connection: sqlite3.Connection) -> None:
    schema_ensure._ensure_memories_embedding_space_column(connection)


def _memory_migration_v3(connection: sqlite3.Connection) -> None:
    schema_ensure._ensure_revision_columns(connection)
    core_memory._merge_duplicate_active_core_sections(connection)
    core_columns = connection.execute(
        "PRAGMA table_info(core_memory_sections)"
    ).fetchall()
    if core_columns:
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_core_memory_user_section_active
            ON core_memory_sections(user_id, section)
            WHERE archived = 0
            """
        )


def _memory_migration_v4(connection: sqlite3.Connection) -> None:
    # 激活与近期上下文通过本表在 TTL 内跨 worker/重启去重；ingest 使用独立
    # durable outbox 作为崩溃恢复与终态幂等权威。
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_side_effect_claims (
            kind TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (kind, key_hash)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_side_effect_claims_expiry
        ON chat_side_effect_claims(expires_at)
        """
    )


def _memory_migration_v5(connection: sqlite3.Connection) -> None:
    # Durable finalize outbox: survives process crash between "answer complete"
    # and long-term ingest. Payload is capped by chat gateway limits; no secrets.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_finalize_jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            claim_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN ('pending', 'running', 'done', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(kind, claim_key)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_finalize_jobs_status
        ON chat_finalize_jobs(status, updated_at)
        """
    )


def _memory_migration_v6(connection: sqlite3.Connection) -> None:
    """Space workbench metadata: color, description, sort_order."""
    table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'memory_spaces'
        """
    ).fetchone()
    if table_exists is None:
        # Partial fixture databases used by migration unit tests may only
        # include the memories table; skip metadata columns until full schema.
        return
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(memory_spaces)")
    }
    if "color" not in columns:
        connection.execute("ALTER TABLE memory_spaces ADD COLUMN color TEXT")
    if "description" not in columns:
        connection.execute("ALTER TABLE memory_spaces ADD COLUMN description TEXT")
    if "sort_order" not in columns:
        connection.execute(
            "ALTER TABLE memory_spaces ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_spaces_user_sort
        ON memory_spaces(user_id, archived, sort_order ASC, name ASC)
        """
    )


def _memory_migration_v7(connection: sqlite3.Connection) -> None:
    """Add lease ownership and enforce bounded, body-free terminal jobs."""
    table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'chat_finalize_jobs'
        """
    ).fetchone()
    if table_exists is None:
        return
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(chat_finalize_jobs)")
    }
    if "lease_token" not in columns:
        connection.execute("ALTER TABLE chat_finalize_jobs ADD COLUMN lease_token TEXT")
    if "lease_expires_at" not in columns:
        connection.execute(
            "ALTER TABLE chat_finalize_jobs ADD COLUMN lease_expires_at TEXT"
        )

    now = datetime.now(UTC)
    now_text = now.isoformat()
    age_cutoff = (now - timedelta(hours=24)).isoformat()
    # Terminal rows do not need the copied chat turn. Also terminate rows that
    # can no longer be attempted before a v7 worker can claim them.
    connection.execute(
        """
        UPDATE chat_finalize_jobs
        SET payload_json = '', lease_token = NULL, lease_expires_at = NULL
        WHERE status IN ('done', 'failed')
        """
    )
    connection.execute(
        "UPDATE chat_finalize_jobs SET attempts = 8 WHERE attempts > 8"
    )
    connection.execute(
        """
        UPDATE chat_finalize_jobs
        SET status = 'failed', payload_json = '',
            lease_token = NULL, lease_expires_at = NULL,
            last_error = COALESCE(last_error, 'max_attempts_exceeded'),
            updated_at = ?
        WHERE status IN ('pending', 'running') AND attempts >= 8
        """,
        (now_text,),
    )
    connection.execute(
        """
        UPDATE chat_finalize_jobs
        SET status = 'failed', payload_json = '',
            lease_token = NULL, lease_expires_at = NULL,
            last_error = COALESCE(last_error, 'max_age_exceeded'),
            updated_at = ?
        WHERE status IN ('pending', 'running') AND created_at <= ?
        """,
        (now_text, age_cutoff),
    )

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
        user_id = str(row[1])
        counts[user_id] = counts.get(user_id, 0) + 1
        if counts[user_id] > 100:
            overflow.append(str(row[0]))
    for offset in range(0, len(overflow), 500):
        batch = overflow[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        connection.execute(
            f"""
            UPDATE chat_finalize_jobs
            SET status = 'failed', payload_json = '',
                lease_token = NULL, lease_expires_at = NULL,
                last_error = COALESCE(last_error, 'queue_limit_exceeded'),
                updated_at = ?
            WHERE id IN ({placeholders})
              AND status IN ('pending', 'running')
            """,
            (now_text, *batch),
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_finalize_jobs_claim
        ON chat_finalize_jobs(status, lease_expires_at, created_at)
        """
    )


_MEMORY_SCHEMA_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _memory_migration_v1),
    (2, _memory_migration_v2),
    (3, _memory_migration_v3),
    (4, _memory_migration_v4),
    (5, _memory_migration_v5),
    (6, _memory_migration_v6),
    (7, _memory_migration_v7),
]

if _MEMORY_SCHEMA_MIGRATIONS[-1][0] != MEMORY_SCHEMA_VERSION:
    raise RuntimeError(
        "app.schema_versions.MEMORY_SCHEMA_VERSION 与 memory 迁移列表不一致"
    )
