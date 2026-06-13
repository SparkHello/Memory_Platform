from dataclasses import dataclass
from datetime import UTC, datetime

from app.memory.models import (
    MemoryRecord,
    MemoryRelation,
    MemoryReviewRecommendation,
    MemoryReviewResult,
)
from app.memory.store import MemoryStore
from app.memory.utils import (
    _has_negation,
    _normalize,
    _parse_iso_datetime,
    _terms,
)


class MemoryReviewer:
    """Analyze stored memories and return cleanup suggestions without mutating data."""

    def __init__(self, *, store: MemoryStore):
        self.store = store

    def review(self, *, user_id: str, limit: int = 200) -> MemoryReviewResult:
        memories = self.store.list_memories(user_id=user_id, limit=max(1, min(limit, 500)))
        recommendations: list[MemoryReviewRecommendation] = []

        recommendations.extend(_review_after_recommendations(memories))
        recommendations.extend(_validity_recommendations(memories))
        recommendations.extend(_sensitivity_recommendations(memories))
        recommendations.extend(_relationship_recommendations(memories))

        return MemoryReviewResult(total=len(memories), recommendations=recommendations)


@dataclass
class _PreparedMemory:
    record: MemoryRecord
    normalized: str
    terms: set[str]
    chars: set[str]
    has_negation: bool


def _review_after_recommendations(memories: list[MemoryRecord]) -> list[MemoryReviewRecommendation]:
    now = datetime.now(UTC)
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        review_after = _parse_iso_datetime(memory.review_after)
        if review_after is None or review_after > now:
            continue
        recommendations.append(
            MemoryReviewRecommendation(
                action="review",
                reason="记忆已到复核时间，建议自然确认是否仍然成立",
                memory_ids=[memory.id],
            )
        )
    return recommendations


def _validity_recommendations(memories: list[MemoryRecord]) -> list[MemoryReviewRecommendation]:
    now = datetime.now(UTC)
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        valid_until = _parse_iso_datetime(memory.valid_until)
        if valid_until is None or valid_until >= now:
            continue

        if memory.stability == "temporary":
            if memory.importance <= 3 and memory.usage_count == 0:
                recommendations.append(
                    MemoryReviewRecommendation(
                        action="delete",
                        reason="低重要度临时记忆已过有效期且从未被召回，建议删除",
                        memory_ids=[memory.id],
                    )
                )
                continue
            recommendations.append(
                MemoryReviewRecommendation(
                    action="lower",
                    reason="临时记忆已过有效期，建议降权或复核后删除",
                    memory_ids=[memory.id],
                )
            )
        else:
            recommendations.append(
                MemoryReviewRecommendation(
                    action="review",
                    reason="记忆已过有效期，建议复核是否仍然成立",
                    memory_ids=[memory.id],
                )
            )
    return recommendations


def _sensitivity_recommendations(memories: list[MemoryRecord]) -> list[MemoryReviewRecommendation]:
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        if memory.sensitivity == "normal":
            continue
        recommendations.append(
            MemoryReviewRecommendation(
                action="review",
                reason="隐私或敏感记忆需要更高保留门槛，且不会进入核心记忆",
                memory_ids=[memory.id],
            )
        )
    return recommendations


def _relationship_recommendations(memories: list[MemoryRecord]) -> list[MemoryReviewRecommendation]:
    recommendations: list[MemoryReviewRecommendation] = []
    seen_pairs: set[tuple[str, str]] = set()
    ordered = sorted(memories, key=lambda memory: memory.updated_at)
    prepared = [_prepare_memory(memory) for memory in ordered]
    grouped: dict[str, list[_PreparedMemory]] = {}
    group_positions: dict[str, int] = {}

    for item in prepared:
        group = grouped.setdefault(item.record.type, [])
        group_positions[item.record.id] = len(group)
        group.append(item)

    for left in prepared:
        group = grouped[left.record.type]
        left_position = group_positions[left.record.id]
        for group_index in range(left_position + 1, len(group)):
            right = group[group_index]
            pair = tuple(sorted((left.record.id, right.record.id)))
            if pair in seen_pairs:
                continue
            recommendation = _prepared_pair_recommendation(left, right)
            if recommendation is None:
                continue
            seen_pairs.add(pair)
            recommendations.append(recommendation)

    return recommendations


def _prepare_memory(memory: MemoryRecord) -> _PreparedMemory:
    return _PreparedMemory(
        record=memory,
        normalized=_normalize(memory.content),
        terms=_terms(memory.content),
        chars={char.lower() for char in memory.content if not char.isspace()},
        has_negation=_has_negation(memory.content),
    )


def _set_jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _pair_recommendation(
    left: MemoryRecord,
    right: MemoryRecord,
) -> MemoryReviewRecommendation | None:
    return _prepared_pair_recommendation(_prepare_memory(left), _prepare_memory(right))


def _prepared_pair_recommendation(
    left: _PreparedMemory,
    right: _PreparedMemory,
) -> MemoryReviewRecommendation | None:
    left_normalized = left.normalized
    right_normalized = right.normalized
    if not left_normalized or not right_normalized:
        return None

    if left_normalized == right_normalized:
        return MemoryReviewRecommendation(
            action="merge",
            relation="same",
            reason="存在重复记忆，建议保留一条并合并来源",
            memory_ids=[left.record.id, right.record.id],
            suggested_content=_newer(left.record, right.record).content,
        )

    if left_normalized in right_normalized:
        return MemoryReviewRecommendation(
            action="merge",
            relation="supplement",
            reason="后一条记忆包含前一条信息，建议合并为更完整版本",
            memory_ids=[left.record.id, right.record.id],
            suggested_content=right.record.content,
        )
    if right_normalized in left_normalized:
        return MemoryReviewRecommendation(
            action="merge",
            relation="supplement",
            reason="前一条记忆包含后一条信息，建议合并为更完整版本",
            memory_ids=[left.record.id, right.record.id],
            suggested_content=left.record.content,
        )

    similarity = max(_set_jaccard(left.terms, right.terms), _set_jaccard(left.chars, right.chars))
    if similarity < 0.65:
        return None

    relation = _prepared_content_relation(left, right)
    return MemoryReviewRecommendation(
        action="review",
        relation=relation,
        reason=(
            "两条同类型记忆可能互相冲突，建议确认哪条仍然成立"
            if relation == "conflict"
            else "两条同类型记忆高度相似，建议确认是否由新记忆取代旧记忆"
        ),
        memory_ids=[left.record.id, right.record.id],
        suggested_content=_newer(left.record, right.record).content,
    )


def _prepared_content_relation(left: _PreparedMemory, right: _PreparedMemory) -> MemoryRelation:
    if left.has_negation != right.has_negation:
        return "conflict"
    return "supersede"


def _content_relation(left: str, right: str) -> MemoryRelation:
    if _has_negation(left) != _has_negation(right):
        return "conflict"
    return "supersede"


def _newer(left: MemoryRecord, right: MemoryRecord) -> MemoryRecord:
    return right if right.updated_at >= left.updated_at else left
