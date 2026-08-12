"""Versioned SQLite migrations for memory.db."""
from __future__ import annotations

from collections.abc import Callable
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


_MEMORY_SCHEMA_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _memory_migration_v1),
    (2, _memory_migration_v2),
    (3, _memory_migration_v3),
    (4, _memory_migration_v4),
    (5, _memory_migration_v5),
]

if _MEMORY_SCHEMA_MIGRATIONS[-1][0] != MEMORY_SCHEMA_VERSION:
    raise RuntimeError(
        "app.schema_versions.MEMORY_SCHEMA_VERSION 与 memory 迁移列表不一致"
    )

