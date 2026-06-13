from datetime import UTC, datetime

from app.memory.models import (
    MemoryRecord,
    MemoryRelation,
    MemoryReviewRecommendation,
    MemoryReviewResult,
)
from app.memory.store import MemoryStore
from app.memory.utils import (
    _char_overlap,
    _has_negation,
    _normalize,
    _parse_iso_datetime,
    _term_jaccard,
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

    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if left.type != right.type:
                continue
            pair = tuple(sorted((left.id, right.id)))
            if pair in seen_pairs:
                continue
            recommendation = _pair_recommendation(left, right)
            if recommendation is None:
                continue
            seen_pairs.add(pair)
            recommendations.append(recommendation)

    return recommendations


def _pair_recommendation(
    left: MemoryRecord,
    right: MemoryRecord,
) -> MemoryReviewRecommendation | None:
    left_normalized = _normalize(left.content)
    right_normalized = _normalize(right.content)
    if not left_normalized or not right_normalized:
        return None

    if left_normalized == right_normalized:
        return MemoryReviewRecommendation(
            action="merge",
            relation="same",
            reason="存在重复记忆，建议保留一条并合并来源",
            memory_ids=[left.id, right.id],
            suggested_content=_newer(left, right).content,
        )

    if left_normalized in right_normalized:
        return MemoryReviewRecommendation(
            action="merge",
            relation="supplement",
            reason="后一条记忆包含前一条信息，建议合并为更完整版本",
            memory_ids=[left.id, right.id],
            suggested_content=right.content,
        )
    if right_normalized in left_normalized:
        return MemoryReviewRecommendation(
            action="merge",
            relation="supplement",
            reason="前一条记忆包含后一条信息，建议合并为更完整版本",
            memory_ids=[left.id, right.id],
            suggested_content=left.content,
        )

    similarity = max(_term_jaccard(left.content, right.content), _char_overlap(left.content, right.content))
    if similarity < 0.65:
        return None

    relation = _content_relation(left.content, right.content)
    return MemoryReviewRecommendation(
        action="review",
        relation=relation,
        reason=(
            "两条同类型记忆可能互相冲突，建议确认哪条仍然成立"
            if relation == "conflict"
            else "两条同类型记忆高度相似，建议确认是否由新记忆取代旧记忆"
        ),
        memory_ids=[left.id, right.id],
        suggested_content=_newer(left, right).content,
    )


def _content_relation(left: str, right: str) -> MemoryRelation:
    if _has_negation(left) != _has_negation(right):
        return "conflict"
    return "supersede"


def _newer(left: MemoryRecord, right: MemoryRecord) -> MemoryRecord:
    return right if right.updated_at >= left.updated_at else left
