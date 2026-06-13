from abc import ABC, abstractmethod
import json
import math
import re

import httpx

from app.memory.models import MemoryRecord
from app.memory.store import MemoryStore


class EmbeddingClient(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float] | None:
        raise NotImplementedError


class NullEmbeddingClient(EmbeddingClient):
    async def embed(self, text: str) -> list[float] | None:
        return None


class OpenAICompatibleEmbeddingClient(EmbeddingClient):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 1024,
        timeout_seconds: float = 60.0,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds

    async def embed(self, text: str) -> list[float] | None:
        url = f"{self.base_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "input": text,
            "encoding_format": "float",
            "dimensions": self.dimensions,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return None

        try:
            embedding = data.get("data", [{}])[0].get("embedding")
            if not isinstance(embedding, list):
                return None
            return [float(value) for value in embedding]
        except (IndexError, TypeError, ValueError):
            return None


class MemorySearchService:
    def __init__(self, *, store: MemoryStore, embedding_client: EmbeddingClient):
        self.store = store
        self.embedding_client = embedding_client

    async def search(self, *, query: str, user_id: str, limit: int = 8) -> list[MemoryRecord]:
        memories = self.store.list_memories(user_id=user_id, limit=200)
        if not memories:
            return []

        query_embedding = await self.embedding_client.embed(query)
        if query_embedding:
            scored = self._score_by_embedding(memories, query_embedding)
            if scored:
                return [memory for _, memory in scored[:limit]]

        scored = self._score_by_keywords(memories, query)
        return [memory for _, memory in scored[:limit]]

    def _score_by_embedding(
        self,
        memories: list[MemoryRecord],
        query_embedding: list[float],
    ) -> list[tuple[float, MemoryRecord]]:
        scored: list[tuple[float, MemoryRecord]] = []
        for memory in memories:
            if not memory.embedding_json:
                continue
            try:
                memory_embedding = json.loads(memory.embedding_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(memory_embedding, list):
                continue
            score = cosine_similarity(query_embedding, [float(value) for value in memory_embedding])
            if score > 0:
                scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    def _score_by_keywords(
        self,
        memories: list[MemoryRecord],
        query: str,
    ) -> list[tuple[float, MemoryRecord]]:
        query_terms = _terms(query)
        scored: list[tuple[float, MemoryRecord]] = []
        query_lower = query.lower()

        for memory in memories:
            content_lower = memory.content.lower()
            content_terms = _terms(memory.content)
            text_score = (
                len(query_terms & content_terms)
                + (2 if query_lower and query_lower in content_lower else 0)
                + _char_overlap_score(query_lower, content_lower)
            )
            if text_score > 0:
                score = text_score + memory.importance * 0.05
                scored.append((score, memory))

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _terms(text: str) -> set[str]:
    return {term.lower() for term in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", text)}


def _char_overlap_score(query: str, content: str) -> float:
    query_chars = {char for char in query if not char.isspace()}
    content_chars = {char for char in content if not char.isspace()}
    if not query_chars or not content_chars:
        return 0.0
    return len(query_chars & content_chars) / len(query_chars)
