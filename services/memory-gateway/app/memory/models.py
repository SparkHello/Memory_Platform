from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


MemoryType = Literal[
    "episodic",
    "semantic",
    "procedural",
    "emotional",
    "reflective",
]

MEMORY_TYPES: set[str] = {
    "episodic",
    "semantic",
    "procedural",
    "emotional",
    "reflective",
}

LEGACY_MEMORY_TYPES: set[str] = {
    "project",
    "preference",
    "fact",
    "learning",
    "style",
    "person",
    "relationship",
}


def normalize_memory_type(value: object) -> object:
    """Map pre-sector business types to the conservative semantic sector."""
    if not isinstance(value, str):
        return value
    normalized = value.strip().lower()
    if normalized in LEGACY_MEMORY_TYPES:
        return "semantic"
    return normalized


def normalize_optional_text(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    normalized = " ".join(value.strip().split())
    return normalized or None


def normalize_iso_text(value: object) -> object:
    normalized = normalize_optional_text(value)
    if normalized is None or not isinstance(normalized, str):
        return normalized
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("must be an ISO date or datetime") from exc
    return normalized


def _validate_temporal_metadata(
    *,
    temporal_subject: str | None,
    temporal_predicate: str | None,
    valid_from: str | None,
    valid_until: str | None,
) -> None:
    has_subject = bool(temporal_subject)
    has_predicate = bool(temporal_predicate)
    if has_subject != has_predicate:
        raise ValueError("temporal_subject and temporal_predicate must be provided together")
    if not valid_from or not valid_until:
        return

    starts_at = datetime.fromisoformat(valid_from)
    ends_at = datetime.fromisoformat(valid_until)
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    else:
        starts_at = starts_at.astimezone(UTC)
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=UTC)
    else:
        ends_at = ends_at.astimezone(UTC)
    if starts_at > ends_at:
        raise ValueError("valid_from must not be later than valid_until")

MemoryAction = Literal["create", "update", "ignore"]

MemoryIngestStatus = Literal["completed", "no_memory", "rejected", "retryable_error"]

DecisionLogAction = Literal["create", "update", "ignore", "purge"]

MemoryStability = Literal["temporary", "medium", "stable"]

MemorySensitivity = Literal["normal", "private", "sensitive"]

MemoryOrigin = Literal["user_asserted", "agent_derived"]

MemoryRelation = Literal["none", "same", "supplement", "conflict", "supersede"]

MemoryReviewAction = Literal["keep", "merge", "lower", "delete", "review"]

MemoryReviewRiskTag = Literal[
    "duplicate",
    "conflict",
    "expired",
    "time_uncertain",
    "sensitive",
    "low_value",
    "core_evidence",
    "stale",
    "emotion_uncertain",
]

MemorySurfaceMode = Literal[
    "balanced",
    "important",
    "emotional",
    "stale",
    "review_due",
]

MemorySurfaceSignal = Literal[
    "expired",
    "review_due",
    "near_expiry",
    "sensitive",
    "stale",
    "emotion_uncertain",
    "low_life",
]

MemoryStatus = Literal["dynamic", "resolved", "archived", "pinned"]

DatabaseHealthStatus = Literal["ok", "warning", "error"]

DatabaseHealthSeverity = Literal["info", "warning", "error"]

DatabaseHealthIssueType = Literal[
    "orphan_core_evidence",
    "archived_core_evidence",
    "orphan_space_link_memory",
    "orphan_space_link_space",
    "embedding_missing",
    "embedding_invalid",
    "embedding_dimension_mismatch",
    "export_consistency_error",
    "export_space_reference_missing",
    "stale_search_cache_reference",
    "orphan_core_history_evidence",
    "orphan_decision_log_reference",
    "invalid_decision_log_json",
]

MemoryReviewSeverity = Literal["low", "medium", "high"]

MemoryReviewNextAction = Literal[
    "ai_modify",
    "confirm_valid",
    "move_to_trash",
    "lower_importance",
    "merge",
    "review_core_memory",
    "snooze",
]

MemoryReviewRevisionOperationKind = Literal["update", "merge", "archive", "no_change"]

MemoryReviewPolicyCode = Literal[
    "temporary",
    "sensitive",
    "time_variable",
    "stage",
    "stable",
]

CoreMemorySectionName = Literal[
    "profile",
    "preferences",
    "relationships",
    "routines",
    "goals",
    "communication",
]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_memory_id() -> str:
    return str(uuid4())


class MemoryRecord(BaseModel):
    id: str
    user_id: str
    content: str
    type: MemoryType = "semantic"
    importance: int = Field(default=1, ge=1, le=10)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    valence: float = Field(default=0.5, ge=0.0, le=1.0)
    arousal: float = Field(default=0.3, ge=0.0, le=1.0)
    source_message: str | None = None
    source_conversation_id: str | None = None
    origin: MemoryOrigin = "user_asserted"
    embedding_json: str | None = None
    embedding_space_id: str | None = Field(default=None, max_length=300)
    last_used_at: str | None = None
    usage_count: float = Field(default=0.0, ge=0.0)
    stability: MemoryStability = "stable"
    valid_from: str | None = None
    valid_until: str | None = None
    review_after: str | None = None
    sensitivity: MemorySensitivity = "normal"
    evidence_memory_ids: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    space_ids: list[str] = Field(default_factory=list)
    temporal_subject: str | None = None
    temporal_predicate: str | None = None
    status: MemoryStatus = "dynamic"
    digested: bool = False
    decay_lambda: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        allow_inf_nan=False,
    )
    supersedes: str | None = None
    superseded_by: str | None = None
    created_at: str
    updated_at: str
    archived_at: str | None = None
    archived: int = 0
    revision: int = Field(default=1, ge=1, le=9_223_372_036_854_775_806)

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: object) -> object:
        return normalize_memory_type(value)

    @field_validator("embedding_space_id", mode="before")
    @classmethod
    def _normalize_embedding_space_id(cls, value: object) -> object:
        return normalize_optional_text(value)

    @field_validator("temporal_subject", "temporal_predicate", mode="before")
    @classmethod
    def _normalize_temporal_key(cls, value: object) -> object:
        return normalize_optional_text(value)

    @field_validator("valid_from", "valid_until", "review_after", mode="before")
    @classmethod
    def _normalize_datetime_fields(cls, value: object) -> object:
        return normalize_iso_text(value)

    @model_validator(mode="after")
    def _validate_temporal_metadata(self) -> "MemoryRecord":
        if self.embedding_json is None and self.embedding_space_id is not None:
            raise ValueError("embedding_space_id requires embedding_json")
        _validate_temporal_metadata(
            temporal_subject=self.temporal_subject,
            temporal_predicate=self.temporal_predicate,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
        )
        return self


class MemorySpace(BaseModel):
    id: str
    user_id: str
    name: str
    normalized_name: str
    created_at: str
    updated_at: str
    archived: int = 0


class CoreMemorySection(BaseModel):
    id: str
    user_id: str
    section: CoreMemorySectionName
    content: str
    evidence_memory_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    version: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str
    archived: int = 0
    revision: int = Field(default=1, ge=1, le=9_223_372_036_854_775_806)


class CoreMemorySectionHistory(BaseModel):
    id: str
    core_memory_section_id: str
    user_id: str
    section: CoreMemorySectionName
    content: str
    evidence_memory_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    version: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str
    replaced_at: str
    revision: int = Field(default=1, ge=1, le=9_223_372_036_854_775_806)


class RecentContextTurn(BaseModel):
    user: str = Field(max_length=65_536)
    assistant: str = Field(max_length=65_536)
    sensitivity: MemorySensitivity = "normal"

    @model_validator(mode="after")
    def validate_turn_bytes(self):
        if len(self.user.encode("utf-8")) + len(self.assistant.encode("utf-8")) > 64 * 1024:
            raise ValueError("recent context turn exceeds 64 KiB")
        return self


class RecentContextSummary(BaseModel):
    id: str
    user_id: str
    conversation_id: str | None = None
    summary: str
    compressed_summary: str = ""
    recent_turns: list[RecentContextTurn] = Field(default_factory=list)
    turn_count: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str
    archived: int = 0

    @model_validator(mode="after")
    def validate_context_bytes(self):
        turns_bytes = sum(
            len(turn.user.encode("utf-8")) + len(turn.assistant.encode("utf-8"))
            for turn in self.recent_turns
        )
        compressed_bytes = len(self.compressed_summary.encode("utf-8"))
        if turns_bytes + compressed_bytes > 512 * 1024:
            raise ValueError("recent context state exceeds 512 KiB")
        if len(self.summary.encode("utf-8")) > 512 * 1024:
            raise ValueError("recent context summary exceeds 512 KiB")
        return self


class ConversationBranchNode(RecentContextSummary):
    """A branch-local rolling-context snapshot for one completed chat history."""

    history_fingerprint: str
    parent_history_fingerprint: str = ""
    turn_fingerprint: str
    assistant_digest: str


class CandidateMemory(BaseModel):
    """记忆提取模型输出的候选记忆，字段与提取 JSON 一一对应。"""

    action: MemoryAction
    memory: str = ""
    type: MemoryType = "semantic"
    importance: int = Field(default=1, ge=0, le=10)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    valence: float = Field(default=0.5, ge=0.0, le=1.0)
    arousal: float = Field(default=0.3, ge=0.0, le=1.0)
    stability: MemoryStability = "stable"
    valid_from: str | None = None
    valid_until: str | None = None
    review_after: str | None = None
    sensitivity: MemorySensitivity = "normal"
    temporal_subject: str | None = None
    temporal_predicate: str | None = None
    topics: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    reason: str = ""
    source_quote: str = ""
    context_quote: str = ""

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: object) -> object:
        return normalize_memory_type(value)

    @field_validator("entities")
    @classmethod
    def _drop_entity_fragments(cls, value: list[str]) -> list[str]:
        """去掉空白/重复实体，以及从复合名称拆出的碎片（如 Dark Mode → Dark）。"""
        cleaned: list[str] = []
        seen: set[str] = set()
        for entity in value:
            normalized = entity.strip()
            if not normalized or normalized.casefold() in seen:
                continue
            seen.add(normalized.casefold())
            cleaned.append(normalized)
        folded = [entity.casefold() for entity in cleaned]
        return [
            entity
            for index, entity in enumerate(cleaned)
            if not any(
                index != other and folded[index] in folded[other]
                for other in range(len(cleaned))
            )
        ]

    @field_validator("temporal_subject", "temporal_predicate", mode="before")
    @classmethod
    def _normalize_temporal_key(cls, value: object) -> object:
        return normalize_optional_text(value)

    @field_validator("valid_from", "valid_until", "review_after", mode="before")
    @classmethod
    def _normalize_datetime_fields(cls, value: object) -> object:
        return normalize_iso_text(value)

    @model_validator(mode="after")
    def _validate_temporal_metadata(self) -> "CandidateMemory":
        _validate_temporal_metadata(
            temporal_subject=self.temporal_subject,
            temporal_predicate=self.temporal_predicate,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
        )
        return self


class ResolveResult(BaseModel):
    action: MemoryAction
    memory: MemoryRecord | None = None
    relation: MemoryRelation = "none"
    reason: str


class DecisionLog(BaseModel):
    id: str
    user_id: str = "default"
    conversation_id: str | None = None
    candidate_json: str
    decision: DecisionLogAction
    reason: str
    created_at: str


class MemoryReviewRecommendation(BaseModel):
    action: MemoryReviewAction
    reason: str
    memory_ids: list[str] = Field(default_factory=list)
    relation: MemoryRelation = "none"
    suggested_content: str | None = None
    risk_tags: list[MemoryReviewRiskTag] = Field(default_factory=list)
    severity: MemoryReviewSeverity = "low"
    next_action_options: list[MemoryReviewNextAction] = Field(default_factory=list)
    core_memory_sections: list[CoreMemorySectionName] = Field(default_factory=list)


class MemoryReviewResult(BaseModel):
    total: int
    recommendations: list[MemoryReviewRecommendation] = Field(default_factory=list)


class DatabaseHealthIssue(BaseModel):
    type: DatabaseHealthIssueType
    severity: DatabaseHealthSeverity
    object_id: str
    related_id: str | None = None
    message: str
    recommended_action: str


class DatabaseHealthSummary(BaseModel):
    errors: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    info: int = Field(default=0, ge=0)


class DatabaseHealthResult(BaseModel):
    status: DatabaseHealthStatus
    checked_at: str
    summary: DatabaseHealthSummary
    issues: list[DatabaseHealthIssue] = Field(default_factory=list)


class MemoryReviewPolicy(BaseModel):
    code: MemoryReviewPolicyCode
    interval_days: int = Field(ge=1)
    review_after: str
    reason: str


class MemoryReviewRevisionOperation(BaseModel):
    operation: MemoryReviewRevisionOperationKind
    reason: str = ""
    memory_ids: list[str] = Field(default_factory=list)
    target_memory_id: str | None = None
    content: str | None = None
    type: MemoryType | None = None
    importance: int | None = Field(default=None, ge=1, le=10)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    valence: float | None = Field(default=None, ge=0.0, le=1.0)
    arousal: float | None = Field(default=None, ge=0.0, le=1.0)
    stability: MemoryStability | None = None
    valid_until: str | None = None
    sensitivity: MemorySensitivity | None = None
    review_policy: MemoryReviewPolicy | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: object) -> object:
        if value is None:
            return None
        return normalize_memory_type(value)


class MemoryReviewRevisionPreview(BaseModel):
    operations: list[MemoryReviewRevisionOperation] = Field(default_factory=list)
    preview_token: str
    reason: str = ""


class MemorySourceExplanation(BaseModel):
    memory_id: str
    content: str
    source_excerpt: str | None = None
    source_conversation_id: str | None = None
    saved_at: str
    updated_at: str
    confidence: float
    is_core_memory_evidence: bool = False
    core_memory_sections: list[CoreMemorySectionName] = Field(default_factory=list)
    evidence_memory_ids: list[str] = Field(default_factory=list)


class MemoryMergeResult(BaseModel):
    action: MemoryAction
    memory: MemoryRecord | None = None
    merged_memory_ids: list[str] = Field(default_factory=list)
    archived_memory_ids: list[str] = Field(default_factory=list)
    reason: str


class MemoryIngestItemResult(BaseModel):
    action: MemoryAction
    relation: MemoryRelation = "none"
    reason: str
    memory_id: str | None = None
    content: str | None = None


class MemoryIngestResult(BaseModel):
    created: int = 0
    updated: int = 0
    ignored: int = 0
    items: list[MemoryIngestItemResult] = Field(default_factory=list)
    reason: str
    status: MemoryIngestStatus = "completed"
    retryable: bool = False
