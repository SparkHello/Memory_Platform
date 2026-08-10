from __future__ import annotations

import errno
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import disk_capacity
from app.auth.tokens import AuthTokenStore
from app.config import Settings, get_settings
from app.disk_capacity import DiskCapacityError
from app.knowledge.retrieval import KnowledgeEmbeddingIndexer
from app.memory.search import EmbeddingClient


MIB = 1024 * 1024


def _disk_usage(*, total: int, free: int):
    return SimpleNamespace(total=total, used=total - free, free=free)


def _assert_safe_507(response, *paths: str) -> None:
    assert response.status_code == 507
    assert response.json()["detail"]["code"] == "insufficient_storage"
    for path in paths:
        assert path not in response.text


def test_default_reserves_adapt_to_small_volume_without_false_unready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=str(tmp_path / "memory.db"),
        KNOWLEDGE_DATABASE_PATH=str(tmp_path / "knowledge.db"),
        AUTH_DATABASE_PATH=str(tmp_path / "auth.db"),
    )
    monkeypatch.setattr(
        disk_capacity.shutil,
        "disk_usage",
        lambda _path: _disk_usage(total=64 * MIB, free=8 * MIB),
    )

    capacity = disk_capacity.disk_capacity_for_path(settings.database_path, settings)

    assert capacity.soft_reserve_bytes == 4 * MIB
    assert capacity.hard_reserve_bytes == 1 * MIB
    assert disk_capacity.disk_readiness_code(settings) == ""


def test_explicit_reserve_order_is_validated() -> None:
    with pytest.raises(ValidationError, match="DISK_SOFT_RESERVE_BYTES"):
        Settings(
            _env_file=None,
            DISK_SOFT_RESERVE_BYTES=1,
            DISK_HARD_RESERVE_BYTES=2,
        )


def test_readyz_reports_only_safe_disk_low_reason(
    client,
    memory_store,
    knowledge_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        disk_capacity.shutil,
        "disk_usage",
        lambda _path: _disk_usage(total=10 * 1024 * MIB, free=32 * MIB),
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "code": "disk_low"}
    assert memory_store.database_path not in response.text
    assert knowledge_store.database_path not in response.text


def test_hard_reserve_rejects_writes_but_keeps_read_api_available(
    client,
    auth_headers,
    memory_store,
    knowledge_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        disk_capacity.shutil,
        "disk_usage",
        lambda _path: _disk_usage(total=10 * 1024 * MIB, free=8 * MIB),
    )

    rejected = client.post(
        "/memories",
        headers=auth_headers,
        json={"content": "must not be parsed or persisted"},
    )
    readable = client.get("/memories", headers=auth_headers)

    _assert_safe_507(
        rejected,
        memory_store.database_path,
        knowledge_store.database_path,
    )
    assert readable.status_code == 200
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.headers["x-content-type-options"] == "nosniff"


def test_request_spool_enospc_uses_stable_507(
    client,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FullSpool:
        def write(self, _body: bytes) -> None:
            raise OSError(errno.ENOSPC, "injected path must not leak")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "app.request_limits.tempfile.SpooledTemporaryFile",
        lambda **_kwargs: FullSpool(),
    )

    response = client.post(
        "/memories",
        headers=auth_headers,
        json={"content": "spool failure"},
    )

    _assert_safe_507(response, "injected path must not leak")


def test_auth_last_used_full_is_507_but_other_auth_db_errors_remain_401(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AuthTokenStore(get_settings().auth_database_path)
    created = store.create_token(name="disk test", user_id="default", role="chat")
    headers = {"Authorization": f"Bearer {created.token}"}

    def full(_self, _token: str):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(AuthTokenStore, "authenticate", full)
    full_response = client.get("/v1/models", headers=headers)
    assert full_response.status_code == 507
    assert full_response.json()["error"]["code"] == (
        "memory_gateway_insufficient_storage"
    )

    def unavailable(_self, _token: str):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(AuthTokenStore, "authenticate", unavailable)
    unavailable_response = client.get("/v1/models", headers=headers)
    assert unavailable_response.status_code == 401


def test_token_create_sqlite_full_is_stable_507(
    client,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def full(_self, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(AuthTokenStore, "create_token", full)

    response = client.post(
        "/auth/tokens",
        headers=auth_headers,
        json={"name": "will fail", "role": "chat"},
    )

    _assert_safe_507(response, "database or disk is full")


def test_memory_core_sqlite_full_is_507_and_preserves_previous_value(
    client,
    auth_headers,
    memory_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_store.upsert_core_memory_section(
        user_id="default",
        section="profile",
        content="original",
        evidence_memory_ids=[],
        confidence=0.9,
    )

    def full(**_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(memory_store, "upsert_core_memory_section", full)
    response = client.patch(
        "/memories/core/profile",
        headers=auth_headers,
        json={"content": "must roll back"},
    )

    _assert_safe_507(response, memory_store.database_path)
    assert memory_store.get_core_memory_section(
        user_id="default", section="profile"
    ).content == "original"


def test_knowledge_upload_sqlite_full_is_not_hidden_as_503(
    client,
    auth_headers,
    knowledge_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def full(**_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(knowledge_store, "begin_upload", full)
    response = client.post(
        "/knowledge/uploads",
        headers=auth_headers,
        json={"title": "full disk", "content_type": "text/plain"},
    )

    _assert_safe_507(response, knowledge_store.database_path)


def test_memory_restore_sqlite_full_returns_507_and_rolls_back_all_partitions(
    client,
    auth_headers,
    memory_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "version": 3,
        "user_id": "source-user",
        "memory_spaces": [{"id": "space-a", "name": "Atomic"}],
        "memories": [
            {"id": "memory-a", "content": "first", "space_ids": ["space-a"]},
            {"id": "memory-b", "content": "second", "space_ids": ["space-a"]},
        ],
        "deleted_memories": [],
        "recent_context_summaries": [],
        "conversation_branch_nodes": [],
    }
    original = memory_store._import_prepared_memory_record_on_connection
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("database or disk is full")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        memory_store,
        "_import_prepared_memory_record_on_connection",
        fail_on_second,
    )
    response = client.post(
        "/memories/restore",
        headers=auth_headers,
        json={"data": payload},
    )

    _assert_safe_507(response, memory_store.database_path)
    assert memory_store.list_memories(user_id="default") == []
    assert memory_store.list_memory_spaces(user_id="default") == []


def test_knowledge_restore_sqlite_full_returns_507_and_rolls_back_document(
    client,
    auth_headers,
    knowledge_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = knowledge_store.begin_upload("alice", "source")
    knowledge_store.append_upload("alice", upload.id, 0, "synthetic restore body")
    knowledge_store.commit_upload("alice", upload.id, 1)
    exported = knowledge_store.export_user("alice")

    def full(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(knowledge_store, "_index_version_in_connection", full)
    response = client.post(
        "/knowledge/restore",
        headers={**auth_headers, "X-User-Id": "bob"},
        json={"data": exported},
    )

    _assert_safe_507(response, knowledge_store.database_path)
    assert knowledge_store.list_documents("bob", include_sensitive=True) == []


class _SyntheticEmbedding(EmbeddingClient):
    model = "synthetic-embedding"
    embedding_space_id = "synthetic-space"
    allow_sensitive_egress = False

    async def embed(self, _text: str) -> list[float] | None:
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_knowledge_index_sqlite_full_raises_capacity_error_and_no_vectors(
    knowledge_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = knowledge_store.begin_upload("alice", "index source")
    knowledge_store.append_upload("alice", upload.id, 0, "indexable synthetic body")
    committed = knowledge_store.commit_upload("alice", upload.id, 1)

    def full(**_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(knowledge_store, "replace_chunk_embeddings", full)
    indexer = KnowledgeEmbeddingIndexer(
        store=knowledge_store,
        embedding_client=_SyntheticEmbedding(),
    )

    with pytest.raises(DiskCapacityError):
        await indexer.index_version(
            user_id="alice",
            version_ref=committed.version.ref,
        )

    with knowledge_store._connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_chunk_embeddings WHERE user_id = ?",
            ("alice",),
        ).fetchone()[0]
    assert count == 0
    version = knowledge_store.get_version("alice", version_id=committed.version.ref)
    assert version.embedding_status == "failed"
    assert version.embedding_error == "insufficient storage"
