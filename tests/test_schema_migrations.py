"""init_db 的版本化迁移行为测试。

核心契约：
- 新库创建后 PRAGMA user_version 到达最新版本；
- 重复 init_db 幂等，不重复执行历史迁移；
- 老库（user_version=0）首次 init_db 补齐缺列并回填遗留值，然后锁定版本；
- 版本已是最新时，即使表缺列也不再补（迁移只执行一次）。
"""

from app.knowledge.store import KnowledgeStore
from app.memory.store import MemoryStore


def _user_version(db_path: str) -> int:
    store = MemoryStore(db_path)
    with store._connect() as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


class TestMemorySchemaMigrations:
    def test_fresh_database_reaches_latest_version(self, tmp_path) -> None:
        db_path = str(tmp_path / "fresh-memory.db")
        MemoryStore(db_path).init_db()
        assert _user_version(db_path) == 1

    def test_init_db_twice_is_idempotent(self, tmp_path) -> None:
        db_path = str(tmp_path / "twice-memory.db")
        store = MemoryStore(db_path)
        store.init_db()
        store.create_memory(user_id="default", content="重复初始化不应丢数据")
        store.init_db()
        assert _user_version(db_path) == 1
        memory = store.get_memory(memory_id="unknown", user_id="default")
        assert memory is None
        rows = store.list_memories(user_id="default")
        assert len(rows) == 1

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
                                      source_message, created_at, updated_at, archived)
                VALUES ('legacy-1', 'default', '旧记录', 'preference', 5, 0.8,
                        'digest_memories:reflection', 'now', 'now', 0)
                """
            )

        MemoryStore(str(db_path)).init_db()

        assert _user_version(str(db_path)) == 1
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
            ):
                assert required in columns, f"迁移后缺少列 {required}"
        memory = store.get_memory(memory_id="legacy-1", user_id="default")
        assert memory is not None
        assert memory.origin == "agent_derived"  # 遗留值回填生效
        assert memory.status == "dynamic"

    def test_already_migrated_database_does_not_rerun_migration(self, tmp_path) -> None:
        """版本已锁定为 1 的老库不再补列（迁移只执行一次）。"""
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

        # 只运行迁移步骤：版本已是最新时应被跳过，缺列保持不变。
        with MemoryStore(str(db_path))._connect() as connection:
            MemoryStore._run_migrations(connection)
            assert (
                int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            }
            assert "valence" not in columns  # 迁移未重跑


class TestKnowledgeSchemaMigrations:
    def test_fresh_database_reaches_latest_version(self, tmp_path) -> None:
        db_path = str(tmp_path / "fresh-knowledge.db")
        KnowledgeStore(db_path, max_document_bytes=1024 * 1024).init_db()
        store = KnowledgeStore(db_path, max_document_bytes=1024 * 1024)
        with store._connect() as connection:
            assert (
                int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1
            )

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
                int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1
            )
