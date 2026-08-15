"""init_db 的版本化迁移行为测试。

核心契约：
- 新库创建后 PRAGMA user_version 到达最新版本；
- 重复 init_db 幂等，不重复执行历史迁移；
- 老库（user_version=0）首次 init_db 补齐缺列并回填遗留值，然后锁定版本；
- 版本已是最新时，即使表缺列也不再补（迁移只执行一次）。
"""

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from queue import Empty
import sqlite3

import pytest

import app.memory.store.migrations as memory_store_module
from app.knowledge.store import KnowledgeStore
from app.memory.store import MemoryStore
from app.schema_migrations import enable_wal_with_retry


_LATEST_MEMORY_SCHEMA_VERSION = 6


def _initialize_memory_store_in_process(db_path: str, start, results) -> None:
    start.wait()
    try:
        MemoryStore(db_path).init_db()
    except Exception as exc:  # pragma: no cover - asserted in the parent process
        results.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        results.put(("ok", ""))


def _user_version(db_path: str) -> int:
    store = MemoryStore(db_path)
    with store._connect() as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def test_enable_wal_retries_transient_lock_only(monkeypatch) -> None:
    class TransientConnection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, sql: str):
            assert sql == "PRAGMA journal_mode=WAL"
            self.calls += 1
            if self.calls < 3:
                raise sqlite3.OperationalError("database is locked")
            return self

        @staticmethod
        def fetchone():
            return ("wal",)

    connection = TransientConnection()
    monkeypatch.setattr("app.schema_migrations.time.sleep", lambda _: None)

    assert enable_wal_with_retry(connection) == "wal"
    assert connection.calls == 3

    class BrokenConnection:
        @staticmethod
        def execute(sql: str):
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        enable_wal_with_retry(BrokenConnection())


class TestMemorySchemaMigrations:
    def test_fresh_database_reaches_latest_version(self, tmp_path) -> None:
        db_path = str(tmp_path / "fresh-memory.db")
        MemoryStore(db_path).init_db()
        assert _user_version(db_path) == _LATEST_MEMORY_SCHEMA_VERSION

    def test_init_db_twice_is_idempotent(self, tmp_path) -> None:
        db_path = str(tmp_path / "twice-memory.db")
        store = MemoryStore(db_path)
        store.init_db()
        store.create_memory(user_id="default", content="重复初始化不应丢数据")
        store.init_db()
        assert _user_version(db_path) == _LATEST_MEMORY_SCHEMA_VERSION
        memory = store.get_memory(memory_id="unknown", user_id="default")
        assert memory is None
        rows = store.list_memories(user_id="default")
        assert len(rows) == 1

    def test_concurrent_fresh_database_initialization_is_serialized(self, tmp_path) -> None:
        db_path = str(tmp_path / "concurrent-memory.db")

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: MemoryStore(db_path).init_db(), range(16)))

        assert _user_version(db_path) == _LATEST_MEMORY_SCHEMA_VERSION
        with MemoryStore(db_path)._connect() as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_concurrent_processes_serialize_legacy_migration(self, tmp_path) -> None:
        db_path = str(tmp_path / "multiprocess-legacy-memory.db")
        legacy = MemoryStore(db_path)
        with legacy._connect() as connection:
            connection.execute(
                """
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    content TEXT,
                    type TEXT,
                    importance INTEGER,
                    confidence REAL,
                    source_message TEXT,
                    source_conversation_id TEXT,
                    embedding_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    archived INTEGER DEFAULT 0
                )
                """
            )
            connection.execute("PRAGMA user_version = 0")

        # ``spawn`` is available on macOS, Linux and Windows; the Event keeps
        # startup concurrent after each child has imported the test module.
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_initialize_memory_store_in_process,
                args=(db_path, start, results),
            )
            for _ in range(12)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0

        outcomes = []
        for _ in processes:
            try:
                outcomes.append(results.get(timeout=2))
            except Empty:
                pytest.fail("schema initializer process did not report a result")
        assert outcomes == [("ok", "")] * len(processes)
        assert _user_version(db_path) == _LATEST_MEMORY_SCHEMA_VERSION

    def test_legacy_database_migrates_columns_and_backfills_once(self, tmp_path) -> None:
        db_path = tmp_path / "legacy-migrate.db"
        legacy = MemoryStore(str(db_path))
        with legacy._connect() as connection:
            connection.execute(
                """
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    content TEXT,
                    type TEXT,
                    importance INTEGER,
                    confidence REAL,
                    source_message TEXT,
                    source_conversation_id TEXT,
                    embedding_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    archived INTEGER DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                INSERT INTO memories (id, user_id, content, type, importance, confidence,
                                      source_message, embedding_json,
                                      created_at, updated_at, archived)
                VALUES ('legacy-1', 'default', '旧记录', 'preference', 5, 0.8,
                        'digest_memories:reflection', '[0.1, 0.2]', 'now', 'now', 0)
                """
            )

        MemoryStore(str(db_path)).init_db()

        assert _user_version(str(db_path)) == _LATEST_MEMORY_SCHEMA_VERSION
        store = MemoryStore(str(db_path))
        with store._connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            }
            for required in (
                "valence",
                "arousal",
                "origin",
                "status",
                "sensitivity",
                "temporal_subject",
                "temporal_predicate",
                "embedding_space_id",
                "revision",
            ):
                assert required in columns, f"迁移后缺少列 {required}"
        memory = store.get_memory(memory_id="legacy-1", user_id="default")
        assert memory is not None
        assert memory.origin == "agent_derived"  # 遗留值回填生效
        assert memory.status == "dynamic"
        assert memory.embedding_json == "[0.1, 0.2]"
        assert memory.embedding_space_id is None  # 旧向量不按当前配置猜空间

    def test_already_migrated_database_does_not_rerun_migration(self, tmp_path) -> None:
        """v1 老库只运行新增的 v2/v3/v4，不重跑历史迁移。"""
        db_path = tmp_path / "locked-memory.db"
        legacy = MemoryStore(str(db_path))
        with legacy._connect() as connection:
            connection.execute(
                """
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    content TEXT,
                    type TEXT,
                    importance INTEGER,
                    confidence REAL,
                    source_message TEXT,
                    source_conversation_id TEXT,
                    embedding_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    archived INTEGER DEFAULT 0
                )
                """
            )
            connection.execute("PRAGMA user_version = 1")

        # 只运行 v2/v3/v4：应补空间、revision 和 claim 表，但不重跑 v1。
        with MemoryStore(str(db_path))._connect() as connection:
            MemoryStore._run_migrations(connection)
            assert (
                int(connection.execute("PRAGMA user_version").fetchone()[0])
                == _LATEST_MEMORY_SCHEMA_VERSION
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            }
            assert "valence" not in columns  # 迁移未重跑
            assert "embedding_space_id" in columns
            assert "revision" in columns
            assert connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'chat_side_effect_claims'"
            ).fetchone()

    def test_v2_migration_merges_duplicate_active_core_sections_before_unique_index(
        self,
        tmp_path,
    ) -> None:
        db_path = str(tmp_path / "v2-duplicate-core.db")
        store = MemoryStore(db_path)
        store.init_db()
        with store._connect() as connection:
            connection.execute("DROP INDEX ux_core_memory_user_section_active")
            connection.execute("ALTER TABLE memories DROP COLUMN revision")
            connection.execute(
                "ALTER TABLE core_memory_sections DROP COLUMN revision"
            )
            connection.execute(
                "ALTER TABLE core_memory_section_history DROP COLUMN revision"
            )
            connection.executemany(
                """
                INSERT INTO core_memory_sections (
                    id, user_id, section, content, evidence_memory_ids_json,
                    confidence, version, created_at, updated_at, archived
                )
                VALUES (?, 'alice', 'preferences', ?, ?, ?, ?, ?, ?, 0)
                """,
                [
                    (
                        "core-old",
                        "旧偏好",
                        '["memory-old", "memory-shared"]',
                        0.7,
                        1,
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                    (
                        "core-new",
                        "新偏好",
                        '["memory-new", "memory-shared"]',
                        0.9,
                        2,
                        "2026-01-02T00:00:00+00:00",
                        "2026-01-02T00:00:00+00:00",
                    ),
                ],
            )
            connection.execute("PRAGMA user_version = 2")

        store.init_db()

        assert _user_version(db_path) == _LATEST_MEMORY_SCHEMA_VERSION
        with store._connect() as connection:
            active = connection.execute(
                """
                SELECT * FROM core_memory_sections
                WHERE user_id = 'alice' AND section = 'preferences' AND archived = 0
                """
            ).fetchall()
            archived = connection.execute(
                """
                SELECT * FROM core_memory_sections
                WHERE user_id = 'alice' AND section = 'preferences' AND archived = 1
                """
            ).fetchall()
            history = connection.execute(
                """
                SELECT * FROM core_memory_section_history
                WHERE user_id = 'alice' AND section = 'preferences'
                """
            ).fetchall()
            assert len(active) == 1
            assert active[0]["id"] == "core-new"
            assert active[0]["content"] == "新偏好"
            assert set(json.loads(active[0]["evidence_memory_ids_json"])) == {
                "memory-old",
                "memory-new",
                "memory-shared",
            }
            assert int(active[0]["revision"]) >= 2
            assert len(archived) == 1
            assert len(history) == 2

            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO core_memory_sections (
                        id, user_id, section, content, evidence_memory_ids_json,
                        confidence, version, created_at, updated_at, archived, revision
                    )
                    VALUES (
                        'core-third', 'alice', 'preferences', '重复', '[]',
                        0.5, 1, 'now', 'now', 0, 1
                    )
                    """
                )

    def test_future_database_version_is_rejected_before_creating_tables(
        self,
        tmp_path,
    ) -> None:
        db_path = str(tmp_path / "future-memory.db")
        store = MemoryStore(db_path)
        with store._connect() as connection:
            connection.execute("PRAGMA user_version = 99")

        with pytest.raises(RuntimeError, match="newer than supported"):
            store.init_db()

        with store._connect() as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
            ).fetchone()
        assert table is None

    @pytest.mark.parametrize(
        "migrations",
        [
            [(2, lambda connection: None), (1, lambda connection: None)],
            [(1, lambda connection: None), (1, lambda connection: None)],
            [(True, lambda connection: None)],
        ],
    )
    def test_invalid_migration_table_is_rejected(
        self,
        tmp_path,
        monkeypatch,
        migrations,
    ) -> None:
        db_path = str(tmp_path / "invalid-memory-migrations.db")
        monkeypatch.setattr(
            memory_store_module,
            "_MEMORY_SCHEMA_MIGRATIONS",
            migrations,
        )
        with MemoryStore(db_path)._connect() as connection:
            with pytest.raises(RuntimeError, match="migration versions"):
                MemoryStore._run_migrations(connection)


class TestKnowledgeSchemaMigrations:
    def test_fresh_database_reaches_latest_version(self, tmp_path) -> None:
        db_path = str(tmp_path / "fresh-knowledge.db")
        KnowledgeStore(db_path, max_document_bytes=1024 * 1024).init_db()
        store = KnowledgeStore(db_path, max_document_bytes=1024 * 1024)
        with store._connect() as connection:
            assert (
                int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
            )

    def test_concurrent_fresh_database_initialization_is_serialized(self, tmp_path) -> None:
        db_path = str(tmp_path / "concurrent-knowledge.db")

        def initialize(_: int) -> None:
            KnowledgeStore(db_path, max_document_bytes=1024 * 1024).init_db()

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(initialize, range(16)))

        with KnowledgeStore(db_path, max_document_bytes=1024 * 1024)._connect() as connection:
            assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_legacy_database_gets_source_document_ref(self, tmp_path) -> None:
        db_path = tmp_path / "legacy-knowledge.db"
        legacy = KnowledgeStore(str(db_path), max_document_bytes=1024 * 1024)
        with legacy._connect() as connection:
            connection.execute(
                """
                CREATE TABLE knowledge_documents (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT 'text/markdown',
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'active',
                    current_version_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                )
                """
            )

        KnowledgeStore(str(db_path), max_document_bytes=1024 * 1024).init_db()

        store = KnowledgeStore(str(db_path), max_document_bytes=1024 * 1024)
        with store._connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(knowledge_documents)"
                ).fetchall()
            }
            for required in (
                "source_document_ref",
                "tags_json",
                "metadata_json",
                "detected_sensitivity",
                "sensitivity_override_confirmed",
            ):
                assert required in columns, f"迁移后缺少列 {required}"
            assert (
                int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
            )

    def test_v1_embedding_rows_migrate_to_unknown_space(self, tmp_path) -> None:
        db_path = str(tmp_path / "v1-knowledge-embeddings.db")
        store = KnowledgeStore(db_path, max_document_bytes=1024 * 1024)
        store.init_db()
        upload = store.begin_upload("alice", "旧知识向量")
        store.append_upload("alice", upload.id, 0, "旧向量正文")
        committed = store.commit_upload("alice", upload.id, 1)
        chunks = store.list_chunks_for_embedding(
            "alice",
            committed.version.ref,
        )
        store.replace_chunk_embeddings(
            "alice",
            committed.version.ref,
            model="legacy-model",
            embedding_space_id="legacy-known-space",
            vectors={chunk.ref: [1.0, 0.0] for chunk in chunks},
            total_chunks=len(chunks),
        )

        # Recreate the exact v1 shape: it had vectors and model metadata but
        # no trustworthy vector-space identifier.
        with store._connect() as connection:
            connection.execute("DROP INDEX idx_knowledge_embeddings_user_space")
            connection.execute(
                "ALTER TABLE knowledge_chunk_embeddings "
                "DROP COLUMN embedding_space_id"
            )
            connection.execute(
                "ALTER TABLE knowledge_versions DROP COLUMN embedding_space_id"
            )
            connection.execute("PRAGMA user_version = 1")

        store.init_db()

        with store._connect() as connection:
            version = connection.execute(
                "SELECT embedding_space_id FROM knowledge_versions WHERE id = ?",
                (committed.version.id,),
            ).fetchone()
            embedding = connection.execute(
                """
                SELECT embedding_space_id, vector_json
                FROM knowledge_chunk_embeddings WHERE version_id = ?
                """,
                (committed.version.id,),
            ).fetchone()
            assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2

        assert version["embedding_space_id"] == ""
        assert embedding["embedding_space_id"] == ""
        assert embedding["vector_json"] == "[1.0,0.0]"
        assert store.search_chunks_by_embedding(
            "alice",
            [1.0, 0.0],
            embedding_space_id="current-space",
        ) == []

    def test_future_database_version_is_rejected_before_creating_tables(
        self,
        tmp_path,
    ) -> None:
        db_path = str(tmp_path / "future-knowledge.db")
        store = KnowledgeStore(db_path, max_document_bytes=1024 * 1024)
        with store._connect() as connection:
            connection.execute("PRAGMA user_version = 99")

        with pytest.raises(RuntimeError, match="newer than supported"):
            store.init_db()

        with store._connect() as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'knowledge_documents'"
            ).fetchone()
        assert table is None
