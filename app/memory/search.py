from abc import ABC, abstractmethod
from datetime import UTC, datetime
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

    async def search(
        self,
        *,
        query: str,
        user_id: str,
        limit: int = 8,
        record_usage: bool = True,
    ) -> list[MemoryRecord]:
        memories = self.store.list_memories(user_id=user_id, limit=200)
        if not memories:
            return []

        query_embedding = await self.embedding_client.embed(query)
        if query_embedding:
            scored = self._score_by_embedding(memories, query_embedding)
            if scored:
                return self._record_usage(
                    [memory for _, memory in scored[:limit]],
                    user_id=user_id,
                    record_usage=record_usage,
                )

        scored = self._score_by_keywords(memories, query)
        return self._record_usage(
            [memory for _, memory in scored[:limit]],
            user_id=user_id,
            record_usage=record_usage,
        )

    def _score_by_embedding(
        self,
        memories: list[MemoryRecord],
        query_embedding: list[float],
    ) -> list[tuple[float, MemoryRecord]]:
        scored: list[tuple[float, MemoryRecord]] = []
        now = datetime.now(UTC)
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
                scored.append((score + _metadata_score(memory, now, embedding_mode=True), memory))
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
        now = datetime.now(UTC)

        for memory in memories:
            content_lower = memory.content.lower()
            content_terms = _terms(memory.content)
            text_score = (
                len(query_terms & content_terms)
                + (2 if query_lower and query_lower in content_lower else 0)
                + _char_overlap_score(query_lower, content_lower)
            )
            if text_score > 0:
                score = text_score + _metadata_score(memory, now, embedding_mode=False)
                scored.append((score, memory))

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    def _record_usage(
        self,
        memories: list[MemoryRecord],
        *,
        user_id: str,
        record_usage: bool,
    ) -> list[MemoryRecord]:
        if not record_usage:
            return memories
        used_at = self.store.mark_memories_used(
            memory_ids=[memory.id for memory in memories],
            user_id=user_id,
        )
        if used_at:
            for memory in memories:
                memory.usage_count += 1
                memory.last_used_at = used_at
        return memories


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


def _metadata_score(memory: MemoryRecord, now: datetime, *, embedding_mode: bool) -> float:
    importance_weight = 0.015 if embedding_mode else 0.05
    usage_weight = 0.01 if embedding_mode else 0.02
    return (
        memory.importance * importance_weight
        + min(memory.usage_count, 10) * usage_weight
        - _decay_penalty(memory, now)
        - _validity_penalty(memory, now, embedding_mode=embedding_mode)
        - _sensitivity_penalty(memory, embedding_mode=embedding_mode)
    )


def _decay_penalty(memory: MemoryRecord, now: datetime) -> float:
    # 关系、长期偏好、沟通风格不应因为少用就明显贬值；情景类事实自然下沉。
    rate, cap, grace_days = {
        "project": (0.0020, 0.40, 14),
        "learning": (0.0015, 0.30, 30),
        "fact": (0.0010, 0.25, 30),
        "preference": (0.0003, 0.08, 60),
        "style": (0.0002, 0.05, 60),
        "person": (0.0, 0.0, 0),
        "relationship": (0.0, 0.0, 0),
    }.get(memory.type, (0.0010, 0.20, 30))
    if rate <= 0:
        return 0.0

    anchor = _parse_iso_datetime(memory.last_used_at or memory.updated_at or memory.created_at)
    if anchor is None:
        return 0.0
    elapsed_days = max(0.0, (now - anchor).total_seconds() / 86400)
    decaying_days = max(0.0, elapsed_days - grace_days)
    return min(cap, decaying_days * rate)


def _validity_penalty(memory: MemoryRecord, now: datetime, *, embedding_mode: bool) -> float:
    valid_until = _parse_iso_datetime(memory.valid_until)
    if valid_until is None or valid_until >= now:
        return 0.0
    penalties = {
        "temporary": 0.45 if embedding_mode else 1.50,
        "medium": 0.25 if embedding_mode else 0.80,
        "stable": 0.10 if embedding_mode else 0.30,
    }
    return penalties.get(memory.stability, 0.30)


def _sensitivity_penalty(memory: MemoryRecord, *, embedding_mode: bool) -> float:
    penalties = {
        "private": 0.08 if embedding_mode else 0.25,
        "sensitive": 0.18 if embedding_mode else 0.60,
    }
    return penalties.get(memory.sensitivity, 0.0)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
