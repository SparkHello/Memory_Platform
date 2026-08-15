"""Schema migrations (PRAGMA user_version) for knowledge.db.

v1 汇总历史遗留的一次性列补齐（老库升级路径）；新库建表已含全部列，
v1 对空表运行无副作用。v2 给派生 chunk embedding 增加不可猜测的
空间标识；遗留向量保持空值，只有重新生成后才进入已知空间。
"""

from __future__ import annotations

import sqlite3

from app.knowledge.store import schema as _schema
from app.schema_migrations import SchemaMigration
from app.schema_versions import KNOWLEDGE_SCHEMA_VERSION


def _knowledge_migration_v1(connection: sqlite3.Connection) -> None:
    _schema._ensure_documents_source_document_ref(connection)
    _schema._ensure_document_metadata_columns(connection)
    _schema._ensure_document_sensitivity_columns(connection)
    _schema._ensure_version_embedding_columns(connection)
    _schema._ensure_upload_metadata_columns(connection)


def _knowledge_migration_v2(connection: sqlite3.Connection) -> None:
    _schema._ensure_embedding_space_columns(connection)


_KNOWLEDGE_SCHEMA_MIGRATIONS: list[SchemaMigration] = [
    (1, _knowledge_migration_v1),
    (2, _knowledge_migration_v2),
]

if _KNOWLEDGE_SCHEMA_MIGRATIONS[-1][0] != KNOWLEDGE_SCHEMA_VERSION:
    raise RuntimeError(
        "app.schema_versions.KNOWLEDGE_SCHEMA_VERSION 与 knowledge 迁移列表不一致"
    )
