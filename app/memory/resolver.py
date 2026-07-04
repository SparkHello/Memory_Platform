import json

from app.memory.classification import classify_memory, normalize_classification_values
from app.memory.models import CandidateMemory, MemoryRecord, MemoryRelation, ResolveResult
from app.memory.search import EmbeddingClient, cosine_similarity
from app.memory.store import MemoryStore
from app.memory.utils import _char_overlap, _has_negation, _normalize, _term_jaccard

# embedding 余弦相似度达到该值即视为同主题旧记忆
EMBEDDING_SIMILARITY_THRESHOLD = 0.80
# 无向量可用时退化为词重叠（Jaccard）判断
TERM_SIMILARITY_THRESHOLD = 0.5


class MemoryResolver:
    """决定候选记忆的落库方式：创建、更新旧记忆，还是忽略。

    能走到这里的候选都已通过 extractor 的保存校验（明确表达、非假设、
    高置信度）。相似但非完全重复的记忆默认新建，让体检流程建议人工合并，
    避免自动覆盖用户时间线。
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
        auto_classify: bool = True,
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

        target, related_reason, relation = _find_related_memory(
            candidate, existing, vector, normalized_new
        )
        classification_kwargs = self._classification_kwargs(
            user_id=user_id,
            candidate=candidate,
            source_text=source_message or candidate.source_quote or candidate.memory,
            auto_classify=auto_classify,
        )
        if target:
            created = self.store.create_memory(
                user_id=user_id,
                content=candidate.memory,
                type=candidate.type,
                importance=candidate.importance,
                confidence=candidate.confidence,
                valence=candidate.valence,
                arousal=candidate.arousal,
                source_message=source_message,
                source_conversation_id=conversation_id,
                embedding_json=embedding_json,
                stability=candidate.stability,
                valid_from=candidate.valid_from,
                valid_until=candidate.valid_until,
                review_after=candidate.review_after,
                sensitivity=candidate.sensitivity,
                temporal_subject=candidate.temporal_subject,
                temporal_predicate=candidate.temporal_predicate,
                **classification_kwargs,
            )
            return ResolveResult(
                action="create",
                memory=created,
                relation=relation,
                reason=f"{related_reason}，暂不自动合并，建议体检确认",
            )

        created = self.store.create_memory(
            user_id=user_id,
            content=candidate.memory,
            type=candidate.type,
            importance=candidate.importance,
            confidence=candidate.confidence,
            valence=candidate.valence,
            arousal=candidate.arousal,
            source_message=source_message,
            source_conversation_id=conversation_id,
            embedding_json=embedding_json,
            stability=candidate.stability,
            valid_from=candidate.valid_from,
            valid_until=candidate.valid_until,
            review_after=candidate.review_after,
            sensitivity=candidate.sensitivity,
            temporal_subject=candidate.temporal_subject,
            temporal_predicate=candidate.temporal_predicate,
            **classification_kwargs,
        )
        return ResolveResult(action="create", memory=created, reason="没有相似旧记忆，创建新记忆")


    def _classification_kwargs(
        self,
        *,
        user_id: str,
        candidate: CandidateMemory,
        source_text: str,
        auto_classify: bool,
    ) -> dict:
        if auto_classify:
            classification = classify_memory(candidate, source_text=source_text)
            space_ids = [
                self.store.upsert_memory_space(user_id=user_id, name=name).id
                for name in classification.space_names
            ]
            return {
                "topics": classification.topics,
                "entities": classification.entities,
                "space_ids": space_ids,
            }

        return {
            "topics": normalize_classification_values(
                candidate.topics,
                max_items=20,
                field_name="topics",
            ),
            "entities": normalize_classification_values(
                candidate.entities,
                max_items=20,
                field_name="entities",
            ),
            "space_ids": [],
        }


def _find_related_memory(
    candidate: CandidateMemory,
    existing: list[MemoryRecord],
    vector: list[float] | None,
    normalized_new: str,
) -> tuple[MemoryRecord | None, str, MemoryRelation]:
    # 新内容完整包含旧内容：记录为补充，但不直接覆盖旧记忆。
    for memory in existing:
        normalized_old = _normalize(memory.content)
        if normalized_old and normalized_old in normalized_new:
            return memory, "发现新信息补充了旧记忆的细节", "supplement"

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
            return best, _related_reason_for_relation(relation), relation

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
        return best, _related_reason_for_relation(relation), relation

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


def _related_content_relation(new_content: str, old_content: str) -> MemoryRelation:
    if _looks_conflicting(new_content, old_content):
        return "conflict"
    if _looks_superseding(new_content):
        return "supersede"
    return "supplement"


def _related_reason_for_relation(relation: MemoryRelation) -> str:
    return {
        "conflict": "发现新信息与旧记忆可能冲突",
        "supersede": "发现新信息可能取代同主题旧记忆",
        "supplement": "发现新信息补充了旧记忆的细节",
    }.get(relation, "发现相似旧记忆")


def _looks_conflicting(new_content: str, old_content: str) -> bool:
    if _has_negation(new_content) == _has_negation(old_content):
        return False
    return _char_overlap(new_content, old_content) >= 0.45


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
