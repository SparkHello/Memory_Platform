from __future__ import annotations

from pathlib import Path

import pytest

from app.knowledge.retrieval import (
    KnowledgeEmbeddingIndexer,
    KnowledgeRetrievalService,
)
from app.knowledge.store import KnowledgeStore
from app.memory.search import EmbeddingClient


class FakeEmbeddingClient(EmbeddingClient):
    model = "fake-embedding-v1"
    embedding_space_id = "knowledge-space-a"
    allow_sensitive_egress = False

    async def embed(self, text: str) -> list[float] | None:
        if "semantic-query" in text:
            return [1.0, 0.0]
        if "VECTOR-TARGET" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


class FailingQueryEmbeddingClient(FakeEmbeddingClient):
    async def embed(self, text: str) -> list[float] | None:
        raise RuntimeError("provider unavailable")


class UnknownSpaceEmbeddingClient(FakeEmbeddingClient):
    embedding_space_id = ""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float] | None:
        self.calls += 1
        return await super().embed(text)


def _commit(store: KnowledgeStore, text: str, *, title: str, tags=None, metadata=None):
    session = store.begin_upload(
        "alice",
        title,
        tags=tags,
        metadata=metadata,
    )
    store.append_upload("alice", session.id, 0, text)
    return store.commit_upload("alice", session.id, 1)


@pytest.fixture
def knowledge_store(tmp_path: Path) -> KnowledgeStore:
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    store.init_db()
    return store


@pytest.mark.asyncio
async def test_chunk_embeddings_add_semantic_recall_and_expose_channels(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(
        knowledge_store,
        "# Vector document\n\nVECTOR-TARGET contains wording unrelated to the query.",
        title="向量目标",
    )
    client = FakeEmbeddingClient()
    indexed = await KnowledgeEmbeddingIndexer(
        store=knowledge_store,
        embedding_client=client,
    ).index_version(user_id="alice", version_ref=result.version.ref)

    hits = await KnowledgeRetrievalService(
        store=knowledge_store,
        embedding_client=client,
        min_cosine=0.8,
    ).search_chunks(user_id="alice", query="semantic-query", limit=5)

    assert indexed["status"] == "ready"
    version = knowledge_store.get_version(
        "alice",
        version_id=result.version.ref,
    )
    assert version.embedding_space_id == "knowledge-space-a"
    with knowledge_store._connect() as connection:
        spaces = {
            row["embedding_space_id"]
            for row in connection.execute(
                "SELECT embedding_space_id FROM knowledge_chunk_embeddings "
                "WHERE user_id = ? AND version_id = ?",
                ("alice", result.version.id),
            ).fetchall()
        }
    assert spaces == {"knowledge-space-a"}
    assert hits
    assert hits[0].document_ref == result.document.ref
    assert "embedding" in hits[0].channels
    assert "rrf" in hits[0].match_signals


def test_zero_norm_embeddings_are_never_search_hits(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(
        knowledge_store,
        "# Invalid vector\n\nThis chunk receives a zero-norm embedding.",
        title="零向量",
    )
    chunks = knowledge_store.list_chunks_for_embedding(
        "alice",
        result.version.ref,
    )
    knowledge_store.replace_chunk_embeddings(
        "alice",
        result.version.ref,
        model="broken-embedding-v1",
        embedding_space_id="knowledge-space-a",
        vectors={chunk.ref: [0.0, 0.0] for chunk in chunks},
        total_chunks=len(chunks),
    )

    hits = knowledge_store.search_chunks_by_embedding(
        "alice",
        [1.0, 0.0],
        embedding_space_id="knowledge-space-a",
        min_cosine=0.0,
    )

    assert hits == []


@pytest.mark.asyncio
async def test_retrieval_service_reads_chunk_refs_off_event_loop(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(
        knowledge_store,
        "# Referenced chunk\n\nExact content loaded through the retrieval service.",
        title="引用读取",
    )
    chunk = knowledge_store.list_chunks_for_embedding(
        "alice",
        result.version.ref,
    )[0]
    service = KnowledgeRetrievalService(
        store=knowledge_store,
        embedding_client=FakeEmbeddingClient(),
    )

    hits = await service.get_chunks_by_refs(
        user_id="alice",
        chunk_refs=[chunk.ref],
        include_sensitive=False,
    )

    assert [hit.chunk_ref for hit in hits] == [chunk.ref]
    assert hits[0].excerpt == chunk.content


@pytest.mark.asyncio
async def test_embedding_failure_falls_back_to_keyword_results(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(
        knowledge_store,
        "# Local fallback\n\nKEYWORD-FALLBACK remains searchable.",
        title="关键词回退",
    )
    service = KnowledgeRetrievalService(
        store=knowledge_store,
        embedding_client=FailingQueryEmbeddingClient(),
    )

    hits = await service.search_chunks(
        user_id="alice",
        query="KEYWORD-FALLBACK",
        limit=5,
    )

    assert hits[0].document_ref == result.document.ref
    assert hits[0].channels == ["fts"]


@pytest.mark.asyncio
async def test_route_space_switch_ignores_old_vectors_and_uses_fts(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(
        knowledge_store,
        "# Route switch\n\nROUTE-KEYWORD remains locally searchable.",
        title="路由切换",
    )
    first_client = FakeEmbeddingClient()
    indexed = await KnowledgeEmbeddingIndexer(
        store=knowledge_store,
        embedding_client=first_client,
    ).index_version(user_id="alice", version_ref=result.version.ref)
    assert indexed["status"] == "ready"

    switched_client = FakeEmbeddingClient()
    switched_client.embedding_space_id = "knowledge-space-b"
    hits = await KnowledgeRetrievalService(
        store=knowledge_store,
        embedding_client=switched_client,
        min_cosine=0.8,
    ).search_chunks(
        user_id="alice",
        query="ROUTE-KEYWORD",
        limit=5,
    )

    assert hits
    assert hits[0].document_ref == result.document.ref
    assert hits[0].channels == ["fts"]
    assert "embedding" not in hits[0].match_signals


@pytest.mark.asyncio
async def test_unknown_embedding_space_never_calls_provider_and_falls_back_to_fts(
    knowledge_store: KnowledgeStore,
) -> None:
    result = _commit(
        knowledge_store,
        "UNKNOWN-SPACE-KEYWORD remains available through FTS.",
        title="未知空间",
    )
    client = UnknownSpaceEmbeddingClient()

    indexed = await KnowledgeEmbeddingIndexer(
        store=knowledge_store,
        embedding_client=client,
    ).index_version(user_id="alice", version_ref=result.version.ref)
    hits = await KnowledgeRetrievalService(
        store=knowledge_store,
        embedding_client=client,
    ).search_chunks(
        user_id="alice",
        query="UNKNOWN-SPACE-KEYWORD",
        limit=5,
    )

    assert indexed["status"] == "disabled"
    assert client.calls == 0
    assert hits[0].channels == ["fts"]


@pytest.mark.asyncio
async def test_export_omits_vectors_and_restore_rebuilds_current_space(
    knowledge_store: KnowledgeStore,
    tmp_path: Path,
) -> None:
    source = _commit(
        knowledge_store,
        "# Backup\n\nVECTOR-TARGET is rebuilt after restore.",
        title="向量备份",
    )
    await KnowledgeEmbeddingIndexer(
        store=knowledge_store,
        embedding_client=FakeEmbeddingClient(),
    ).index_version(user_id="alice", version_ref=source.version.ref)
    exported = knowledge_store.export_user("alice")

    exported_version = exported["documents"][0]["versions"][0]
    assert "embedding_space_id" not in exported_version
    assert "embedding_model" not in exported_version
    assert "embedding" not in exported_version

    restored_store = KnowledgeStore(str(tmp_path / "restored-space.db"))
    restored_store.init_db()
    restored_store.restore_export("bob", exported)
    restored_document = restored_store.list_documents(
        "bob",
        include_sensitive=True,
    )[0]
    restored_version = restored_store.get_version(
        "bob",
        version_id=restored_document.current_version_ref,
    )
    with restored_store._connect() as connection:
        before = connection.execute(
            "SELECT COUNT(*) AS count FROM knowledge_chunk_embeddings WHERE user_id = ?",
            ("bob",),
        ).fetchone()["count"]
    assert before == 0
    assert restored_version.embedding_space_id == ""

    current_client = FakeEmbeddingClient()
    current_client.embedding_space_id = "knowledge-space-current"
    rebuilt = await KnowledgeEmbeddingIndexer(
        store=restored_store,
        embedding_client=current_client,
    ).index_version(
        user_id="bob",
        version_ref=restored_document.current_version_ref,
    )
    refreshed = restored_store.get_version(
        "bob",
        version_id=restored_document.current_version_ref,
    )
    with restored_store._connect() as connection:
        spaces = {
            row["embedding_space_id"]
            for row in connection.execute(
                "SELECT embedding_space_id FROM knowledge_chunk_embeddings "
                "WHERE user_id = ?",
                ("bob",),
            ).fetchall()
        }

    assert rebuilt["status"] == "ready"
    assert refreshed.embedding_space_id == "knowledge-space-current"
    assert spaces == {"knowledge-space-current"}


def test_document_scope_requires_all_tags_and_exact_metadata(
    knowledge_store: KnowledgeStore,
) -> None:
    matching = _commit(
        knowledge_store,
        "匹配正文",
        title="匹配",
        tags=["产品", "架构"],
        metadata={"department": "研发", "year": 2026},
    )
    _commit(
        knowledge_store,
        "其他正文",
        title="其他",
        tags=["产品"],
        metadata={"department": "市场", "year": 2026},
    )

    refs = knowledge_store.resolve_document_refs(
        "alice",
        tags=["产品", "架构"],
        metadata_filter={"department": "研发"},
    )

    assert refs == [matching.document.ref]
