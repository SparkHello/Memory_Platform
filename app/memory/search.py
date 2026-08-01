from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
import hashlib
import json
import math
import re
import threading
import time

import anyio
import httpx

from app.memory.decay import MemoryDecayScore, life_score, score_memory
from app.memory.models import MemoryRecord, MemorySurfaceMode, MemorySurfaceSignal
from app.memory.redaction import detect_text_sensitivity
from app.memory.store import MemoryStore
from app.memory.utils import _memory_embedding_vector, _parse_iso_datetime, _terms
from app.usage.context import model_usage_scope
from app.usage.recorder import UsageRecorder


# ---------------------------------------------------------------------------
# 模块级缓存（跨请求存活）
# ---------------------------------------------------------------------------
_EMBEDDING_CACHE: dict[tuple, tuple[float, list[float]]] = {}
"""L1: (user_id, normalized_query) -> (expires_at, embedding_vector)"""

SEARCH_CACHE: dict[tuple, tuple[float, str, int, list[dict[str, object]]]] = {}
"""L2: (user_id, normalized_query, limit) -> (expires_at, max_updated_at, active_count, hit payloads)"""

_EMBEDDING_CACHE_MAX = 512
_SEARCH_CACHE_MAX = 256
_EMBEDDING_CACHE_TTL = 300   # 5 分钟
_SEARCH_CACHE_TTL = 120       # 2 分钟
_CACHE_METRICS: dict[str, dict[str, int]] = {}
_CACHE_METRICS_LOCK = threading.Lock()
_RECALL_LIMIT = 20
# SQLite/FTS 候选生成接入前，宁可扫描当前个人库，也不能按重要度提前丢掉
# 低频但精确匹配的旧记忆。该上限只是异常数据量保护，不参与相关性裁剪。
RECALL_CANDIDATE_POOL = 10_000
EMBEDDING_MIN_COSINE = 0.55
KEYWORD_MIN_SCORE = 20.0
_QUERY_QUESTION_PHRASES = (
    "有什么",
    "是什么",
    "为什么",
    "有没有",
    "在哪里",
    "在哪儿",
    "哪些",
    "哪个",
    "什么",
    "哪里",
    "怎么",
    "如何",
    "是否",
)
_QUERY_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:请(?:帮我)?(?:查找|搜索|回忆|告诉我)?|关于)?"
    r"(?:用户本人|用户|我本人|本人|我的|我|他的|他|她的|她)(?:的)?"
)
_EXPLICIT_USER_QUERY_RE = re.compile(
    r"^(?:请(?:帮我)?(?:查找|搜索|回忆|告诉我)?|关于)?"
    r"(?:用户本人|用户|我本人|本人|我的|我|他的|他|她的|她)"
)
_RELATED_ENTITY_QUERY_TERMS = (
    "宠物",
    "猫",
    "狗",
    "动物",
    "孩子",
    "家人",
    "父母",
    "伴侣",
    "朋友",
    "同事",
)
_KEYWORD_LOW_INFORMATION_PHRASES = (
    "喜欢",
    "偏好",
    "相关",
    "信息",
    "情况",
)
_SURFACE_MODES: set[MemorySurfaceMode] = {
    "balanced",
    "important",
    "emotional",
    "stale",
    "review_due",
}
_STALE_DAYS = 90.0
_NEAR_EXPIRY_DAYS = 14
_LOW_LIFE_THRESHOLD = 30.0


def _record_cache_metric(user_id: str, name: str) -> None:
    with _CACHE_METRICS_LOCK:
        metrics = _CACHE_METRICS.setdefault(user_id, {})
        metrics[name] = metrics.get(name, 0) + 1


def search_cache_stats(user_id: str) -> dict[str, object]:
    """Return process-local, user-isolated cache counters and cache policy."""
    with _CACHE_METRICS_LOCK:
        metrics = dict(_CACHE_METRICS.get(user_id, {}))
    recall_hits = metrics.get("recall_hits", 0)
    recall_misses = metrics.get("recall_misses", 0)
    embedding_hits = metrics.get("embedding_hits", 0)
    embedding_misses = metrics.get("embedding_misses", 0)

    def _rate(hits: int, misses: int) -> float | None:
        attempts = hits + misses
        return round(hits / attempts, 4) if attempts else None

    return {
        "scope": "current_process",
        "user_id": user_id,
        "recall": {
            "hits": recall_hits,
            "misses": recall_misses,
            "hit_rate": _rate(recall_hits, recall_misses),
            "ttl_seconds": _SEARCH_CACHE_TTL,
            "max_entries": _SEARCH_CACHE_MAX,
        },
        "embedding": {
            "hits": embedding_hits,
            "misses": embedding_misses,
            "hit_rate": _rate(embedding_hits, embedding_misses),
            "ttl_seconds": _EMBEDDING_CACHE_TTL,
            "max_entries": _EMBEDDING_CACHE_MAX,
        },
        "note": (
            "Counters reset when the service process restarts. Empty queries and "
            "cache-disabled searches are excluded."
        ),
    }


def _normalize_query(query: str) -> str:
    """Normalize a cache key without colliding on long chat messages."""
    normalized = " ".join(query.split()).lower()
    if len(normalized) <= 200:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{normalized[:200]}:{digest}"


def _semantic_query_text(query: str) -> str:
    """把面向助手的自然问句压缩成更接近记忆正文的检索意图。"""
    original = " ".join(query.split()).strip()
    text = original
    for phrase in _QUERY_QUESTION_PHRASES:
        text = text.replace(phrase, "")
    text = _QUERY_SUBJECT_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"[？?。！!，,：:]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or original


def _keyword_query_text(query: str) -> str:
    """移除会让几乎所有用户记忆互相命中的低信息量表达。"""
    text = _semantic_query_text(query)
    for phrase in _KEYWORD_LOW_INFORMATION_PHRASES:
        text = text.replace(phrase, "")
    text = text.replace("的", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _now() -> float:
    return time.time()


@dataclass
class MemorySearchHit:
    memory: MemoryRecord
    relevance: float
    channels: list[str] = field(default_factory=list)
    topic_score: float = 0.0
    total_score: float = 0.0
    final_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    activation_count: int = 0
    last_active_at: str | None = None
    freshness_bonus: float = 1.0


@dataclass
class MemorySurfaceHit:
    memory: MemoryRecord
    final_score: float
    activation_count: int
    last_active_at: str | None
    freshness_bonus: float
    surface_reason: str
    surface_score: float
    surface_mode: MemorySurfaceMode
    surface_reason_text: str
    life_score: float
    days_since_last_active: float
    review_signals: list[MemorySurfaceSignal]


# ---------------------------------------------------------------------------
# Embedding client interfaces
# ---------------------------------------------------------------------------


class EmbeddingClient(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float] | None:
        raise NotImplementedError

    async def embed_many(self, texts: list[str]) -> list[list[float] | None]:
        return [await self.embed(text) for text in texts]


class NullEmbeddingClient(EmbeddingClient):
    async def embed(self, text: str) -> list[float] | None:
        return None

    async def embed_many(self, texts: list[str]) -> list[list[float] | None]:
        return [None for _ in texts]


class OpenAICompatibleEmbeddingClient(EmbeddingClient):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 1024,
        timeout_seconds: float = 60.0,
        allow_sensitive_egress: bool = False,
        usage_recorder: UsageRecorder | None = None,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self.allow_sensitive_egress = allow_sensitive_egress
        self.usage_recorder = usage_recorder

    async def embed(self, text: str) -> list[float] | None:
        if not self.allow_sensitive_egress and detect_text_sensitivity(text) != "normal":
            return None
        vectors = await self._request_embeddings(text)
        return vectors[0] if vectors else None

    async def embed_many(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []
        allowed_indices: list[int] = []
        allowed_texts: list[str] = []
        results: list[list[float] | None] = [None for _ in texts]
        for index, text in enumerate(texts):
            if (
                self.allow_sensitive_egress
                or detect_text_sensitivity(text) == "normal"
            ):
                allowed_indices.append(index)
                allowed_texts.append(text)
        if not allowed_texts:
            return results
        vectors = await self._request_embeddings(allowed_texts)
        for local_index, original_index in enumerate(allowed_indices):
            if local_index < len(vectors):
                results[original_index] = vectors[local_index]
        return results

    async def _request_embeddings(
        self,
        input_value: str | list[str],
    ) -> list[list[float] | None]:
        expected_count = len(input_value) if isinstance(input_value, list) else 1
        url = f"{self.base_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "input": input_value,
            "encoding_format": "float",
            "dimensions": self.dimensions,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return []

        if self.usage_recorder is not None and isinstance(data, dict):
            self.usage_recorder.record_response(
                payload=data,
                model=self.model,
                kind="embedding",
                base_url=self.base_url,
            )
        try:
            items = data.get("data")
            if not isinstance(items, list):
                return []
            by_index: dict[int, list[float]] = {}
            for fallback_index, item in enumerate(items):
                if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                    continue
                response_index = item.get("index", fallback_index)
                if isinstance(response_index, bool) or not isinstance(response_index, int):
                    continue
                by_index[response_index] = [
                    float(value) for value in item["embedding"]
                ]
            return [by_index.get(index) for index in range(expected_count)]
        except (IndexError, TypeError, ValueError):
            return []


class MemorySearchService:
    def __init__(
        self,
        *,
        store: MemoryStore,
        embedding_client: EmbeddingClient,
        time_ripple_delta: float = 0.0,
        time_ripple_window_hours: int = 48,
        enable_cache: bool = True,
    ):
        self.store = store
        self.embedding_client = embedding_client
        # 评测等隔离场景关闭进程级缓存：缓存 key 只含 (user, query, limit)，
        # 不区分数据源/检索模式，复用会让 keyword 与 embedding 基线互相污染。
        self.enable_cache = enable_cache
        self.last_cache_status = "bypass"
        self.last_embedding_cache_status = "bypass"
        self.time_ripple_delta = max(0.0, min(1.0, float(time_ripple_delta or 0.0)))
        self.time_ripple_window_hours = max(1, min(720, int(time_ripple_window_hours or 48)))

    async def search(
        self,
        *,
        query: str,
        user_id: str,
        limit: int = 8,
        record_usage: bool = True,
        include_sensitive: bool = False,
    ) -> list[MemoryRecord]:
        hits = await self.search_hits(
            query=query,
            user_id=user_id,
            limit=limit,
            record_usage=record_usage,
            include_sensitive=include_sensitive,
        )
        return [hit.memory for hit in hits]

    async def search_hits(
        self,
        *,
        query: str,
        user_id: str,
        limit: int = 8,
        record_usage: bool = True,
        include_sensitive: bool = False,
    ) -> list[MemorySearchHit]:
        normalized = _normalize_query(query)
        if not normalized:
            self.last_cache_status = "empty"
            self.last_embedding_cache_status = "empty"
            return []
        capped_limit = max(1, min(limit, 20))
        now = _now()

        l2_key = (user_id, normalized, capped_limit, bool(include_sensitive))
        if self.enable_cache:
            cached_hits = await anyio.to_thread.run_sync(
                partial(
                    self._cached_hits_with_usage,
                    l2_key,
                    user_id=user_id,
                    now=now,
                    record_usage=record_usage,
                )
            )
            if cached_hits is not None:
                self.last_cache_status = "hit"
                self.last_embedding_cache_status = "not-needed"
                _record_cache_metric(user_id, "recall_hits")
                return cached_hits
            self.last_cache_status = "miss"
            _record_cache_metric(user_id, "recall_misses")
        else:
            self.last_cache_status = "bypass"

        query_embedding = await self._query_embedding(
            query=query,
            normalized_query=normalized,
            user_id=user_id,
            now=now,
        )

        hits = await anyio.to_thread.run_sync(
            partial(
                self._recall_and_rank,
                user_id=user_id,
                query=query,
                query_embedding=query_embedding,
                limit=capped_limit,
                include_sensitive=include_sensitive,
            )
        )
        if not hits:
            return []
        return await anyio.to_thread.run_sync(
            partial(
                self._finalize_hits,
                l2_key,
                hits,
                user_id=user_id,
                record_usage=record_usage,
                now=now,
            )
        )

    def _cached_hits_with_usage(
        self,
        key: tuple,
        *,
        user_id: str,
        now: float,
        record_usage: bool,
    ) -> list[MemorySearchHit] | None:
        cached_hits = self._cached_search_hits(key, user_id=user_id, now=now)
        if cached_hits is None:
            return None
        return self._record_hit_usage(
            cached_hits,
            user_id=user_id,
            record_usage=record_usage,
        )

    def _recall_and_rank(
        self,
        *,
        user_id: str,
        query: str,
        query_embedding: list[float] | None,
        limit: int,
        include_sensitive: bool,
    ) -> list[MemorySearchHit]:
        memories = self.store.list_memories(user_id=user_id, limit=RECALL_CANDIDATE_POOL)
        memories = [
            memory
            for memory in memories
            if memory.origin == "user_asserted"
            and (include_sensitive or not _memory_is_locally_sensitive(memory))
        ]
        if not memories:
            return []
        return self._rank_hits(
            memories=memories,
            query=query,
            query_embedding=query_embedding,
            limit=limit,
        )

    def _finalize_hits(
        self,
        key: tuple,
        hits: list[MemorySearchHit],
        *,
        user_id: str,
        record_usage: bool,
        now: float,
    ) -> list[MemorySearchHit]:
        hits = self._record_hit_usage(hits, user_id=user_id, record_usage=record_usage)
        if self.enable_cache:
            self._cache_search_hits(key, hits, user_id=user_id, now=now)
        return hits

    def surface_memories(
        self,
        *,
        user_id: str,
        limit: int = 8,
        mode: MemorySurfaceMode = "balanced",
        include_archived: bool = False,
        include_sensitive: bool = False,
    ) -> list[MemorySurfaceHit]:
        capped_limit = max(1, min(limit, 20))
        surface_mode = _normalize_surface_mode(mode)
        memories = self.store.list_memories(
            user_id=user_id,
            limit=1000,
            include_lifecycle_archived=include_archived,
        )
        if not memories:
            return []

        now = datetime.now(UTC)
        scored = [
            hit
            for memory in memories
            if memory.origin == "user_asserted"
            if include_sensitive or not _memory_is_locally_sensitive(memory)
            if getattr(memory, "status", None) != "pinned"
            if include_archived or getattr(memory, "status", None) != "archived"
            if (hit := _surface_hit(memory, now=now, mode=surface_mode)) is not None
        ]
        scored.sort(
            key=lambda hit: (hit.surface_score, hit.final_score, hit.memory.updated_at),
            reverse=True,
        )
        return scored[:capped_limit]

    async def _query_embedding(
        self,
        *,
        query: str,
        normalized_query: str,
        user_id: str,
        now: float,
    ) -> list[float] | None:
        l1_key = (user_id, normalized_query)
        if self.enable_cache and l1_key in _EMBEDDING_CACHE:
            l1_expires_at, l1_vector = _EMBEDDING_CACHE[l1_key]
            if now < l1_expires_at:
                self.last_embedding_cache_status = "hit"
                _record_cache_metric(user_id, "embedding_hits")
                return l1_vector
            del _EMBEDDING_CACHE[l1_key]

        if isinstance(self.embedding_client, NullEmbeddingClient):
            self.last_embedding_cache_status = "disabled"
        elif self.enable_cache:
            self.last_embedding_cache_status = "miss"
            _record_cache_metric(user_id, "embedding_misses")
        else:
            self.last_embedding_cache_status = "bypass"
        with model_usage_scope(user_id=user_id, operation="memory_search"):
            query_embedding = await self.embedding_client.embed(
                _semantic_query_text(query)
            )
        if query_embedding and self.enable_cache:
            self._cache_embedding(l1_key, query_embedding, now)
        return query_embedding

    def _rank_hits(
        self,
        *,
        memories: list[MemoryRecord],
        query: str,
        query_embedding: list[float] | None,
        limit: int,
    ) -> list[MemorySearchHit]:
        now = datetime.now(UTC)
        combined: dict[str, MemorySearchHit] = {}
        memories = [
            memory
            for memory in memories
            if not _query_memory_subject_conflict(query, memory)
        ]

        if query_embedding:
            for topic_score, memory in self._score_by_embedding(memories, query_embedding)[:_RECALL_LIMIT]:
                _upsert_hit(combined, memory, topic_score, "embedding", now=now)

        for topic_score, memory in self._score_by_keywords(memories, query)[:_RECALL_LIMIT]:
            _upsert_hit(combined, memory, topic_score, "keyword", now=now)

        hits = list(combined.values())
        hits.sort(
            key=lambda hit: (hit.total_score, hit.topic_score, hit.memory.updated_at),
            reverse=True,
        )
        return hits[:limit]

    def _cached_search_hits(
        self,
        key: tuple,
        *,
        user_id: str,
        now: float,
    ) -> list[MemorySearchHit] | None:
        if key not in SEARCH_CACHE:
            return None

        expires_at, max_updated_at, active_count, payloads = SEARCH_CACHE[key]
        if now >= expires_at:
            del SEARCH_CACHE[key]
            return None

        current_max = self.store.get_memories_max_updated_at(user_id=user_id)
        current_count = self.store.get_active_memory_count(user_id=user_id)
        if not current_max or current_max != max_updated_at or current_count != active_count:
            del SEARCH_CACHE[key]
            return None

        hits: list[MemorySearchHit] = []
        for payload in payloads:
            memory_id = payload.get("id")
            if not isinstance(memory_id, str):
                continue
            memory = self.store.get_memory(memory_id=memory_id, user_id=user_id)
            if memory is None:
                continue
            decay = score_memory(memory)
            channels = payload.get("channels")
            score_breakdown = payload.get("score_breakdown")
            hits.append(
                MemorySearchHit(
                    memory=memory,
                    relevance=_float_payload(payload.get("relevance")),
                    channels=[str(channel) for channel in channels] if isinstance(channels, list) else ["cache"],
                    topic_score=_float_payload(payload.get("topic_score")),
                    total_score=_float_payload(payload.get("total_score")),
                    final_score=decay.final_score,
                    score_breakdown=_score_breakdown_payload(score_breakdown),
                    activation_count=decay.activation_count,
                    last_active_at=decay.last_active_at,
                    freshness_bonus=decay.freshness_bonus,
                )
            )
            _refresh_hit_ranking(hits[-1])
        if not hits:
            del SEARCH_CACHE[key]
            return None
        return hits

    def _cache_embedding(self, key: tuple, vector: list[float], now: float) -> None:
        if len(_EMBEDDING_CACHE) >= _EMBEDDING_CACHE_MAX:
            _cleanup_expired(_EMBEDDING_CACHE, now)
        if len(_EMBEDDING_CACHE) < _EMBEDDING_CACHE_MAX:
            _EMBEDDING_CACHE[key] = (now + _EMBEDDING_CACHE_TTL, vector)

    def _cache_search_hits(
        self,
        key: tuple,
        hits: list[MemorySearchHit],
        *,
        user_id: str,
        now: float,
    ) -> None:
        if not hits:
            return
        if len(SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
            _cleanup_expired(SEARCH_CACHE, now)
        if len(SEARCH_CACHE) < _SEARCH_CACHE_MAX:
            max_updated = self.store.get_memories_max_updated_at(user_id=user_id)
            active_count = self.store.get_active_memory_count(user_id=user_id)
            if max_updated:
                SEARCH_CACHE[key] = (
                    now + _SEARCH_CACHE_TTL,
                    max_updated,
                    active_count,
                    [
                        {
                            "id": hit.memory.id,
                            "relevance": hit.relevance,
                            "channels": hit.channels,
                            "topic_score": hit.topic_score,
                            "total_score": hit.total_score,
                            "score_breakdown": hit.score_breakdown,
                        }
                        for hit in hits
                    ],
                )

    def _score_by_embedding(
        self,
        memories: list[MemoryRecord],
        query_embedding: list[float],
    ) -> list[tuple[float, MemoryRecord]]:
        scored: list[tuple[float, MemoryRecord]] = []
        for memory in memories:
            memory_embedding = _memory_embedding_vector(memory)
            if memory_embedding is None:
                continue
            cosine = cosine_similarity(query_embedding, memory_embedding)
            score = cosine * 100.0
            if cosine >= EMBEDDING_MIN_COSINE:
                scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    def _score_by_keywords(
        self,
        memories: list[MemoryRecord],
        query: str,
    ) -> list[tuple[float, MemoryRecord]]:
        keyword_query = _keyword_query_text(query)
        query_terms = _terms(keyword_query)
        if not query_terms:
            return []
        scored: list[tuple[float, MemoryRecord]] = []
        query_lower = keyword_query.lower()
        compact_query = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", query_lower)
        allow_substring_match = len(compact_query) >= 2

        for memory in memories:
            content_lower = memory.content.lower()
            content_terms = _terms(memory.content)
            shared_terms = query_terms & content_terms
            substring_match = (
                allow_substring_match
                and bool(compact_query)
                and compact_query in re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", content_lower)
            )
            # 字符重叠只能参与精排，不能独自生成候选；否则所有以“用户”开头的
            # 记忆都会被“用户的年龄”之类的无答案 query 误召回。
            if not shared_terms and not substring_match:
                continue
            term_score = min(45.0, len(shared_terms) * 18.0)
            coverage_score = len(shared_terms) / len(query_terms) * 25.0
            substring_score = 35.0 if substring_match else 0.0
            char_score = _char_overlap_score(query_lower, content_lower) * 15.0
            score = min(
                100.0,
                term_score + coverage_score + substring_score + char_score,
            )
            if score >= KEYWORD_MIN_SCORE:
                scored.append((score, memory))

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    def _record_hit_usage(
        self,
        hits: list[MemorySearchHit],
        *,
        user_id: str,
        record_usage: bool,
    ) -> list[MemorySearchHit]:
        if not record_usage:
            return hits
        used_at = self.store.mark_memories_used(
            memory_ids=[hit.memory.id for hit in hits],
            user_id=user_id,
            time_ripple_delta=self.time_ripple_delta,
            time_ripple_window_hours=self.time_ripple_window_hours,
        )
        if used_at:
            for hit in hits:
                hit.memory.usage_count += 1
                hit.memory.last_used_at = used_at
                _refresh_hit_decay(hit)
        return hits


def _cleanup_expired(cache: dict, now: float) -> None:
    expired = [k for k, v in cache.items() if now >= v[0]]
    for k in expired:
        del cache[k]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _char_overlap_score(query: str, content: str) -> float:
    query_chars = {char for char in query if not char.isspace()}
    content_chars = {char for char in content if not char.isspace()}
    if not query_chars or not content_chars:
        return 0.0
    return len(query_chars & content_chars) / len(query_chars)


def _query_memory_subject_conflict(query: str, memory: MemoryRecord) -> bool:
    """用户本人问题不应被宠物等其他主语的高相似文本截胡。"""
    compact_query = re.sub(r"\s+", "", query)
    if not _EXPLICIT_USER_QUERY_RE.match(compact_query):
        return False
    if any(term in compact_query for term in _RELATED_ENTITY_QUERY_TERMS):
        return False
    if memory.temporal_subject and memory.temporal_subject.lower() in {"用户", "user", "我"}:
        return False
    content = memory.content.lstrip()
    return not content.startswith(("用户", "我", "本人"))


def _upsert_hit(
    combined: dict[str, MemorySearchHit],
    memory: MemoryRecord,
    topic_score: float,
    channel: str,
    *,
    now: datetime,
) -> None:
    existing = combined.get(memory.id)
    if existing is None:
        combined[memory.id] = _build_hit(
            memory=memory,
            channels=[channel],
            semantic_score=topic_score if channel == "embedding" else 0.0,
            keyword_score=topic_score if channel == "keyword" else 0.0,
            now=now,
        )
        return

    if channel not in existing.channels:
        existing.channels.append(channel)
    if channel == "embedding":
        existing.score_breakdown["semantic_score"] = max(
            existing.score_breakdown.get("semantic_score", 0.0),
            topic_score,
        )
    elif channel == "keyword":
        existing.score_breakdown["keyword_score"] = max(
            existing.score_breakdown.get("keyword_score", 0.0),
            topic_score,
        )
    _refresh_hit_ranking(existing, now=now)


def _build_hit(
    *,
    memory: MemoryRecord,
    channels: list[str],
    semantic_score: float,
    keyword_score: float,
    now: datetime,
) -> MemorySearchHit:
    decay = score_memory(memory, now=now)
    topic_score = max(semantic_score, keyword_score)
    total = _total_rank_score(memory, topic_score=topic_score, decay=decay, now=now)
    relevance = _final_relevance(total)
    return MemorySearchHit(
        memory=memory,
        relevance=relevance,
        channels=list(channels),
        topic_score=topic_score,
        total_score=total,
        final_score=decay.final_score,
        score_breakdown=_build_score_breakdown(
            memory=memory,
            semantic_score=semantic_score,
            keyword_score=keyword_score,
            decay=decay,
            final_relevance=relevance,
        ),
        activation_count=decay.activation_count,
        last_active_at=decay.last_active_at,
        freshness_bonus=decay.freshness_bonus,
    )


def _surface_hit(
    memory: MemoryRecord,
    *,
    now: datetime,
    mode: MemorySurfaceMode,
) -> MemorySurfaceHit | None:
    decay = score_memory(memory, now=now)
    current_life_score = life_score(memory, now=now, decay=decay)
    review_signals = _surface_review_signals(
        memory,
        decay=decay,
        life=current_life_score,
        now=now,
    )
    if mode == "stale" and "stale" not in review_signals:
        return None
    if mode == "review_due" and not review_signals:
        return None

    surface_score = _surface_mode_score(
        memory,
        mode=mode,
        decay=decay,
        life=current_life_score,
        review_signals=review_signals,
    )
    surface_reason = _surface_reason(memory, decay, mode=mode, review_signals=review_signals)
    return MemorySurfaceHit(
        memory=memory,
        final_score=decay.final_score,
        activation_count=decay.activation_count,
        last_active_at=decay.last_active_at,
        freshness_bonus=decay.freshness_bonus,
        surface_reason=surface_reason,
        surface_score=surface_score,
        surface_mode=mode,
        surface_reason_text=_surface_reason_text(surface_reason, review_signals=review_signals),
        life_score=current_life_score,
        days_since_last_active=decay.days_since_last_active,
        review_signals=review_signals,
    )


def _normalize_surface_mode(mode: str) -> MemorySurfaceMode:
    return mode if mode in _SURFACE_MODES else "balanced"


def _surface_reason(
    memory: MemoryRecord,
    decay: MemoryDecayScore,
    *,
    mode: MemorySurfaceMode,
    review_signals: list[MemorySurfaceSignal],
) -> str:
    if mode == "review_due":
        if "expired" in review_signals:
            return "expired_review"
        if "review_due" in review_signals:
            return "review_due"
        if "near_expiry" in review_signals:
            return "near_expiry"
        if "emotion_uncertain" in review_signals:
            return "emotion_uncertain"
        if "sensitive" in review_signals:
            return "sensitive_review"
        if "stale" in review_signals:
            return "stale_review"
        if "low_life" in review_signals:
            return "low_life"
    if mode == "stale":
        return "stale_important"
    if mode == "emotional":
        return "emotional_signal"
    if mode == "important":
        return "important_memory"
    if decay.activation_count == 0 and memory.importance >= 8 and decay.freshness_bonus > 1.1:
        return "fresh_high_importance"
    if memory.importance >= 8:
        return "high_importance"
    return "high_score"


def _surface_reason_text(
    reason: str,
    *,
    review_signals: list[MemorySurfaceSignal],
) -> str:
    labels = {
        "fresh_high_importance": "新近且重要，适合重新看见",
        "high_importance": "高重要度记忆",
        "high_score": "活跃度和新鲜度较高",
        "important_memory": "按重要度优先浮现",
        "emotional_signal": "情绪唤起或情绪偏离较强",
        "stale_important": "长期未被使用但仍有重要度",
        "expired_review": "已经过有效期，建议复核",
        "review_due": "已到复核时间",
        "near_expiry": "即将到达有效期",
        "emotion_uncertain": "高唤起但置信度偏低",
        "sensitive_review": "隐私或敏感记忆需要更高保留门槛",
        "stale_review": "长期未活跃，建议确认是否仍重要",
        "low_life": "生命力偏低，建议整理",
    }
    if reason in labels:
        return labels[reason]
    if review_signals:
        return "包含复核信号，建议检查"
    return reason


def _surface_mode_score(
    memory: MemoryRecord,
    *,
    mode: MemorySurfaceMode,
    decay: MemoryDecayScore,
    life: float,
    review_signals: list[MemorySurfaceSignal],
) -> float:
    if mode == "balanced":
        return decay.final_score
    if mode == "important":
        return _clamp_score(float(memory.importance) * 7.0 + memory.confidence * 15.0 + life * 0.15)
    if mode == "emotional":
        emotion = max(0.0, min(1.0, memory.arousal)) * 70.0
        emotion += abs(max(0.0, min(1.0, memory.valence)) - 0.5) * 60.0
        return _clamp_score(emotion + float(memory.importance) * 2.0)
    if mode == "stale":
        return _clamp_score(
            min(70.0, decay.days_since_last_active * 0.5)
            + float(memory.importance) * 4.0
            + (100.0 - life) * 0.15
        )
    if mode == "review_due":
        signal_scores = {
            "expired": 100.0,
            "review_due": 95.0,
            "near_expiry": 80.0,
            "emotion_uncertain": 72.0,
            "sensitive": 66.0,
            "stale": 58.0,
            "low_life": 48.0,
        }
        top_signal = max((signal_scores[signal] for signal in review_signals), default=0.0)
        return _clamp_score(top_signal + float(memory.importance) * 2.0)
    return decay.final_score


def _surface_review_signals(
    memory: MemoryRecord,
    *,
    decay: MemoryDecayScore,
    life: float,
    now: datetime,
) -> list[MemorySurfaceSignal]:
    signals: list[MemorySurfaceSignal] = []
    valid_until = _parse_iso_datetime(memory.valid_until)
    if valid_until is not None:
        if valid_until < now:
            signals.append("expired")
        elif valid_until <= now + timedelta(days=_NEAR_EXPIRY_DAYS):
            signals.append("near_expiry")

    review_after = _parse_iso_datetime(memory.review_after)
    if review_after is not None and review_after <= now:
        signals.append("review_due")

    if memory.sensitivity != "normal":
        signals.append("sensitive")
    if decay.days_since_last_active >= _STALE_DAYS and memory.importance >= 6:
        signals.append("stale")
    if memory.arousal >= 0.7 and memory.confidence <= 0.55:
        signals.append("emotion_uncertain")
    if life <= _LOW_LIFE_THRESHOLD:
        signals.append("low_life")
    return signals


def _refresh_hit_decay(hit: MemorySearchHit) -> None:
    _refresh_hit_ranking(hit)


def _refresh_hit_ranking(
    hit: MemorySearchHit,
    *,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(UTC)
    semantic_score = hit.score_breakdown.get("semantic_score", 0.0)
    keyword_score = hit.score_breakdown.get("keyword_score", 0.0)
    decay = score_memory(hit.memory, now=current)
    hit.topic_score = max(semantic_score, keyword_score, hit.topic_score)
    hit.total_score = _total_rank_score(
        hit.memory,
        topic_score=hit.topic_score,
        decay=decay,
        now=current,
    )
    hit.relevance = _final_relevance(hit.total_score)
    hit.final_score = decay.final_score
    hit.activation_count = decay.activation_count
    hit.last_active_at = decay.last_active_at
    hit.freshness_bonus = decay.freshness_bonus
    hit.score_breakdown = _build_score_breakdown(
        memory=hit.memory,
        semantic_score=semantic_score,
        keyword_score=keyword_score,
        decay=decay,
        final_relevance=hit.relevance,
    )


def _build_score_breakdown(
    *,
    memory: MemoryRecord,
    semantic_score: float,
    keyword_score: float,
    decay: MemoryDecayScore,
    final_relevance: float,
) -> dict[str, float]:
    recency_score = math.exp(-0.05 * decay.days_since_last_active) * 100.0
    usage_score = min(100.0, math.log1p(max(0, memory.usage_count or 0)) * 25.0)
    emotion_score = (
        max(0.0, min(1.0, memory.arousal)) * 70.0
        + abs(max(0.0, min(1.0, memory.valence)) - 0.5) * 60.0
    )
    return {
        "semantic_score": _clamp_score(semantic_score),
        "keyword_score": _clamp_score(keyword_score),
        "importance_score": _clamp_score(float(memory.importance) * 10.0),
        "recency_score": _clamp_score(recency_score),
        "usage_score": _clamp_score(usage_score),
        "emotion_score": _clamp_score(emotion_score),
        "final_score": _clamp_score(final_relevance),
    }


def _score_breakdown_payload(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return _empty_score_breakdown()
    payload = _empty_score_breakdown()
    for key in payload:
        payload[key] = _clamp_score(_float_payload(value.get(key)))
    return payload


def _empty_score_breakdown() -> dict[str, float]:
    return {
        "semantic_score": 0.0,
        "keyword_score": 0.0,
        "importance_score": 0.0,
        "recency_score": 0.0,
        "usage_score": 0.0,
        "emotion_score": 0.0,
        "final_score": 0.0,
    }


def _final_relevance(total_score: float) -> float:
    return _clamp_score(total_score / 4.5)


def _clamp_score(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _total_rank_score(
    memory: MemoryRecord,
    *,
    topic_score: float,
    decay: MemoryDecayScore,
    now: datetime,
) -> float:
    time_score = min(decay.final_score, 10.0)
    freshness_score = max(0.0, decay.freshness_bonus - 1.0) * 10.0
    penalty = _metadata_penalty(memory, now) * 20.0
    return (
        topic_score * 4.0
        + time_score * 1.5
        + float(memory.importance)
        + freshness_score * 0.5
        - penalty
    )


def _metadata_penalty(memory: MemoryRecord, now: datetime) -> float:
    return (
        _decay_penalty(memory, now)
        + _validity_penalty(memory, now, embedding_mode=False)
        + _sensitivity_penalty(memory, embedding_mode=False)
    )


def _memory_is_locally_sensitive(memory: MemoryRecord) -> bool:
    if memory.sensitivity != "normal":
        return True
    text = "\n".join(
        part
        for part in (memory.content, memory.source_message, *memory.entities)
        if part
    )
    return detect_text_sensitivity(text) != "normal"


def _float_payload(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _decay_penalty(memory: MemoryRecord, now: datetime) -> float:
    rate, cap, grace_days = {
        "episodic": (0.0020, 0.40, 14),
        "semantic": (0.0010, 0.25, 30),
        "procedural": (0.0005, 0.12, 60),
        "emotional": (0.0003, 0.08, 60),
        "reflective": (0.0004, 0.10, 60),
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
        "private": 0.12 if embedding_mode else 0.50,
        "sensitive": 0.25 if embedding_mode else 1.20,
    }
    return penalties.get(memory.sensitivity, 0.0)
