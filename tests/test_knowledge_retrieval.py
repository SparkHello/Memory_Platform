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
    allow_sensitive_egress = False

    async def embed(self, text: str) -> list[float] | None:
        if "semantic-query" in text:
            return [1.0, 0.0]
        if "VECTOR-TARGET" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]

    async def embed_many(self, texts: list[str]) -> list[list[float] | None]:
        return [await self.embed(text) for text in texts]


class FailingQueryEmbeddingClient(FakeEmbeddingClient):
    async def embed(self, text: str) -> list[float] | None:
        raise RuntimeError("provider unavailable")


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
    assert hits
    assert hits[0].document_ref == result.document.ref
    assert "embedding" in hits[0].channels
    assert "rrf" in hits[0].match_signals


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
