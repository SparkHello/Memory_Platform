from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


MemoryType = Literal[
    "project",
    "preference",
    "fact",
    "learning",
    "style",
    "person",
    "relationship",
]

MemoryAction = Literal["create", "update", "ignore"]

MemoryStability = Literal["temporary", "medium", "stable"]

MemorySensitivity = Literal["normal", "private", "sensitive"]

MemoryRelation = Literal["none", "same", "supplement", "conflict", "supersede"]

MemoryReviewAction = Literal["keep", "merge", "lower", "delete", "review"]

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
    type: MemoryType = "fact"
    importance: int = Field(default=1, ge=1, le=10)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    source_message: str | None = None
    source_conversation_id: str | None = None
    embedding_json: str | None = None
    last_used_at: str | None = None
    usage_count: int = Field(default=0, ge=0)
    stability: MemoryStability = "stable"
    valid_until: str | None = None
    review_after: str | None = None
    sensitivity: MemorySensitivity = "normal"
    evidence_memory_ids: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    archived_at: str | None = None
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


class RecentContextSummary(BaseModel):
    id: str
    user_id: str
    conversation_id: str | None = None
    summary: str
    created_at: str
    updated_at: str
    archived: int = 0


class CandidateMemory(BaseModel):
    """记忆提取模型输出的候选记忆，字段与提取 JSON 一一对应。"""

    action: MemoryAction
    memory: str = ""
    type: MemoryType = "fact"
    importance: int = Field(default=1, ge=0, le=10)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    stability: MemoryStability = "stable"
    valid_until: str | None = None
    review_after: str | None = None
    sensitivity: MemorySensitivity = "normal"
    reason: str = ""
    source_quote: str = ""


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
    decision: MemoryAction
    reason: str
    created_at: str


class MemoryReviewRecommendation(BaseModel):
    action: MemoryReviewAction
    reason: str
    memory_ids: list[str] = Field(default_factory=list)
    relation: MemoryRelation = "none"
    suggested_content: str | None = None


class MemoryReviewResult(BaseModel):
    total: int
    recommendations: list[MemoryReviewRecommendation] = Field(default_factory=list)


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
