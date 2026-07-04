from dataclasses import dataclass
from datetime import UTC, datetime

from app.memory.decay import life_score, score_memory
from app.memory.models import (
    CoreMemorySectionName,
    MemoryRecord,
    MemoryRelation,
    MemoryReviewAction,
    MemoryReviewNextAction,
    MemoryReviewRecommendation,
    MemoryReviewResult,
    MemoryReviewRiskTag,
    MemoryReviewSeverity,
)
from app.memory.store import MemoryStore
from app.memory.utils import (
    _has_negation,
    _normalize,
    _parse_iso_datetime,
    _terms,
)


_STALE_DAYS = 90.0
_LOW_LIFE_THRESHOLD = 30.0


class MemoryReviewer:
    """Analyze stored memories and return cleanup suggestions without mutating data."""

    def __init__(self, *, store: MemoryStore):
        self.store = store

    def review(self, *, user_id: str, limit: int = 200) -> MemoryReviewResult:
        memories = self.store.list_memories(user_id=user_id, limit=max(1, min(limit, 500)))
        core_map = _core_evidence_map(self.store, user_id=user_id)
        recommendations: list[MemoryReviewRecommendation] = []
        relationship_recommendations = _relationship_recommendations(
            memories,
            core_map=core_map,
        )
        relationship_memory_ids = {
            memory_id
            for recommendation in relationship_recommendations
            for memory_id in recommendation.memory_ids
        }

        recommendations.extend(_review_after_recommendations(memories, core_map=core_map))
        recommendations.extend(_validity_recommendations(memories, core_map=core_map))
        recommendations.extend(_time_uncertain_recommendations(memories, core_map=core_map))
        recommendations.extend(_sensitivity_recommendations(memories, core_map=core_map))
        recommendations.extend(_emotion_uncertain_recommendations(memories, core_map=core_map))
        recommendations.extend(
            _stale_recommendations(
                memories,
                core_map=core_map,
                excluded_memory_ids=relationship_memory_ids,
            )
        )
        recommendations.extend(
            _low_life_recommendations(
                memories,
                core_map=core_map,
                excluded_memory_ids=relationship_memory_ids,
            )
        )
        recommendations.extend(relationship_recommendations)
        recommendations.extend(_core_evidence_recommendations(memories, core_map=core_map))

        return MemoryReviewResult(total=len(memories), recommendations=recommendations)


@dataclass
class _PreparedMemory:
    record: MemoryRecord
    normalized: str
    terms: set[str]
    chars: set[str]
    has_negation: bool


def _review_after_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> list[MemoryReviewRecommendation]:
    now = datetime.now(UTC)
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        review_after = _parse_iso_datetime(memory.review_after)
        if review_after is None or review_after > now:
            continue
        risk_tags: list[MemoryReviewRiskTag] = []
        if _is_time_variable_fact(memory):
            risk_tags.append("time_uncertain")
        recommendations.append(
            _recommendation(
                action="review",
                reason="记忆已到复核时间，建议自然确认是否仍然成立",
                memory_ids=[memory.id],
                risk_tags=risk_tags,
                core_map=core_map,
            )
        )
    return recommendations


def _validity_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> list[MemoryReviewRecommendation]:
    now = datetime.now(UTC)
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        valid_until = _parse_iso_datetime(memory.valid_until)
        if valid_until is None or valid_until >= now:
            continue

        if memory.stability == "temporary":
            if memory.importance <= 3 and memory.usage_count == 0:
                recommendations.append(
                    _recommendation(
                        action="delete",
                        reason="低重要度临时记忆已过有效期且从未被召回，建议删除",
                        memory_ids=[memory.id],
                        risk_tags=["expired", "low_value"],
                        severity="medium",
                        core_map=core_map,
                    )
                )
                continue
            recommendations.append(
                _recommendation(
                    action="lower",
                    reason="临时记忆已过有效期，建议降权或复核后删除",
                    memory_ids=[memory.id],
                    risk_tags=["expired"],
                    severity="medium",
                    core_map=core_map,
                )
            )
        else:
            recommendations.append(
                _recommendation(
                    action="review",
                    reason="记忆已过有效期，建议复核是否仍然成立",
                    memory_ids=[memory.id],
                    risk_tags=["expired"],
                    severity="medium",
                    core_map=core_map,
                )
            )
    return recommendations


def _time_uncertain_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> list[MemoryReviewRecommendation]:
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        if memory.valid_until or memory.review_after:
            continue
        if not _is_time_variable_fact(memory):
            continue
        recommendations.append(
            _recommendation(
                action="review",
                reason="这条事实可能随时间变化，但缺少有效期或复核时间，建议补充时间锚点",
                memory_ids=[memory.id],
                risk_tags=["time_uncertain"],
                severity="medium",
                core_map=core_map,
            )
        )
    return recommendations


def _sensitivity_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> list[MemoryReviewRecommendation]:
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        if memory.sensitivity == "normal":
            continue
        recommendations.append(
            _recommendation(
                action="review",
                reason="隐私或敏感记忆需要更高保留门槛，且不会进入核心记忆",
                memory_ids=[memory.id],
                risk_tags=["sensitive"],
                severity="medium",
                core_map=core_map,
            )
        )
    return recommendations


def _emotion_uncertain_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> list[MemoryReviewRecommendation]:
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        if memory.arousal < 0.7 or memory.confidence > 0.55:
            continue
        recommendations.append(
            _recommendation(
                action="review",
                reason="这条记忆情绪唤起较强但置信度偏低，建议确认是否准确表达真实情况",
                memory_ids=[memory.id],
                risk_tags=["emotion_uncertain"],
                severity="medium",
                core_map=core_map,
            )
        )
    return recommendations


def _stale_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
    excluded_memory_ids: set[str],
) -> list[MemoryReviewRecommendation]:
    now = datetime.now(UTC)
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        if memory.id in excluded_memory_ids:
            continue
        if _has_due_or_expired_marker(memory, now=now):
            continue
        decay = score_memory(memory, now=now)
        if decay.days_since_last_active < _STALE_DAYS or memory.importance < 6:
            continue
        recommendations.append(
            _recommendation(
                action="review",
                reason="这条重要记忆长期未被召回，建议确认它是否仍值得保留在显眼位置",
                memory_ids=[memory.id],
                risk_tags=["stale"],
                severity="medium",
                core_map=core_map,
            )
        )
    return recommendations


def _low_life_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
    excluded_memory_ids: set[str],
) -> list[MemoryReviewRecommendation]:
    now = datetime.now(UTC)
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        if memory.id in excluded_memory_ids:
            continue
        if memory.importance > 3 or _has_due_or_expired_marker(memory, now=now):
            continue
        decay = score_memory(memory, now=now)
        if life_score(memory, now=now, decay=decay) > _LOW_LIFE_THRESHOLD:
            continue
        recommendations.append(
            _recommendation(
                action="lower",
                reason="这条低重要度记忆生命力偏低，建议降权或移入回收站",
                memory_ids=[memory.id],
                risk_tags=["low_value"],
                severity="low",
                core_map=core_map,
            )
        )
    return recommendations


def _relationship_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> list[MemoryReviewRecommendation]:
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
            recommendation = _prepared_pair_recommendation(left, right, core_map=core_map)
            if recommendation is None:
                continue
            seen_pairs.add(pair)
            recommendations.append(recommendation)

    return recommendations


def _core_evidence_recommendations(
    memories: list[MemoryRecord],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> list[MemoryReviewRecommendation]:
    now = datetime.now(UTC)
    recommendations: list[MemoryReviewRecommendation] = []
    for memory in memories:
        core_sections = core_map.get(memory.id, [])
        if not core_sections:
            continue
        valid_until = _parse_iso_datetime(memory.valid_until)
        is_expired = valid_until is not None and valid_until < now
        if memory.confidence >= 0.55 and not is_expired:
            continue
        risk_tags: list[MemoryReviewRiskTag] = ["core_evidence"]
        if is_expired:
            risk_tags.append("expired")
        recommendations.append(
            _recommendation(
                action="review",
                reason="这条记忆被核心记忆引用，但置信度较低或已经过期，建议先复核并手动整理核心记忆",
                memory_ids=[memory.id],
                risk_tags=risk_tags,
                severity="high",
                core_map=core_map,
            )
        )
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
    return _prepared_pair_recommendation(
        _prepare_memory(left),
        _prepare_memory(right),
        core_map={},
    )


def _prepared_pair_recommendation(
    left: _PreparedMemory,
    right: _PreparedMemory,
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> MemoryReviewRecommendation | None:
    left_normalized = left.normalized
    right_normalized = right.normalized
    if not left_normalized or not right_normalized:
        return None

    if left_normalized == right_normalized:
        return _recommendation(
            action="merge",
            relation="same",
            reason="存在重复记忆，建议保留一条并合并来源",
            memory_ids=[left.record.id, right.record.id],
            suggested_content=_newer(left.record, right.record).content,
            risk_tags=["duplicate"],
            severity="medium",
            core_map=core_map,
        )

    if left_normalized in right_normalized:
        return _recommendation(
            action="merge",
            relation="supplement",
            reason="后一条记忆包含前一条信息，建议合并为更完整版本",
            memory_ids=[left.record.id, right.record.id],
            suggested_content=right.record.content,
            risk_tags=["duplicate"],
            severity="medium",
            core_map=core_map,
        )
    if right_normalized in left_normalized:
        return _recommendation(
            action="merge",
            relation="supplement",
            reason="前一条记忆包含后一条信息，建议合并为更完整版本",
            memory_ids=[left.record.id, right.record.id],
            suggested_content=left.record.content,
            risk_tags=["duplicate"],
            severity="medium",
            core_map=core_map,
        )

    similarity = max(_set_jaccard(left.terms, right.terms), _set_jaccard(left.chars, right.chars))
    if similarity < 0.65:
        return None

    relation = _prepared_content_relation(left, right)
    return _recommendation(
        action="review",
        relation=relation,
        reason=(
            "两条同类型记忆可能互相冲突，建议确认哪条仍然成立"
            if relation == "conflict"
            else "两条同类型记忆高度相似，建议确认是否由新记忆取代旧记忆"
        ),
        memory_ids=[left.record.id, right.record.id],
        suggested_content=_newer(left.record, right.record).content,
        risk_tags=["conflict"] if relation == "conflict" else ["duplicate"],
        severity="high" if relation == "conflict" else "medium",
        core_map=core_map,
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


def _core_evidence_map(
    store: MemoryStore,
    *,
    user_id: str,
) -> dict[str, list[CoreMemorySectionName]]:
    result: dict[str, list[CoreMemorySectionName]] = {}
    for section in store.list_core_memory_sections(user_id=user_id):
        for memory_id in section.evidence_memory_ids:
            result.setdefault(memory_id, []).append(section.section)
    return result


def _recommendation(
    *,
    action: MemoryReviewAction,
    reason: str,
    memory_ids: list[str],
    risk_tags: list[MemoryReviewRiskTag],
    core_map: dict[str, list[CoreMemorySectionName]],
    relation: MemoryRelation = "none",
    suggested_content: str | None = None,
    severity: MemoryReviewSeverity | None = None,
) -> MemoryReviewRecommendation:
    core_sections = _core_sections_for_ids(memory_ids, core_map=core_map)
    merged_risk_tags = _unique_risk_tags(
        [*risk_tags, *(["core_evidence"] if core_sections else [])]
    )
    final_severity = _max_severity(severity, _severity_for_tags(merged_risk_tags))
    return MemoryReviewRecommendation(
        action=action,
        reason=reason,
        memory_ids=memory_ids,
        relation=relation,
        suggested_content=suggested_content,
        risk_tags=merged_risk_tags,
        severity=final_severity,
        next_action_options=_next_action_options(action, merged_risk_tags),
        core_memory_sections=core_sections,
    )


def _core_sections_for_ids(
    memory_ids: list[str],
    *,
    core_map: dict[str, list[CoreMemorySectionName]],
) -> list[CoreMemorySectionName]:
    sections: list[CoreMemorySectionName] = []
    for memory_id in memory_ids:
        for section in core_map.get(memory_id, []):
            if section not in sections:
                sections.append(section)
    return sections


def _unique_risk_tags(tags: list[MemoryReviewRiskTag]) -> list[MemoryReviewRiskTag]:
    result: list[MemoryReviewRiskTag] = []
    for tag in tags:
        if tag not in result:
            result.append(tag)
    return result


def _severity_for_tags(tags: list[MemoryReviewRiskTag]) -> MemoryReviewSeverity:
    if "conflict" in tags or "core_evidence" in tags:
        return "high"
    if (
        "expired" in tags
        or "time_uncertain" in tags
        or "sensitive" in tags
        or "duplicate" in tags
        or "stale" in tags
        or "emotion_uncertain" in tags
    ):
        return "medium"
    return "low"


def _max_severity(
    left: MemoryReviewSeverity | None,
    right: MemoryReviewSeverity,
) -> MemoryReviewSeverity:
    if left is None:
        return right
    ranks: dict[MemoryReviewSeverity, int] = {"low": 1, "medium": 2, "high": 3}
    return left if ranks[left] >= ranks[right] else right


def _next_action_options(
    action: MemoryReviewAction,
    risk_tags: list[MemoryReviewRiskTag],
) -> list[MemoryReviewNextAction]:
    options: list[MemoryReviewNextAction] = []
    if action in {"review", "keep"}:
        options.extend(["ai_modify", "confirm_valid", "snooze"])
    if action == "merge" or "duplicate" in risk_tags:
        options.extend(["merge", "ai_modify"])
    if action == "delete":
        options.extend(["move_to_trash", "ai_modify"])
    if action == "lower" or "low_value" in risk_tags:
        options.extend(["lower_importance", "confirm_valid", "move_to_trash"])
    if "core_evidence" in risk_tags:
        options.append("review_core_memory")

    deduped: list[MemoryReviewNextAction] = []
    for option in options:
        if option not in deduped:
            deduped.append(option)
    return deduped


def _is_time_variable_fact(memory: MemoryRecord) -> bool:
    if memory.type != "semantic":
        return False
    content = memory.content.lower()
    if "截至 " in memory.content or "as of" in content:
        return False
    markers = (
        "现在",
        "目前",
        "最近",
        "近期",
        "正在",
        "计划",
        "准备",
        "打算",
        "临时",
        "当前",
        "岁",
        "年龄",
        "now",
        "currently",
        "recently",
        "planning",
    )
    return any(marker in content for marker in markers)


def _has_due_or_expired_marker(memory: MemoryRecord, *, now: datetime) -> bool:
    review_after = _parse_iso_datetime(memory.review_after)
    if review_after is not None and review_after <= now:
        return True
    valid_until = _parse_iso_datetime(memory.valid_until)
    return valid_until is not None and valid_until < now
