"""SQLite schema creation for memory.db."""
from __future__ import annotations

import sqlite3


def create_tables(connection: sqlite3.Connection) -> None:
    """幂等建表。老库已存在的表会被跳过；新列由 _run_migrations 补齐。"""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            content TEXT,
            type TEXT,
            importance INTEGER,
            confidence REAL,
            valence REAL DEFAULT 0.5,
            arousal REAL DEFAULT 0.3,
            source_message TEXT,
            source_conversation_id TEXT,
            origin TEXT DEFAULT 'user_asserted',
            embedding_json TEXT,
            embedding_space_id TEXT,
            last_used_at TEXT,
            usage_count REAL DEFAULT 0.0,
            stability TEXT DEFAULT 'stable',
            valid_from TEXT,
            valid_until TEXT,
            review_after TEXT,
            sensitivity TEXT DEFAULT 'normal',
            evidence_memory_ids_json TEXT,
            topics_json TEXT,
            entities_json TEXT,
            temporal_subject TEXT,
            temporal_predicate TEXT,
            status TEXT DEFAULT 'dynamic',
            digested INTEGER DEFAULT 0,
            decay_lambda REAL,
            supersedes TEXT,
            superseded_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            archived_at TEXT,
            archived INTEGER DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_spaces (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            name TEXT,
            normalized_name TEXT,
            created_at TEXT,
            updated_at TEXT,
            archived INTEGER DEFAULT 0,
            UNIQUE(user_id, normalized_name)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_space_links (
            user_id TEXT,
            memory_id TEXT,
            space_id TEXT,
            created_at TEXT,
            PRIMARY KEY(user_id, memory_id, space_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_decision_logs (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            conversation_id TEXT,
            candidate_json TEXT,
            decision TEXT,
            reason TEXT,
            created_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS core_memory_sections (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            section TEXT,
            content TEXT,
            evidence_memory_ids_json TEXT,
            confidence REAL,
            version INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            archived INTEGER DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS core_memory_section_history (
            id TEXT PRIMARY KEY,
            core_memory_section_id TEXT,
            user_id TEXT,
            section TEXT,
            content TEXT,
            evidence_memory_ids_json TEXT,
            confidence REAL,
            version INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            replaced_at TEXT,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recent_context_summaries (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            conversation_id TEXT,
            summary TEXT,
            compressed_summary TEXT DEFAULT '',
            recent_turns_json TEXT DEFAULT '[]',
            turn_count INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            archived INTEGER DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_branch_nodes (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            conversation_id TEXT,
            history_fingerprint TEXT NOT NULL,
            parent_history_fingerprint TEXT DEFAULT '',
            turn_fingerprint TEXT NOT NULL,
            assistant_digest TEXT NOT NULL,
            summary TEXT DEFAULT '',
            compressed_summary TEXT DEFAULT '',
            recent_turns_json TEXT DEFAULT '[]',
            turn_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived INTEGER DEFAULT 0
        )
        """
    )


def create_indexes(connection: sqlite3.Connection) -> None:
    """幂等建索引。必须在 _run_migrations 之后执行，
    因为部分索引引用了迁移补齐的列（如 memories.temporal_subject）。"""
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_user_archived ON memories(user_id, archived)"
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memories_user_archived_importance_updated
        ON memories(user_id, archived, importance DESC, updated_at DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memories_user_archived_archived_updated
        ON memories(user_id, archived, archived_at DESC, updated_at DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memories_temporal_key
        ON memories(user_id, temporal_subject, temporal_predicate, archived)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_spaces_user_archived
        ON memory_spaces(user_id, archived, updated_at DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_space_links_user_space
        ON memory_space_links(user_id, space_id, memory_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_decision_logs_user_conversation
        ON memory_decision_logs(user_id, conversation_id, created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_core_memory_sections_user_archived
        ON core_memory_sections(user_id, archived, section)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_core_memory_user_section_active
        ON core_memory_sections(user_id, section)
        WHERE archived = 0
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_core_memory_history_user_section
        ON core_memory_section_history(user_id, section, replaced_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recent_context_user_updated
        ON recent_context_summaries(user_id, archived, updated_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recent_context_user_conversation_archived_updated
        ON recent_context_summaries(user_id, conversation_id, archived, updated_at DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recent_context_user_archived_updated_desc
        ON recent_context_summaries(user_id, archived, updated_at DESC)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_recent_context_user_conversation_active
        ON recent_context_summaries(user_id, conversation_id)
        WHERE archived = 0 AND conversation_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_recent_context_user_global_active
        ON recent_context_summaries(user_id)
        WHERE archived = 0 AND conversation_id IS NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_branch_user_history
        ON conversation_branch_nodes(user_id, history_fingerprint)
        WHERE archived = 0
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_branch_user_parent
        ON conversation_branch_nodes(
            user_id, parent_history_fingerprint, archived, updated_at DESC
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_branch_user_conversation
        ON conversation_branch_nodes(
            user_id, conversation_id, archived, updated_at DESC
        )
        """
    )

