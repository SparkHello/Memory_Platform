from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
import hashlib
import heapq
import json
import math
import re
import threading
import time

import anyio
import httpx

from app.llm.model_gateway import (
    ModelGatewayProtocolError,
    parse_model_gateway_metadata,
    validate_model_gateway_metadata,
)
from app.memory.decay import MemoryDecayScore, life_score, score_memory
from app.memory.models import MemoryRecord, MemorySurfaceMode, MemorySurfaceSignal
from app.memory.redaction import detect_text_sensitivity
from app.memory.store import MemoryStore
from app.memory.temporal import (
    memory_matches_temporal_mode,
    temporal_query_mode,
    temporal_query_window,
)
from app.memory.utils import _memory_embedding_vector, _parse_iso_datetime, _terms
from app.usage.context import current_usage_context, model_usage_scope
from app.usage.recorder import UsageRecorder
from app.usage.attribution import model_gateway_usage_headers


# ---------------------------------------------------------------------------
# 模块级缓存（跨请求存活）
# ---------------------------------------------------------------------------
_EMBEDDING_CACHE: dict[tuple, tuple[float, list[float]]] = {}
"""L1: (user_id, embedding_space_id, normalized_query) -> cached query vector."""
_EMBEDDING_CACHE_LOCK = threading.Lock()

SEARCH_CACHE: dict[tuple, tuple[float, str, int, list[dict[str, object]]]] = {}
"""L2 keys include embedding_space_id so results never cross vector spaces."""
_SEARCH_CACHE_LOCK = threading.Lock()

_EMBEDDING_CACHE_MAX = 512
_SEARCH_CACHE_MAX = 256
_EMBEDDING_CACHE_TTL = 300   # 5 分钟
_SEARCH_CACHE_TTL = 120       # 2 分钟
_CACHE_METRICS: dict[str, dict[str, int]] = {}
_CACHE_METRICS_LOCK = threading.Lock()
_CACHE_METRICS_MAX_USERS = 1024
_RECALL_LIMIT = 20
# 单次检索最多激活的记忆条数：只有真正进入回答的头部命中才应获得
# usage/activation 强化，避免"被检索曝光"就无限自增的正反馈。
ACTIVATION_LIMIT = 5
# The evaluation workbench still uses this bounded preview size. Live recall no
# longer uses it: correctness must not depend on an importance-ordered cutoff.
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
_GENERIC_METADATA_LABELS = {
    "偏好",
    "工具",
    "项目",
    "经历",
    "信息",
    "情况",
    "时间事实",
    "沟通偏好",
}
# Keyword fallback cannot infer even common hypernym/hyponym relations from
# character n-grams.  Keep a deliberately small, auditable taxonomy for broad
# category questions; embeddings remain responsible for open-ended semantics.
_KEYWORD_CATEGORY_EXPANSIONS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("宠物", "pet", "pets"),
        ("宠物", "猫", "狗", "犬", "兔", "鸟", "hamster", "cat", "dog", "pet"),
    ),
    (
        ("数码产品", "数码设备", "电子产品", "consumer electronics"),
        (
            "设备",
            "硬件",
            "电脑",
            "笔记本",
            "手机",
            "平板",
            "耳机",
            "相机",
            "镜头",
            "显卡",
            "散热器",
            "computer",
            "laptop",
            "phone",
            "tablet",
            "headphone",
            "camera",
        ),
    ),
    (
        ("电脑", "计算机", "computer", "pc"),
        ("电脑", "计算机", "笔记本", "台式机", "主机", "computer", "laptop", "desktop", "pc"),
    ),
    (
        ("拍照", "摄影", "photography"),
        ("拍照", "摄影", "拍摄", "照片", "相片", "photo", "photography"),
    ),
)
_USER_FOOD_PREFERENCE_QUERY_RE = re.compile(
    r"(?:喜欢|爱|偏好).{0,4}(?:吃|喝|食物|饮食)|(?:吃|喝).{0,4}(?:什么|哪些)"
    r"|\b(?:what|which).{0,20}(?:user|they|he|she).{0,20}(?:eat|drink|food)"
    r"|\b(?:user|they|he|she).{0,20}(?:like|love|prefer).{0,10}(?:eat|drink|food)\b",
    re.IGNORECASE,
)
_USER_FOOD_STATEMENT_RE = re.compile(
    r"^(?:用户|我|本人)(?:自己|平时|通常|经常|常常|每天|早餐|午餐|晚餐|也|会)?"
    r"(?:明确)?(?:(?:喜欢|爱|偏好|不喜欢|不爱).{0,4}(?:吃|喝|食物|饮食)"
    r"|(?:常吃|常喝|吃|喝))"
    r"|^(?:用户|我|本人).{0,8}(?:饮食|食物|口味)(?:偏好|习惯)"
    r"|^(?:the\s+)?user.{0,12}(?:like|love|prefer|eat|drink|food)",
    re.IGNORECASE,
)
_PHOTO_EQUIPMENT_QUERY_RE = re.compile(
    r"(?:拍照|摄影|拍摄).{0,5}(?:设备|器材|相机|镜头|型号)"
    r"|(?:设备|器材|相机|镜头|型号).{0,5}(?:拍照|摄影|拍摄)"
)
_PHOTO_EQUIPMENT_STATEMENT_RE = re.compile(
    r"(?:拍照|摄影|拍摄).{0,12}(?:设备|器材|相机|镜头|型号)"
    r"|(?:设备|器材|相机|镜头|型号).{0,12}(?:拍照|摄影|拍摄)"
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
        if user_id not in _CACHE_METRICS and len(_CACHE_METRICS) >= _CACHE_METRICS_MAX_USERS:
            _CACHE_METRICS.pop(next(iter(_CACHE_METRICS)), None)
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


_SENTENCE_SPLIT_RE = re.compile(r"[。！？；\n]+|[!?;]+(?:\s+|$)|\.(?:\s+|$)")
_MAX_QUERY_SENTENCES = 4


def _query_sentences(query: str) -> list[str]:
    """把"新事实+提问"式多意图消息按中英句读切成独立召回意图。

    单句消息返回空列表（不启用多路召回）；子句数量有上限，防止长消息
    放大 embedding 调用成本。
    """
    text = " ".join(query.split())
    sentences: list[str] = []
    for part in _SENTENCE_SPLIT_RE.split(text):
        cleaned = part.strip(" ，,、·…~—-")
        if not cleaned:
            continue
        compact = re.sub(r"\s+", "", cleaned)
        # 过短碎片（"好的"、"OK"）没有独立召回价值。
        if len(compact) < 4:
            continue
        sentences.append(cleaned)
    if len(sentences) < 2:
        return []
    return sentences[:_MAX_QUERY_SENTENCES]


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
    # Empty means the vector space is unknown and therefore unsafe for memory
    # vector comparison. Non-memory consumers may still use the raw vectors.
    embedding_space_id: str = ""

    @abstractmethod
    async def embed(self, text: str) -> list[float] | None:
        raise NotImplementedError

    async def embed_many(
        self,
        texts: list[str],
        *,
        screen_sensitivity: bool = True,
    ) -> list[list[float] | None]:
        # This fallback always screens through embed().  Only subclasses that
        # own the provider call may honour screen_sensitivity, so the generic
        # path can never become the permissive one by accident.
        return [await self.embed(text) for text in texts]


class NullEmbeddingClient(EmbeddingClient):
    async def embed(self, text: str) -> list[float] | None:
        return None

    async def embed_many(
        self,
        texts: list[str],
        *,
        screen_sensitivity: bool = True,
    ) -> list[list[float] | None]:
        return [None for _ in texts]


class OpenAICompatibleEmbeddingClient(EmbeddingClient):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 1024,
        expected_space_id: str = "",
        model_gateway_mode: bool = False,
        timeout_seconds: float = 60.0,
        allow_sensitive_egress: bool = False,
        usage_recorder: UsageRecorder | None = None,
        usage_hmac_secret: str = "",
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.expected_space_id = " ".join(expected_space_id.strip().split())
        self.embedding_space_id = self.expected_space_id
        self.model_gateway_mode = bool(model_gateway_mode)
        self.timeout_seconds = timeout_seconds
        self.allow_sensitive_egress = allow_sensitive_egress
        self.usage_recorder = usage_recorder
        self.usage_hmac_secret = usage_hmac_secret

    async def embed(self, text: str) -> list[float] | None:
        if not self.allow_sensitive_egress and detect_text_sensitivity(text) != "normal":
            return None
        vectors = await self._request_embeddings(text)
        return vectors[0] if vectors else None

    async def embed_many(
        self,
        texts: list[str],
        *,
        screen_sensitivity: bool = True,
    ) -> list[list[float] | None]:
        if not texts:
            return []
        allowed_indices: list[int] = []
        allowed_texts: list[str] = []
        results: list[list[float] | None] = [None for _ in texts]
        for index, text in enumerate(texts):
            if (
                self.allow_sensitive_egress
                or not screen_sensitivity
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
        if self.model_gateway_mode:
            context_operation = current_usage_context().operation
            headers.update(
                model_gateway_usage_headers(
                    signing_secret=self.usage_hmac_secret,
                    operation=(
                        context_operation
                        if context_operation != "unspecified"
                        else "memory.embedding"
                    ),
                )
            )
        payload = {
            "model": self.model,
            "input": input_value,
            "encoding_format": "float",
            "dimensions": self.dimensions,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                metadata = parse_model_gateway_metadata(response.headers)
                if self.model_gateway_mode:
                    try:
                        validate_model_gateway_metadata(
                            metadata,
                            expected_route=self.model,
                            expected_embedding_space=self.expected_space_id,
                            expected_embedding_dimensions=self.dimensions,
                        )
                    except ModelGatewayProtocolError:
                        return []
                elif (
                    self.expected_space_id
                    and metadata.embedding_space_id != self.expected_space_id
                ):
                    return []
                data = response.json()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return []

        if (
            self.usage_recorder is not None
            and not self.model_gateway_mode
            and isinstance(data, dict)
        ):
            await anyio.to_thread.run_sync(
                partial(
                    self.usage_recorder.record_response,
                    payload=data,
                    model=(
                        metadata.upstream_model
                        if self.model_gateway_mode
                        else self.model
                    ),
                    kind="embedding",
                    base_url=self.base_url,
                    provider_override=(
                        metadata.channel_operator if self.model_gateway_mode else ""
                    ),
                    use_local_pricing=not self.model_gateway_mode,
                )
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
                vector = [float(value) for value in item["embedding"]]
                if len(vector) != self.dimensions or any(
                    not math.isfinite(value) for value in vector
                ):
                    continue
                by_index[response_index] = vector
            return [by_index.get(index) for index in range(expected_count)]
        except (IndexError, TypeError, ValueError):
            return []


def embedding_space_id_for(client: object) -> str:
    value = getattr(client, "embedding_space_id", "")
    return " ".join(str(value).strip().split()) if value else ""


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
        embedding_space_id = embedding_space_id_for(self.embedding_client)

        l2_key = (
            user_id,
            normalized,
            capped_limit,
            bool(include_sensitive),
            embedding_space_id,
        )
        if self.enable_cache:
            cached_hits = await anyio.to_thread.run_sync(
                partial(
                    self._cached_hits_with_usage,
                    l2_key,
                    user_id=user_id,
                    query=query,
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
        sentence_queries = _query_sentences(query)
        sentence_embeddings = (
            await self._sentence_embeddings(sentence_queries, user_id=user_id, now=now)
            if sentence_queries
            else []
        )

        candidate_limit = _RECALL_LIMIT * 2 if self.enable_cache else capped_limit
        hits = await anyio.to_thread.run_sync(
            partial(
                self._recall_and_rank,
                user_id=user_id,
                query=query,
                query_embedding=query_embedding,
                sentence_queries=sentence_queries,
                sentence_embeddings=sentence_embeddings,
                embedding_space_id=embedding_space_id,
                limit=candidate_limit,
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
                requested_limit=capped_limit,
            )
        )

    def _cached_hits_with_usage(
        self,
        key: tuple,
        *,
        user_id: str,
        query: str,
        now: float,
        record_usage: bool,
    ) -> list[MemorySearchHit] | None:
        cached_hits = self._cached_search_hits(
            key,
            user_id=user_id,
            query=query,
            now=now,
        )
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
        embedding_space_id: str,
        limit: int,
        include_sensitive: bool,
        sentence_queries: list[str] | None = None,
        sentence_embeddings: list[list[float]] | None = None,
    ) -> list[MemorySearchHit]:
        now = datetime.now(UTC)
        temporal_mode = temporal_query_mode(query, now=now)
        temporal_window = temporal_query_window(query)
        # 多意图消息按句多路召回：整条消息 + 每个子句各自评分，取并集，
        # 每条记忆保留最高分。channels/score_breakdown 解释字段保持不变。
        keyword_variants = [query, *(sentence_queries or [])]
        query_embeddings = [
            vector
            for vector in (query_embedding, *(sentence_embeddings or []))
            if vector
        ]
        query_terms: set[str] = set()
        for variant in keyword_variants:
            query_terms |= _terms(_keyword_query_text(variant))
        document_frequency = {term: 0 for term in query_terms}
        document_count = 0

        def eligible(memories: list[MemoryRecord]) -> list[MemoryRecord]:
            return [
                memory
                for memory in memories
                if memory.origin == "user_asserted"
                and (include_sensitive or not _memory_is_locally_sensitive(memory))
                and memory_matches_temporal_mode(
                    memory,
                    mode=temporal_mode,
                    now=now,
                    query_window=temporal_window,
                )
                and not _query_memory_subject_conflict(query, memory)
            ]

        embedding_heap: list[tuple[float, int, str, int, MemoryRecord]] = []
        keyword_heap: list[tuple[float, int, str, int, MemoryRecord]] = []
        sequence = 0

        # 大库、纯关键词查询时先用 FTS5 索引把候选缩小到"共享至少一个
        # 查询词"的记忆，再用原有打分精排；embedding 查询、单字/类别
        # 通道和小库都返回 None，走下面的全表扫描路径。
        fts_candidates = (
            self._fts_keyword_candidates(keyword_variants, user_id=user_id)
            if not query_embeddings
            else None
        )
        if fts_candidates is not None:
            page = eligible(fts_candidates)
            page_keyword_best: dict[str, tuple[float, MemoryRecord]] = {}
            for variant in keyword_variants:
                for score, memory in self._score_by_keywords(page, variant):
                    previous = page_keyword_best.get(memory.id)
                    if previous is None or score > previous[0]:
                        page_keyword_best[memory.id] = (score, memory)
            for score, memory in page_keyword_best.values():
                sequence += 1
                _push_bounded_scored_memory(
                    keyword_heap,
                    score=score,
                    memory=memory,
                    sequence=sequence,
                )
        else:
            # A keyword score contains corpus-wide metadata IDF.  The first
            # pass computes those statistics; the second pass ranks from the
            # exact same SQLite snapshot and keeps only bounded global channel
            # candidates.
            with self.store.memory_recall_snapshot(user_id=user_id) as read_pages:
                for page in read_pages():
                    page = eligible(page)
                    document_count += len(page)
                    for memory in page:
                        all_terms = (
                            _terms(memory.content)
                            | _terms(" ".join(memory.topics))
                            | _terms(" ".join(memory.entities))
                        )
                        for term in query_terms & all_terms:
                            document_frequency[term] += 1

                for page in read_pages():
                    page = eligible(page)
                    if query_embeddings:
                        for score, memory in self._score_by_embedding(
                            page,
                            query_embeddings,
                            embedding_space_id=embedding_space_id,
                        ):
                            sequence += 1
                            _push_bounded_scored_memory(
                                embedding_heap,
                                score=score,
                                memory=memory,
                                sequence=sequence,
                            )
                    page_keyword_best = {}
                    for variant in keyword_variants:
                        for score, memory in self._score_by_keywords(
                            page,
                            variant,
                            document_frequency=document_frequency,
                            document_count=document_count,
                        ):
                            previous = page_keyword_best.get(memory.id)
                            if previous is None or score > previous[0]:
                                page_keyword_best[memory.id] = (score, memory)
                    for score, memory in page_keyword_best.values():
                        sequence += 1
                        _push_bounded_scored_memory(
                            keyword_heap,
                            score=score,
                            memory=memory,
                            sequence=sequence,
                        )

        combined: dict[str, MemorySearchHit] = {}
        for score, memory in _scored_memories_descending(embedding_heap):
            _upsert_hit(combined, memory, score, "embedding", now=now)
        for score, memory in _scored_memories_descending(keyword_heap):
            _upsert_hit(combined, memory, score, "keyword", now=now)
        hits = list(combined.values())
        hits.sort(
            key=lambda hit: (hit.total_score, hit.topic_score, hit.memory.updated_at),
            reverse=True,
        )
        return hits[:limit]

    def _fts_keyword_candidates(
        self,
        keyword_variants: list[str],
        *,
        user_id: str,
    ) -> list[MemoryRecord] | None:
        """尝试用 FTS5 索引生成关键词候选；返回 None 表示走全表扫描。

        单字 CJK 与类别标记通道在打分层不要求共享查询词，term 索引无法
        为它们生成完整候选，出现时整体回退，保证召回不缩水。
        """
        all_terms: set[str] = set()
        for variant in keyword_variants:
            keyword_query = _keyword_query_text(variant)
            if _single_cjk_keyword(keyword_query) is not None:
                return None
            query_lower = keyword_query.lower()
            compact_query = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", query_lower)
            if _keyword_category_markers(
                query_lower=query_lower,
                compact_query=compact_query,
            ):
                return None
            all_terms |= _terms(keyword_query)
        if not all_terms:
            return None
        try:
            return self.store.keyword_candidate_memories(
                user_id=user_id,
                terms=sorted(all_terms),
            )
        except Exception:
            # 索引层任何故障都不能影响检索本身；回退全表扫描。
            return None

    def _finalize_hits(
        self,
        key: tuple,
        hits: list[MemorySearchHit],
        *,
        user_id: str,
        record_usage: bool,
        now: float,
        requested_limit: int,
    ) -> list[MemorySearchHit]:
        if self.enable_cache:
            self._cache_search_hits(key, hits, user_id=user_id, now=now)
        return self._record_hit_usage(
            hits[:requested_limit],
            user_id=user_id,
            record_usage=record_usage,
        )

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
        embedding_space_id = embedding_space_id_for(self.embedding_client)
        if not embedding_space_id:
            self.last_embedding_cache_status = (
                "disabled"
                if isinstance(self.embedding_client, NullEmbeddingClient)
                else "space-unavailable"
            )
            return None
        l1_key = (user_id, embedding_space_id, normalized_query)
        cached_embedding: tuple[float, list[float]] | None = None
        if self.enable_cache:
            with _EMBEDDING_CACHE_LOCK:
                cached_embedding = _EMBEDDING_CACHE.get(l1_key)
                if cached_embedding is not None and now >= cached_embedding[0]:
                    _EMBEDDING_CACHE.pop(l1_key, None)
                    cached_embedding = None
        if cached_embedding is not None:
            l1_expires_at, l1_vector = cached_embedding
            if now < l1_expires_at:
                self.last_embedding_cache_status = "hit"
                _record_cache_metric(user_id, "embedding_hits")
                return l1_vector

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

    async def _sentence_embeddings(
        self,
        sentences: list[str],
        *,
        user_id: str,
        now: float,
    ) -> list[list[float]]:
        """为多意图子句取向量；逐句复用 L1 缓存，未命中的批量请求。"""
        embedding_space_id = embedding_space_id_for(self.embedding_client)
        if not embedding_space_id or isinstance(
            self.embedding_client, NullEmbeddingClient
        ):
            return []
        vectors: list[list[float] | None] = [None] * len(sentences)
        pending: list[tuple[int, str]] = []
        for index, sentence in enumerate(sentences):
            l1_key = (user_id, embedding_space_id, _normalize_query(sentence))
            if self.enable_cache:
                with _EMBEDDING_CACHE_LOCK:
                    cached = _EMBEDDING_CACHE.get(l1_key)
                if cached is not None and now < cached[0]:
                    vectors[index] = cached[1]
                    continue
            pending.append((index, sentence))
        if pending:
            with model_usage_scope(user_id=user_id, operation="memory_search"):
                fetched = await self.embedding_client.embed_many(
                    [_semantic_query_text(sentence) for _, sentence in pending]
                )
            for (index, sentence), vector in zip(pending, fetched, strict=True):
                if not vector:
                    continue
                vectors[index] = vector
                if self.enable_cache:
                    self._cache_embedding(
                        (user_id, embedding_space_id, _normalize_query(sentence)),
                        vector,
                        now,
                    )
        return [vector for vector in vectors if vector]

    def _cached_search_hits(
        self,
        key: tuple,
        *,
        user_id: str,
        query: str,
        now: float,
    ) -> list[MemorySearchHit] | None:
        with _SEARCH_CACHE_LOCK:
            cached_entry = SEARCH_CACHE.get(key)
        if cached_entry is None:
            return None

        expires_at, max_updated_at, active_count, payloads = cached_entry
        if now >= expires_at:
            _discard_search_cache_entry(key, cached_entry)
            return None

        current_max = self.store.get_memories_max_updated_at(user_id=user_id)
        current_count = self.store.get_active_memory_count(user_id=user_id)
        if not current_max or current_max != max_updated_at or current_count != active_count:
            _discard_search_cache_entry(key, cached_entry)
            return None

        include_sensitive = bool(key[3]) if len(key) > 3 else False
        temporal_mode = temporal_query_mode(query)
        temporal_window = temporal_query_window(query)
        hits: list[MemorySearchHit] = []
        for payload in payloads:
            memory_id = payload.get("id")
            if not isinstance(memory_id, str):
                continue
            memory = self.store.get_memory(memory_id=memory_id, user_id=user_id)
            if memory is None:
                continue
            if memory.origin != "user_asserted" or memory.status == "archived":
                continue
            if not include_sensitive and _memory_is_locally_sensitive(memory):
                continue
            if not memory_matches_temporal_mode(
                memory,
                mode=temporal_mode,
                query_window=temporal_window,
            ):
                continue
            if _query_memory_subject_conflict(query, memory):
                continue
            decay = score_memory(memory)
            channels = payload.get("channels")
            cached_channels = (
                [str(channel) for channel in channels]
                if isinstance(channels, list)
                else ["cache"]
            )
            cached_space_id = str(key[4]) if len(key) > 4 else ""
            if (
                "embedding" in cached_channels
                and (
                    not cached_space_id
                    or memory.embedding_space_id != cached_space_id
                )
            ):
                _discard_search_cache_entry(key, cached_entry)
                return None
            score_breakdown = payload.get("score_breakdown")
            hits.append(
                MemorySearchHit(
                    memory=memory,
                    relevance=_float_payload(payload.get("relevance")),
                    channels=cached_channels,
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
        # Close the update window between the first generation check and the
        # per-row reads. A sensitivity/status/content change must invalidate
        # this reconstruction before any cached result can escape.
        final_max = self.store.get_memories_max_updated_at(user_id=user_id)
        final_count = self.store.get_active_memory_count(user_id=user_id)
        with _SEARCH_CACHE_LOCK:
            entry_is_current = SEARCH_CACHE.get(key) is cached_entry
        if final_max != max_updated_at or final_count != active_count or not entry_is_current:
            _discard_search_cache_entry(key, cached_entry)
            return None
        if not hits:
            _discard_search_cache_entry(key, cached_entry)
            return None
        hits.sort(
            key=lambda hit: (hit.total_score, hit.topic_score, hit.memory.updated_at),
            reverse=True,
        )
        requested_limit = int(key[2]) if len(key) > 2 else len(hits)
        return hits[:requested_limit]

    def _cache_embedding(self, key: tuple, vector: list[float], now: float) -> None:
        with _EMBEDDING_CACHE_LOCK:
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
        max_updated = self.store.get_memories_max_updated_at(user_id=user_id)
        active_count = self.store.get_active_memory_count(user_id=user_id)
        if max_updated:
            expires_at = now + _SEARCH_CACHE_TTL
            next_boundary = self.store.get_next_temporal_boundary(
                user_id=user_id,
                after=datetime.fromtimestamp(now, tz=UTC),
            )
            if next_boundary is not None:
                expires_at = min(expires_at, next_boundary.timestamp())
            entry = (
                expires_at,
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
            with _SEARCH_CACHE_LOCK:
                if len(SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
                    _cleanup_expired(SEARCH_CACHE, now)
                if len(SEARCH_CACHE) < _SEARCH_CACHE_MAX:
                    SEARCH_CACHE[key] = entry

    def _score_by_embedding(
        self,
        memories: list[MemoryRecord],
        query_embeddings: list[list[float]],
        *,
        embedding_space_id: str,
    ) -> list[tuple[float, MemoryRecord]]:
        if not embedding_space_id or not query_embeddings:
            return []
        scored: list[tuple[float, MemoryRecord]] = []
        for memory in memories:
            memory_embedding = _memory_embedding_vector(
                memory,
                expected_space_id=embedding_space_id,
            )
            if memory_embedding is None:
                continue
            # 多意图消息按子句取最高相似度，避免整句向量稀释单个意图。
            cosine = max(
                cosine_similarity(query_embedding, memory_embedding)
                for query_embedding in query_embeddings
            )
            score = cosine * 100.0
            if cosine >= EMBEDDING_MIN_COSINE:
                scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    def _score_by_keywords(
        self,
        memories: list[MemoryRecord],
        query: str,
        *,
        document_frequency: dict[str, int] | None = None,
        document_count: int | None = None,
    ) -> list[tuple[float, MemoryRecord]]:
        keyword_query = _keyword_query_text(query)
        query_terms = _terms(keyword_query)
        single_cjk_keyword = _single_cjk_keyword(keyword_query)
        if not query_terms and single_cjk_keyword is None:
            return []
        scored: list[tuple[float, MemoryRecord]] = []
        query_lower = keyword_query.lower()
        compact_query = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", query_lower)
        allow_substring_match = len(compact_query) >= 2
        category_markers = _keyword_category_markers(
            query_lower=query_lower,
            compact_query=compact_query,
        )

        indexed: list[
            tuple[MemoryRecord, str, set[str], set[str], set[str], list[str]]
        ] = []
        computed_document_frequency = {term: 0 for term in query_terms}
        for memory in memories:
            content_lower = memory.content.lower()
            content_terms = _terms(memory.content)
            topic_terms = _terms(" ".join(memory.topics))
            entity_terms = _terms(" ".join(memory.entities))
            labels = [
                label.strip().lower()
                for label in (*memory.topics, *memory.entities)
                if label.strip()
            ]
            indexed.append(
                (memory, content_lower, content_terms, topic_terms, entity_terms, labels)
            )
            all_terms = content_terms | topic_terms | entity_terms
            for term in query_terms & all_terms:
                computed_document_frequency[term] += 1

        if document_frequency is None:
            document_frequency = computed_document_frequency
        effective_document_count = max(
            1,
            len(indexed) if document_count is None else int(document_count),
        )

        for memory, content_lower, content_terms, topic_terms, entity_terms, labels in indexed:
            shared_terms = query_terms & content_terms
            substring_match = (
                allow_substring_match
                and bool(compact_query)
                and compact_query in re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", content_lower)
            )
            single_cjk_match = bool(
                single_cjk_keyword and single_cjk_keyword in content_lower
            )
            # 字符重叠只能参与精排，不能独自生成候选；否则所有以“用户”开头的
            # 记忆都会被“用户的年龄”之类的无答案 query 误召回。
            metadata_terms = topic_terms | entity_terms
            shared_metadata_terms = query_terms & metadata_terms
            exact_metadata_labels = [
                label
                for label in labels
                if _is_strong_metadata_label_match(label, compact_query)
            ]
            category_match = bool(
                category_markers
                and _memory_matches_category_markers(
                    memory,
                    content_lower=content_lower,
                    markers=category_markers,
                )
            )
            if (
                not shared_terms
                and not substring_match
                and not single_cjk_match
                and not category_match
            ):
                continue
            term_score = min(45.0, len(shared_terms) * 18.0)
            coverage_score = (
                len(shared_terms) / len(query_terms) * 25.0
                if query_terms
                else 0.0
            )
            substring_score = 35.0 if substring_match else 0.0
            single_cjk_score = 45.0 if single_cjk_match else 0.0
            char_score = _char_overlap_score(query_lower, content_lower) * 15.0
            metadata_idf_score = sum(
                (
                    math.log(
                        (effective_document_count + 1.0)
                        / (document_frequency.get(term, 0) + 1.0)
                    )
                    + 1.0
                )
                * (8.0 if term in topic_terms else 5.0)
                for term in shared_metadata_terms
            )
            metadata_score = min(
                45.0,
                metadata_idf_score + min(30.0, len(exact_metadata_labels) * 22.0),
            )
            category_score = 45.0 if category_match else 0.0
            score = min(
                100.0,
                term_score
                + coverage_score
                + substring_score
                + single_cjk_score
                + char_score
                + metadata_score
                + category_score,
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
        activated = hits[:ACTIVATION_LIMIT]
        used_at = self.store.mark_memories_used(
            memory_ids=[hit.memory.id for hit in activated],
            user_id=user_id,
            time_ripple_delta=self.time_ripple_delta,
            time_ripple_window_hours=self.time_ripple_window_hours,
        )
        if used_at:
            for hit in activated:
                hit.memory.usage_count += 1
                hit.memory.last_used_at = used_at
                _refresh_hit_decay(hit)
        return hits


def _cleanup_expired(cache: dict, now: float) -> None:
    expired = [k for k, v in cache.items() if now >= v[0]]
    for k in expired:
        cache.pop(k, None)


def _discard_search_cache_entry(key: tuple, entry: tuple) -> None:
    with _SEARCH_CACHE_LOCK:
        if SEARCH_CACHE.get(key) is entry:
            SEARCH_CACHE.pop(key, None)


_LOW_INFORMATION_SINGLE_CJK = frozenset("我的是有在了和与及他她它吗呢啊吧被把让")


def _single_cjk_keyword(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if (
        len(compact) == 1
        and "\u4e00" <= compact <= "\u9fff"
        and compact not in _LOW_INFORMATION_SINGLE_CJK
    ):
        return compact
    return None


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
    content = memory.content.lstrip()
    if (
        _USER_FOOD_PREFERENCE_QUERY_RE.search(compact_query)
        and not _USER_FOOD_STATEMENT_RE.search(content)
    ):
        return True
    if (
        _PHOTO_EQUIPMENT_QUERY_RE.search(compact_query)
        and not _PHOTO_EQUIPMENT_STATEMENT_RE.search(content)
        and not any(
            label.casefold() in {"拍照设备", "摄影设备", "摄影器材"}
            for label in memory.topics
        )
    ):
        return True
    if not _EXPLICIT_USER_QUERY_RE.match(compact_query):
        return False
    if any(term in compact_query for term in _RELATED_ENTITY_QUERY_TERMS):
        return False
    if memory.temporal_subject and memory.temporal_subject.lower() in {"用户", "user", "我"}:
        return False
    return not content.startswith(("用户", "我", "本人"))


def _keyword_category_markers(
    *,
    query_lower: str,
    compact_query: str,
) -> tuple[str, ...]:
    markers: list[str] = []
    for triggers, expansion in _KEYWORD_CATEGORY_EXPANSIONS:
        if any(
            (
                bool(re.search(rf"\b{re.escape(trigger)}\b", query_lower))
                if trigger.isascii()
                else trigger in compact_query
            )
            for trigger in triggers
        ):
            markers.extend(expansion)
    return tuple(dict.fromkeys(markers))


def _memory_matches_category_markers(
    memory: MemoryRecord,
    *,
    content_lower: str,
    markers: tuple[str, ...],
) -> bool:
    searchable = " ".join(
        (content_lower, *memory.topics, *memory.entities)
    ).casefold()
    return any(
        (
            bool(re.search(rf"\b{re.escape(marker.casefold())}(?:s)?\b", searchable))
            if marker.isascii()
            else marker.casefold() in searchable
        )
        for marker in markers
    )


def _is_strong_metadata_label_match(label: str, compact_query: str) -> bool:
    compact_label = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", label.casefold())
    if len(compact_label) < 2 or compact_label in _GENERIC_METADATA_LABELS:
        return False
    return compact_label in compact_query or compact_query in compact_label


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


def _push_bounded_scored_memory(
    heap: list[tuple[float, int, str, int, MemoryRecord]],
    *,
    score: float,
    memory: MemoryRecord,
    sequence: int,
) -> None:
    """Keep the globally best raw channel scores in bounded memory."""
    item = (
        score,
        memory.importance,
        memory.updated_at,
        -sequence,
        memory,
    )
    if len(heap) < _RECALL_LIMIT:
        heapq.heappush(heap, item)
    elif item[:4] > heap[0][:4]:
        heapq.heapreplace(heap, item)


def _scored_memories_descending(
    heap: list[tuple[float, int, str, int, MemoryRecord]],
) -> list[tuple[float, MemoryRecord]]:
    return [(item[0], item[4]) for item in sorted(heap, reverse=True)]


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
