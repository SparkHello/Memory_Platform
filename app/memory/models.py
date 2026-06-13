from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


MemoryType = Literal["project", "preference", "fact", "learning", "style"]

MemoryAction = Literal["create", "update", "ignore"]


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
    reason: str = ""
    source_quote: str = ""


class ResolveResult(BaseModel):
    action: MemoryAction
    memory: MemoryRecord | None = None
    reason: str


class DecisionLog(BaseModel):
    id: str
    conversation_id: str | None = None
    candidate_json: str
    decision: MemoryAction
    reason: str
    created_at: str
