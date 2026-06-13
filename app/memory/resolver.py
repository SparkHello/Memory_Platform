import json
import re

from app.memory.models import CandidateMemory, MemoryRecord, ResolveResult
from app.memory.search import EmbeddingClient, cosine_similarity
from app.memory.store import MemoryStore

# embedding 余弦相似度达到该值即视为同主题旧记忆
EMBEDDING_SIMILARITY_THRESHOLD = 0.80
# 无向量可用时退化为词重叠（Jaccard）判断
TERM_SIMILARITY_THRESHOLD = 0.5


class MemoryResolver:
    """决定候选记忆的落库方式：创建、更新旧记忆，还是忽略。

    能走到这里的候选都已通过 extractor 的保存校验（明确表达、非假设、
    高置信度），所以与旧记忆冲突时直接以新内容为准。
    """

    def __init__(self, *, store: MemoryStore, embedding_client: EmbeddingClient):
        self.store = store
        self.embedding_client = embedding_client

    async def resolve(
        self,
        *,
        user_id: str,
        candidate: CandidateMemory,
        source_message: str | None = None,
        conversation_id: str | None = None,
    ) -> ResolveResult:
        existing = self.store.list_memories(user_id=user_id, limit=200)
        normalized_new = _normalize(candidate.memory)

        for memory in existing:
            normalized_old = _normalize(memory.content)
            if normalized_old == normalized_new:
                return ResolveResult(action="ignore", memory=memory, reason="已有相同记忆")
            if normalized_new in normalized_old:
                return ResolveResult(action="ignore", memory=memory, reason="已有更完整的同主题记忆")

        vector = await self.embedding_client.embed(candidate.memory)
        embedding_json = json.dumps(vector, ensure_ascii=False) if vector else None

        target, update_reason = _find_update_target(candidate, existing, vector, normalized_new)
        if target:
            updated = self.store.update_memory(
                memory_id=target.id,
                user_id=user_id,
                content=candidate.memory,
                type=candidate.type,
                importance=max(candidate.importance, target.importance),
                confidence=candidate.confidence,
                source_message=source_message,
                source_conversation_id=conversation_id,
                embedding_json=embedding_json or target.embedding_json,
            )
            return ResolveResult(action="update", memory=updated, reason=update_reason)

        created = self.store.create_memory(
            user_id=user_id,
            content=candidate.memory,
            type=candidate.type,
            importance=candidate.importance,
            confidence=candidate.confidence,
            source_message=source_message,
            source_conversation_id=conversation_id,
            embedding_json=embedding_json,
        )
        return ResolveResult(action="create", memory=created, reason="没有相似旧记忆，创建新记忆")


def _find_update_target(
    candidate: CandidateMemory,
    existing: list[MemoryRecord],
    vector: list[float] | None,
    normalized_new: str,
) -> tuple[MemoryRecord | None, str]:
    # 新内容完整包含旧内容：视为补充细节
    for memory in existing:
        normalized_old = _normalize(memory.content)
        if normalized_old and normalized_old in normalized_new:
            return memory, "新信息补充了旧记忆的细节"

    # 向量相似：同主题改写或用户明确表达的新事实
    if vector:
        best, best_score = None, 0.0
        for memory in existing:
            old_vector = _load_vector(memory.embedding_json)
            if old_vector is None:
                continue
            score = cosine_similarity(vector, old_vector)
            if score > best_score:
                best, best_score = memory, score
        if best and best_score >= EMBEDDING_SIMILARITY_THRESHOLD:
            return best, "用户明确表达了新信息，更新同主题旧记忆"

    # 无向量时退化为同类型词重叠
    best, best_score = None, 0.0
    for memory in existing:
        if memory.type != candidate.type:
            continue
        score = _term_jaccard(candidate.memory, memory.content)
        if score > best_score:
            best, best_score = memory, score
    if best and best_score >= TERM_SIMILARITY_THRESHOLD:
        return best, "用户明确表达了新信息，更新同主题旧记忆"

    return None, ""


def _load_vector(embedding_json: str | None) -> list[float] | None:
    if not embedding_json:
        return None
    try:
        data = json.loads(embedding_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    try:
        return [float(value) for value in data]
    except (TypeError, ValueError):
        return None


def _term_jaccard(left: str, right: str) -> float:
    left_terms = _terms(left)
    right_terms = _terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _terms(text: str) -> set[str]:
    return {term.lower() for term in re.findall(r"[A-Za-z0-9_一-鿿]+", text)}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("。.!?！？").lower()
