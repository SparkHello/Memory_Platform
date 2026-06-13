import json
import re

from app.memory.models import CandidateMemory, MemoryRecord, MemoryRelation, ResolveResult
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
                return ResolveResult(
                    action="ignore",
                    memory=memory,
                    relation="same",
                    reason="已有相同记忆",
                )
            if normalized_new in normalized_old:
                return ResolveResult(
                    action="ignore",
                    memory=memory,
                    relation="same",
                    reason="已有更完整的同主题记忆",
                )

        vector = await self.embedding_client.embed(candidate.memory)
        embedding_json = json.dumps(vector, ensure_ascii=False) if vector else None

        target, update_reason, relation = _find_update_target(
            candidate, existing, vector, normalized_new
        )
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
                stability=candidate.stability,
                valid_until=candidate.valid_until,
                review_after=candidate.review_after,
                sensitivity=candidate.sensitivity,
                evidence_memory_ids=target.evidence_memory_ids,
            )
            return ResolveResult(
                action="update",
                memory=updated,
                relation=relation,
                reason=update_reason,
            )

        created = self.store.create_memory(
            user_id=user_id,
            content=candidate.memory,
            type=candidate.type,
            importance=candidate.importance,
            confidence=candidate.confidence,
            source_message=source_message,
            source_conversation_id=conversation_id,
            embedding_json=embedding_json,
            stability=candidate.stability,
            valid_until=candidate.valid_until,
            review_after=candidate.review_after,
            sensitivity=candidate.sensitivity,
        )
        return ResolveResult(action="create", memory=created, reason="没有相似旧记忆，创建新记忆")


def _find_update_target(
    candidate: CandidateMemory,
    existing: list[MemoryRecord],
    vector: list[float] | None,
    normalized_new: str,
) -> tuple[MemoryRecord | None, str, MemoryRelation]:
    # 新内容完整包含旧内容：视为补充细节
    for memory in existing:
        normalized_old = _normalize(memory.content)
        if normalized_old and normalized_old in normalized_new:
            return memory, "新信息补充了旧记忆的细节", "supplement"

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
            relation = _related_content_relation(candidate.memory, best.content)
            return best, _update_reason_for_relation(relation), relation

    # 无向量时退化为同类型词重叠
    best, best_score = None, 0.0
    for memory in existing:
        if memory.type != candidate.type:
            continue
        score = _term_jaccard(candidate.memory, memory.content)
        if score > best_score:
            best, best_score = memory, score
    if best and best_score >= TERM_SIMILARITY_THRESHOLD:
        relation = _related_content_relation(candidate.memory, best.content)
        return best, _update_reason_for_relation(relation), relation

    return None, "", "none"


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


def _related_content_relation(new_content: str, old_content: str) -> MemoryRelation:
    if _looks_conflicting(new_content, old_content):
        return "conflict"
    if _looks_superseding(new_content):
        return "supersede"
    return "supersede"


def _update_reason_for_relation(relation: MemoryRelation) -> str:
    return {
        "conflict": "新信息与旧记忆冲突，以用户明确表达的新事实更新旧记忆",
        "supersede": "新信息取代了同主题旧记忆",
        "supplement": "新信息补充了旧记忆的细节",
    }.get(relation, "用户明确表达了新信息，更新同主题旧记忆")


def _looks_conflicting(new_content: str, old_content: str) -> bool:
    if _has_negation(new_content) == _has_negation(old_content):
        return False
    return _char_overlap(new_content, old_content) >= 0.45


def _has_negation(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "不",
        "不是",
        "不再",
        "没有",
        "没",
        "停止",
        "戒掉",
        "讨厌",
        "不喜欢",
        "不喝",
        "no longer",
        "not",
    )
    return any(marker in lowered for marker in markers)


def _looks_superseding(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "现在",
        "已经",
        "改成",
        "改为",
        "改用",
        "换成",
        "换为",
        "不再",
        "取代",
        "instead",
        "switched",
        "now",
    )
    return any(marker in lowered for marker in markers)


def _char_overlap(left: str, right: str) -> float:
    left_chars = {char.lower() for char in left if not char.isspace()}
    right_chars = {char.lower() for char in right if not char.isspace()}
    if not left_chars or not right_chars:
        return 0.0
    return len(left_chars & right_chars) / len(left_chars | right_chars)


def _terms(text: str) -> set[str]:
    return {term.lower() for term in re.findall(r"[A-Za-z0-9_一-鿿]+", text)}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("。.!?！？").lower()
