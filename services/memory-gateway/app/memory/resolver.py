from collections.abc import Callable
from datetime import UTC, datetime
import json
import re
from functools import partial

import anyio

from app.memory.classification import classify_memory, normalize_classification_values
from app.memory.extraction_hints import (
    _PAST_MARKERS,
    _UNCOMMITTED_INTENT_MARKERS,
    _matches_any,
)
from app.memory.extractor import (
    grounding_subjects_compatible_both_ways,
    grounding_terms_overlap,
    has_text_grounding_anchor,
    shared_relation_families,
    structured_value_kind_changed,
)
from app.memory.models import (
    AutoSupersedeDecision,
    CandidateMemory,
    MemoryRecord,
    MemoryRelation,
    ResolveResult,
)
from app.memory.search import EmbeddingClient, embedding_space_id_for
from app.vector_util import cosine_similarity
from app.memory.store import MemoryStore
from app.memory.temporal import is_current_temporal_memory
from app.memory.utils import (
    _memory_embedding_vector,
    _normalize,
    _parse_iso_datetime,
    _term_jaccard,
    pair_conflict,
)
from app.usage.context import model_usage_scope

# embedding 余弦相似度达到该值即视为同主题旧记忆
EMBEDDING_SIMILARITY_THRESHOLD = 0.80
# 旧记忆在实体、主题和事实细节上覆盖新候选时，可用稍低的向量门槛
# 识别“更完整旧事实的笼统改写”。该门槛不能单独触发忽略。
SEMANTIC_COVERAGE_SIMILARITY_THRESHOLD = 0.70
# 无向量可用时退化为词重叠（Jaccard）判断
TERM_SIMILARITY_THRESHOLD = 0.5
# 已确认相关的两条内容，否定极性不同且字符重叠达到该值时视为冲突
CONFLICT_CHAR_OVERLAP_THRESHOLD = 0.45

_GENERIC_ENTITIES = {
    "个人",
    "本人",
    "用户",
    "设备",
    "电脑",
    "笔记本",
    "笔记本电脑",
}
_INTENT_MARKERS = (
    "准备买",
    "准备购买",
    "打算买",
    "打算购买",
    "计划买",
    "计划购买",
    "考虑买",
    "考虑购买",
    "可能会买",
    "可能购买",
    "想买",
    "想购买",
    "希望买",
    "希望购买",
)


# Automatic supersede only touches these sectors: events are never "replaced"
# and reflective insights accumulate rather than supersede one another.
_AUTO_SUPERSEDE_TYPES = {"semantic", "emotional", "procedural"}
# Preferences and consumption habits are additive (liking tea does not end
# liking coffee), so sharing only these families is never enough on its own;
# an explicit negation conflict is required for them.
_ADDITIVE_RELATION_FAMILIES = {"preference", "consumption"}
_AUTO_SUPERSEDE_REASONS: dict[str, str] = {
    "supersede": "新信息取代同主题旧记忆，已自动替换；旧版本保留为历史，可在记忆详情恢复",
    "conflict": "新信息与旧记忆冲突且带明确转变标记，已自动替换；旧版本保留为历史，可在记忆详情恢复",
}


class MemoryResolver:
    """决定候选记忆的落库方式：创建、更新旧记忆，还是忽略。

    能走到这里的候选都已通过 extractor 的保存校验（明确表达、非假设、
    高置信度）。落库策略分三层：

    1. 白名单时态键（current_city 等）由 ``_apply_temporal_invalidation`` 按键
       关闭旧值；
    2. 无键候选带明确转变标记（换成/改成/现在/不再/取代/switched/now）、与
       某条同类型、同主体、共享属性的活跃旧记忆向量高度相似时，自动关闭旧行
       （保留为历史，可原地恢复）；
    3. 其余相似但不同的记忆仍新建，交给体检建议人工合并，避免误改时间线。
    """

    def __init__(
        self,
        *,
        store: MemoryStore,
        embedding_client: EmbeddingClient,
        auto_supersede: bool = True,
    ):
        self.store = store
        self.embedding_client = embedding_client
        self.auto_supersede = auto_supersede

    async def resolve(
        self,
        *,
        user_id: str,
        candidate: CandidateMemory,
        source_message: str | None = None,
        conversation_id: str | None = None,
        auto_classify: bool = True,
    ) -> ResolveResult:
        existing = await anyio.to_thread.run_sync(
            partial(self.store.list_memories_for_resolution, user_id=user_id)
        )
        normalized_new = _normalize(candidate.memory)

        for memory in existing:
            if not _can_suppress_new_candidate(candidate, memory):
                continue
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

        with model_usage_scope(user_id=user_id, operation="memory_write"):
            vector = await self.embedding_client.embed(candidate.memory)
        embedding_space_id = embedding_space_id_for(self.embedding_client)
        if not embedding_space_id:
            vector = None
        embedding_json = json.dumps(vector, ensure_ascii=False) if vector else None

        covered_by = await anyio.to_thread.run_sync(
            partial(
                _find_semantically_covering_memory,
                candidate,
                existing,
                vector,
                embedding_space_id,
            )
        )
        if covered_by is not None:
            return ResolveResult(
                action="ignore",
                memory=covered_by,
                relation="same",
                reason="已有更完整的语义等价记忆",
            )

        target, related_reason, relation = await anyio.to_thread.run_sync(
            partial(
                _find_related_memory,
                candidate,
                existing,
                vector,
                embedding_space_id,
                normalized_new,
            )
        )
        classification_kwargs = await anyio.to_thread.run_sync(
            partial(
                self._classification_kwargs,
                user_id=user_id,
                candidate=candidate,
                # Classification belongs to this candidate's grounded clause.
                # The full turn may contain unrelated facts for other candidates.
                source_text=candidate.source_quote or source_message or candidate.memory,
                auto_classify=auto_classify,
            )
        )
        final_suppression: tuple[MemoryRecord, str] | None = None

        def final_matcher(latest: list[MemoryRecord]) -> MemoryRecord | None:
            nonlocal final_suppression
            final_suppression = _find_suppressing_memory(
                candidate=candidate,
                existing=latest,
                normalized_new=normalized_new,
                vector=vector,
                embedding_space_id=embedding_space_id,
            )
            return final_suppression[0] if final_suppression is not None else None

        supersede_decision: AutoSupersedeDecision | None = None
        supersede_matcher: Callable[[list[MemoryRecord]], AutoSupersedeDecision | None] | None = None
        candidate_has_key = bool(candidate.temporal_subject and candidate.temporal_predicate)
        if self.auto_supersede and vector and not candidate_has_key:

            def supersede_matcher(latest: list[MemoryRecord]) -> AutoSupersedeDecision | None:
                nonlocal supersede_decision
                # Re-select on the locked snapshot so a row closed by a
                # concurrent write can no longer be chosen.
                supersede_decision = _find_auto_supersede_target(
                    candidate,
                    latest,
                    vector,
                    embedding_space_id,
                )
                return supersede_decision

        created = await anyio.to_thread.run_sync(
            partial(
                self.store.create_memory,
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
                embedding_space_id=embedding_space_id if embedding_json else None,
                stability=candidate.stability,
                valid_from=candidate.valid_from,
                valid_until=candidate.valid_until,
                review_after=candidate.review_after,
                sensitivity=candidate.sensitivity,
                temporal_subject=candidate.temporal_subject,
                temporal_predicate=candidate.temporal_predicate,
                final_matcher=final_matcher,
                supersede_matcher=supersede_matcher,
                **classification_kwargs,
            )
        )
        if final_suppression is not None:
            matched, reason = final_suppression
            return ResolveResult(
                action="ignore",
                memory=matched,
                relation="same",
                reason=reason,
            )
        if (
            supersede_decision is not None
            and created.supersedes == supersede_decision.target.id
        ):
            return ResolveResult(
                action="update",
                memory=created,
                relation=supersede_decision.relation,
                reason=supersede_decision.reason,
                superseded_memory_id=supersede_decision.target.id,
            )
        if target:
            return ResolveResult(
                action="create",
                memory=created,
                relation=relation,
                reason=f"{related_reason}，暂不自动合并，建议体检确认",
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


def _can_suppress_new_candidate(
    candidate: CandidateMemory,
    memory: MemoryRecord,
) -> bool:
    """Only a live, user-asserted peer may suppress a newly asserted fact.

    Historical/resolved rows must remain available for timelines, but an old
    A value must not swallow a later A→B→A transition. Derived summaries also
    cannot override a user's direct assertion merely because their text matches.
    """
    if (
        memory.origin != "user_asserted"
        or memory.status not in {"dynamic", "pinned"}
        or not is_current_temporal_memory(memory)
        or memory.type != candidate.type
        or memory.sensitivity != candidate.sensitivity
    ):
        return False
    candidate_has_key = bool(
        candidate.temporal_subject and candidate.temporal_predicate
    )
    memory_has_key = bool(memory.temporal_subject and memory.temporal_predicate)
    if candidate_has_key != memory_has_key:
        return False
    if candidate_has_key:
        return (
            memory.temporal_subject == candidate.temporal_subject
            and memory.temporal_predicate == candidate.temporal_predicate
        )
    return True


def _find_suppressing_memory(
    *,
    candidate: CandidateMemory,
    existing: list[MemoryRecord],
    normalized_new: str,
    vector: list[float] | None,
    embedding_space_id: str,
) -> tuple[MemoryRecord, str] | None:
    """Repeat the suppressing checks while the final write lock is held."""
    for memory in existing:
        if not _can_suppress_new_candidate(candidate, memory):
            continue
        normalized_old = _normalize(memory.content)
        if normalized_old == normalized_new:
            return memory, "已有相同记忆"
        if normalized_new in normalized_old:
            return memory, "已有更完整的同主题记忆"
    covered_by = _find_semantically_covering_memory(
        candidate,
        existing,
        vector,
        embedding_space_id,
    )
    if covered_by is not None:
        return covered_by, "已有更完整的语义等价记忆"
    return None


def _find_semantically_covering_memory(
    candidate: CandidateMemory,
    existing: list[MemoryRecord],
    vector: list[float] | None,
    embedding_space_id: str,
) -> MemoryRecord | None:
    """Find an active old fact that conservatively entails a broader paraphrase."""
    if not vector or candidate.temporal_subject or candidate.temporal_predicate:
        return None

    best: MemoryRecord | None = None
    best_score = 0.0
    for memory in existing:
        if not _old_memory_covers_candidate(candidate, memory):
            continue
        old_vector = _memory_embedding_vector(
            memory,
            expected_space_id=embedding_space_id,
        )
        if old_vector is None:
            continue
        score = cosine_similarity(vector, old_vector)
        if score >= SEMANTIC_COVERAGE_SIMILARITY_THRESHOLD and score > best_score:
            best = memory
            best_score = score
    return best


def _old_memory_covers_candidate(
    candidate: CandidateMemory,
    memory: MemoryRecord,
) -> bool:
    if (
        memory.type != candidate.type
        or memory.origin != "user_asserted"
        or memory.status not in {"dynamic", "pinned"}
        or not is_current_temporal_memory(memory)
        or memory.sensitivity != candidate.sensitivity
        or memory.temporal_subject
        or memory.temporal_predicate
    ):
        return False

    new_content = candidate.memory
    old_content = memory.content
    if (
        pair_conflict(
            new_content,
            old_content,
            similarity_threshold=CONFLICT_CHAR_OVERLAP_THRESHOLD,
        )
        or _looks_superseding(new_content)
        or _has_intent_marker(new_content) != _has_intent_marker(old_content)
    ):
        return False

    # Embedding similarity plus shared labels is relatedness, not entailment.
    # Before suppressing a user's new assertion, require the old text to ground
    # the same proposition (actor, relation, object and polarity).  This keeps
    # "works at Acme" from swallowing "applied to Acme" and preserves distinct
    # facts about the same person, pet, product or place.
    if not has_text_grounding_anchor(new_content, old_content):
        return False

    if len(_semantic_text(old_content)) < len(_semantic_text(new_content)):
        return False
    if not _structured_tokens(new_content).issubset(_structured_tokens(old_content)):
        return False
    if not _candidate_entities_are_covered(candidate.entities, memory):
        return False
    return _labels_overlap(candidate.topics, memory.topics)


def _candidate_entities_are_covered(
    candidate_entities: list[str],
    memory: MemoryRecord,
) -> bool:
    normalized_candidates = [
        normalized
        for value in candidate_entities
        if (normalized := _semantic_text(value))
        and normalized not in _GENERIC_ENTITIES
    ]
    if not normalized_candidates:
        return False

    old_labels = [
        normalized
        for value in [memory.content, *memory.entities]
        if (normalized := _semantic_text(value))
    ]
    return all(
        any(
            candidate_entity == old_label
            or candidate_entity in old_label
            or old_label in candidate_entity
            for old_label in old_labels
        )
        for candidate_entity in normalized_candidates
    )


def _labels_overlap(left: list[str], right: list[str]) -> bool:
    left_labels = {_semantic_text(value) for value in left} - {""}
    right_labels = {_semantic_text(value) for value in right} - {""}
    return any(
        left_label == right_label
        or (
            min(len(left_label), len(right_label)) >= 2
            and (left_label in right_label or right_label in left_label)
        )
        for left_label in left_labels
        for right_label in right_labels
    )


def _semantic_text(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())


def _structured_tokens(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    return set(re.findall(r"[a-z]*\d+(?:[._-]\d+)*(?:[a-z]+)?", compact))


def _has_intent_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _INTENT_MARKERS)


def _candidate_eligible_for_auto_supersede(candidate: CandidateMemory) -> bool:
    """The new statement must be a committed, present-tense transition."""
    if candidate.temporal_subject or candidate.temporal_predicate:
        return False
    if candidate.type not in _AUTO_SUPERSEDE_TYPES:
        return False
    if not _looks_superseding(candidate.memory):
        return False
    if _has_intent_marker(candidate.memory):
        return False
    if _matches_any(candidate.memory, _UNCOMMITTED_INTENT_MARKERS):
        return False
    if candidate.source_quote and _matches_any(candidate.source_quote, _UNCOMMITTED_INTENT_MARKERS):
        return False
    valid_from = _parse_iso_datetime(candidate.valid_from)
    return valid_from is None or valid_from <= datetime.now(UTC)


def _auto_supersede_target_is_eligible(
    candidate: CandidateMemory,
    memory: MemoryRecord,
    *,
    now: datetime,
) -> bool:
    """Only a live, current, user-asserted present-state fact may be closed."""
    if memory.origin != "user_asserted":
        return False
    if (memory.status or "dynamic") != "dynamic":
        return False
    if memory.superseded_by or not is_current_temporal_memory(memory):
        return False
    if memory.temporal_subject or memory.temporal_predicate:
        return False
    if memory.type != candidate.type or memory.sensitivity != candidate.sensitivity:
        return False
    content = memory.content
    if (
        _matches_any(content, _PAST_MARKERS)
        or _has_intent_marker(content)
        or _matches_any(content, _UNCOMMITTED_INTENT_MARKERS)
    ):
        return False
    starts_at = _parse_iso_datetime(memory.valid_from)
    return starts_at is None or starts_at <= now


def _find_auto_supersede_target(
    candidate: CandidateMemory,
    existing: list[MemoryRecord],
    vector: list[float] | None,
    embedding_space_id: str,
) -> AutoSupersedeDecision | None:
    """Pick the live fact a transition statement replaces, or None.

    Unlike ``_find_related_memory`` this filters to eligible rows *before*
    scoring, so an A→B→A return never selects the historical A.
    """
    if not vector or not embedding_space_id:
        return None
    if not _candidate_eligible_for_auto_supersede(candidate):
        return None
    now = datetime.now(UTC)
    best: MemoryRecord | None = None
    best_score = 0.0
    for memory in existing:
        if not _auto_supersede_target_is_eligible(candidate, memory, now=now):
            continue
        old_vector = _memory_embedding_vector(memory, expected_space_id=embedding_space_id)
        if old_vector is None:
            continue
        score = cosine_similarity(vector, old_vector)
        if score > best_score:
            best, best_score = memory, score
    if best is None or best_score < EMBEDDING_SIMILARITY_THRESHOLD:
        return None
    relation = _related_content_relation(candidate.memory, best.content)
    if relation not in {"supersede", "conflict"}:
        return None
    if not _describes_same_replaceable_attribute(candidate, best, relation=relation):
        return None
    return AutoSupersedeDecision(
        target=best,
        relation=relation,
        reason=_AUTO_SUPERSEDE_REASONS[relation],
    )


def _describes_same_replaceable_attribute(
    candidate: CandidateMemory,
    memory: MemoryRecord,
    *,
    relation: MemoryRelation,
) -> bool:
    """Embedding similarity is relatedness, not replacement; demand attribute evidence.

    * the actor must be the same on both sides (never 用户 vs 用户的猫/朋友);
    * an exclusive relation family (residence, employment, usage, identity,
      possession …) shared by both statements is sufficient — the object may
      change completely (北京 → 上海) without any shared term;
    * additive families (preference, consumption) need a shared grounded term
      *and* an explicit negation conflict;
    * a structured value of the same kind with a different value (phone number,
      e-mail) is sufficient;
    * otherwise both a shared grounded term and overlapping topics are needed.
    """
    new_text, old_text = candidate.memory, memory.content
    if not grounding_subjects_compatible_both_ways(new_text, old_text):
        return False
    if structured_value_kind_changed(new_text, old_text):
        return True
    families = shared_relation_families(new_text, old_text)
    if families - _ADDITIVE_RELATION_FAMILIES:
        return True
    terms_overlap = grounding_terms_overlap(new_text, old_text)
    if families:
        return terms_overlap and relation == "conflict"
    return terms_overlap and _labels_overlap(candidate.topics, memory.topics)


def _find_related_memory(
    candidate: CandidateMemory,
    existing: list[MemoryRecord],
    vector: list[float] | None,
    embedding_space_id: str,
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
            old_vector = _memory_embedding_vector(
                memory,
                expected_space_id=embedding_space_id,
            )
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


def _related_content_relation(new_content: str, old_content: str) -> MemoryRelation:
    if pair_conflict(
        new_content,
        old_content,
        similarity_threshold=CONFLICT_CHAR_OVERLAP_THRESHOLD,
    ):
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


_TRANSITION_MARKERS_CJK = (
    "现在",
    "已经",
    "改成",
    "改为",
    "改用",
    "换成",
    "换为",
    "不再",
    "取代",
)
# English markers need word boundaries: a bare substring "now" would fire on
# "know" / "snow" and drive an automatic supersede.
_TRANSITION_MARKERS_EN_RE = re.compile(r"\b(?:instead|switched|now|no longer)\b", re.IGNORECASE)


def _looks_superseding(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in _TRANSITION_MARKERS_CJK):
        return True
    return _TRANSITION_MARKERS_EN_RE.search(lowered) is not None
