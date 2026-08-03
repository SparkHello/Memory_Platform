from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Sequence

import anyio

from app.knowledge.models import KnowledgeSearchHit
from app.knowledge.store import KnowledgeStore
from app.memory.search import (
    EmbeddingClient,
    NullEmbeddingClient,
    embedding_space_id_for,
)
from app.usage.context import model_usage_scope


class KnowledgeEmbeddingIndexer:
    """Build derived chunk embeddings without changing canonical knowledge text."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        embedding_client: EmbeddingClient,
        batch_size: int = 32,
    ) -> None:
        self.store = store
        self.embedding_client = embedding_client
        self.batch_size = max(1, min(128, int(batch_size)))

    @property
    def enabled(self) -> bool:
        return not isinstance(self.embedding_client, NullEmbeddingClient) and bool(
            self.model and self.embedding_space_id
        )

    @property
    def model(self) -> str:
        return str(getattr(self.embedding_client, "model", "") or "")

    @property
    def embedding_space_id(self) -> str:
        return embedding_space_id_for(self.embedding_client)

    @property
    def allow_sensitive_egress(self) -> bool:
        return bool(
            getattr(self.embedding_client, "allow_sensitive_egress", False)
        )

    async def index_version(
        self,
        *,
        user_id: str,
        version_ref: str,
    ) -> dict[str, int | str]:
        model = self.model
        embedding_space_id = self.embedding_space_id
        if (
            isinstance(self.embedding_client, NullEmbeddingClient)
            or not model
            or not embedding_space_id
        ):
            await anyio.to_thread.run_sync(
                partial(
                    self.store.set_version_embedding_status,
                    user_id=user_id,
                    version_ref=version_ref,
                    status="disabled",
                    embedding_space_id=embedding_space_id,
                    error=(
                        "embedding space is not configured"
                        if model and not embedding_space_id
                        else "embedding provider is not configured"
                    ),
                )
            )
            return {"status": "disabled", "stored": 0, "total": 0}
        chunks = await anyio.to_thread.run_sync(
            partial(
                self.store.list_chunks_for_embedding,
                user_id=user_id,
                version_ref=version_ref,
                include_sensitive=self.allow_sensitive_egress,
            )
        )
        if not chunks:
            await anyio.to_thread.run_sync(
                partial(
                    self.store.set_version_embedding_status,
                    user_id=user_id,
                    version_ref=version_ref,
                    status="disabled",
                    model=model,
                    embedding_space_id=embedding_space_id,
                    error="no eligible chunks; sensitive knowledge requires explicit egress approval",
                )
            )
            return {"status": "disabled", "stored": 0, "total": 0}
        await anyio.to_thread.run_sync(
            partial(
                self.store.set_version_embedding_status,
                user_id=user_id,
                version_ref=version_ref,
                status="indexing",
                model=model,
                embedding_space_id=embedding_space_id,
            )
        )
        override_confirmed = await anyio.to_thread.run_sync(
            partial(
                self.store.egress_override_confirmed,
                user_id=user_id,
                version_ref=version_ref,
            )
        )
        vectors: dict[str, list[float]] = {}
        try:
            for start in range(0, len(chunks), self.batch_size):
                batch = chunks[start : start + self.batch_size]
                with model_usage_scope(
                    user_id=user_id,
                    operation="knowledge_index",
                ):
                    embedded = await self.embedding_client.embed_many(
                        [chunk.content for chunk in batch],
                        screen_sensitivity=not override_confirmed,
                    )
                for chunk, vector in zip(batch, embedded, strict=False):
                    if vector:
                        vectors[chunk.ref] = vector
            return await anyio.to_thread.run_sync(
                partial(
                    self.store.replace_chunk_embeddings,
                    user_id=user_id,
                    version_ref=version_ref,
                    model=model,
                    embedding_space_id=embedding_space_id,
                    vectors=vectors,
                    total_chunks=len(chunks),
                )
            )
        except Exception as exc:
            await anyio.to_thread.run_sync(
                partial(
                    self.store.set_version_embedding_status,
                    user_id=user_id,
                    version_ref=version_ref,
                    status="failed",
                    model=model,
                    embedding_space_id=embedding_space_id,
                    error=_safe_embedding_error(exc),
                )
            )
            return {"status": "failed", "stored": len(vectors), "total": len(chunks)}


class KnowledgeRetrievalService:
    """Hybrid FTS/vector retrieval with deterministic weighted RRF fusion."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        embedding_client: EmbeddingClient,
        vector_weight: float = 0.65,
        min_cosine: float = 0.25,
    ) -> None:
        self.store = store
        self.embedding_client = embedding_client
        self.vector_weight = max(0.0, min(1.0, float(vector_weight)))
        self.min_cosine = max(-1.0, min(1.0, float(min_cosine)))

    async def search_chunks(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 5,
        document_refs: Sequence[str] | None = None,
        include_sensitive: bool = False,
    ) -> list[KnowledgeSearchHit]:
        candidate_limit = min(20, max(10, int(limit) * 4))
        keyword_task = asyncio.create_task(
            anyio.to_thread.run_sync(
                partial(
                    self.store.search_chunks,
                    user_id=user_id,
                    query=query,
                    limit=candidate_limit,
                    document_refs=list(document_refs or []),
                    include_sensitive=include_sensitive,
                )
            )
        )
        embedding_space_id = embedding_space_id_for(self.embedding_client)
        query_vector = None
        if embedding_space_id:
            try:
                with model_usage_scope(
                    user_id=user_id,
                    operation="knowledge_search",
                ):
                    query_vector = await self.embedding_client.embed(query)
            except Exception:
                query_vector = None
        keyword_hits = await keyword_task
        vector_hits: list[KnowledgeSearchHit] = []
        if query_vector:
            vector_hits = await anyio.to_thread.run_sync(
                partial(
                    self.store.search_chunks_by_embedding,
                    user_id=user_id,
                    query_vector=query_vector,
                    embedding_space_id=embedding_space_id,
                    query=query,
                    limit=candidate_limit,
                    document_refs=list(document_refs or []),
                    include_sensitive=include_sensitive,
                    min_cosine=self.min_cosine,
                )
            )
        return _weighted_rrf(
            keyword_hits,
            vector_hits,
            limit=max(1, min(20, int(limit))),
            vector_weight=self.vector_weight,
        )

    search_index = search_chunks

    def get_chunks_by_refs(self, **kwargs):
        return self.store.get_chunks_by_refs(**kwargs)

    inspect_chunks = get_chunks_by_refs


def _weighted_rrf(
    keyword_hits: Sequence[KnowledgeSearchHit],
    vector_hits: Sequence[KnowledgeSearchHit],
    *,
    limit: int,
    vector_weight: float,
    rank_constant: int = 60,
) -> list[KnowledgeSearchHit]:
    keyword_weight = 1.0 - vector_weight if vector_hits else 1.0
    effective_vector_weight = vector_weight if keyword_hits else 1.0
    scores: dict[str, float] = {}
    representatives: dict[str, KnowledgeSearchHit] = {}
    channels: dict[str, list[str]] = {}
    signals: dict[str, list[str]] = {}
    for channel, hits, weight in (
        ("fts", keyword_hits, keyword_weight),
        ("embedding", vector_hits, effective_vector_weight),
    ):
        for rank, hit in enumerate(hits, start=1):
            reference = hit.chunk_ref
            scores[reference] = scores.get(reference, 0.0) + weight / (
                rank_constant + rank
            )
            if reference not in representatives or channel == "fts":
                representatives[reference] = hit
            channels.setdefault(reference, [])
            if channel not in channels[reference]:
                channels[reference].append(channel)
            signals.setdefault(reference, [])
            for signal in hit.match_signals:
                if signal not in signals[reference]:
                    signals[reference].append(signal)
    ordered = sorted(
        scores,
        key=lambda ref: (
            -scores[ref],
            representatives[ref].ordinal,
            ref,
        ),
    )
    maximum = scores[ordered[0]] if ordered else 1.0
    result: list[KnowledgeSearchHit] = []
    for reference in ordered[:limit]:
        channel_values = channels[reference]
        match_signals = list(signals[reference])
        if len(channel_values) > 1:
            match_signals.append("hybrid")
        match_signals.append("rrf")
        result.append(
            representatives[reference].model_copy(
                update={
                    "score": scores[reference] / maximum if maximum else 0.0,
                    "match_signals": list(dict.fromkeys(match_signals)),
                    "channels": channel_values,
                }
            )
        )
    return result


def _safe_embedding_error(exc: Exception) -> str:
    message = str(exc).strip().replace("\x00", "")
    return (message or exc.__class__.__name__)[:1000]
