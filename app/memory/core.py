from datetime import UTC, datetime

from pydantic import BaseModel, Field, ValidationError

from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import render_core_memory_consolidation_messages
from app.memory.models import CoreMemorySection, CoreMemorySectionName, MemoryRecord
from app.memory.store import MemoryStore
from app.memory.utils import _parse_iso_datetime, _parse_json_object
from app.openai_compat.schemas import ChatCompletionRequest


MIN_CORE_CONFIDENCE = 0.75
MAX_SOURCE_MEMORIES = 80


class CoreMemorySectionCandidate(BaseModel):
    section: CoreMemorySectionName
    content: str = ""
    evidence_memory_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class CoreMemoryConsolidationOutput(BaseModel):
    sections: list[CoreMemorySectionCandidate] = Field(default_factory=list)
    reason: str = ""


class CoreMemoryConsolidationResult(BaseModel):
    created: int = 0
    updated: int = 0
    ignored: int = 0
    sections: list[CoreMemorySection] = Field(default_factory=list)
    reason: str


class CoreMemoryConsolidator:
    """整理核心记忆：只从已经落库的长期记忆中提炼，不直接相信聊天上下文。"""

    def __init__(self, *, store: MemoryStore, llm_client: OpenAICompatibleClient):
        self.store = store
        self.llm_client = llm_client

    async def consolidate(self, *, user_id: str) -> CoreMemoryConsolidationResult:
        memories = _select_source_memories(self.store.list_memories(user_id=user_id, limit=200))
        if not memories:
            return CoreMemoryConsolidationResult(reason="没有足够的长期记忆可整理")

        current_sections = self.store.list_core_memory_sections(user_id=user_id)
        try:
            raw_output = await self._call_llm(
                memories=memories,
                current_sections=current_sections,
            )
        except Exception as exc:
            return CoreMemoryConsolidationResult(reason=f"调用核心记忆整理模型失败：{exc}")

        data = _parse_json_object(raw_output)
        if data is None:
            return CoreMemoryConsolidationResult(reason="核心记忆整理模型输出的不是合法 JSON")

        try:
            output = CoreMemoryConsolidationOutput.model_validate(data)
        except ValidationError as exc:
            first_error = exc.errors()[0]
            field = ".".join(str(part) for part in first_error.get("loc", ()))
            return CoreMemoryConsolidationResult(reason=f"整理输出不符合 schema（字段 {field}）")

        valid_memory_ids = {memory.id for memory in memories}
        created = updated = ignored = 0
        touched_sections: list[CoreMemorySection] = []

        for candidate in output.sections:
            rejection = _candidate_rejection(candidate, valid_memory_ids)
            if rejection:
                ignored += 1
                continue

            evidence_memory_ids = [
                memory_id for memory_id in candidate.evidence_memory_ids if memory_id in valid_memory_ids
            ]
            action, section = self.store.upsert_core_memory_section(
                user_id=user_id,
                section=candidate.section,
                content=candidate.content.strip(),
                evidence_memory_ids=evidence_memory_ids,
                confidence=candidate.confidence,
            )
            if action == "create":
                created += 1
            elif action == "update":
                updated += 1
            else:
                ignored += 1
            touched_sections.append(section)

        reason = output.reason or "核心记忆整理完成"
        if created == 0 and updated == 0 and ignored == 0:
            reason = "整理模型没有输出可保存的核心记忆"

        return CoreMemoryConsolidationResult(
            created=created,
            updated=updated,
            ignored=ignored,
            sections=touched_sections,
            reason=reason,
        )

    async def _call_llm(
        self,
        *,
        memories: list[MemoryRecord],
        current_sections: list[CoreMemorySection],
    ) -> str:
        messages = render_core_memory_consolidation_messages(
            memories=memories,
            current_sections=current_sections,
        )
        request = ChatCompletionRequest(
            model="core-memory-consolidator",
            messages=messages,
            temperature=0.0,
            stream=False,
        )
        response = await self.llm_client.create_chat_completion(request=request, messages=messages)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else ""


def _select_source_memories(memories: list[MemoryRecord]) -> list[MemoryRecord]:
    memories = [
        memory
        for memory in memories
        if memory.sensitivity == "normal" and not _is_expired_non_stable(memory)
    ]
    candidates = [
        memory
        for memory in memories
        if memory.importance >= 7 or memory.usage_count >= 2
    ]
    if not candidates:
        candidates = memories[:]
    candidates.sort(
        key=lambda memory: (
            memory.importance,
            min(memory.usage_count, 10),
            memory.updated_at,
        ),
        reverse=True,
    )
    return candidates[:MAX_SOURCE_MEMORIES]


def _is_expired_non_stable(memory: MemoryRecord) -> bool:
    if memory.stability == "stable":
        return False
    valid_until = _parse_iso_datetime(memory.valid_until)
    return valid_until is not None and valid_until < datetime.now(UTC)


def _candidate_rejection(
    candidate: CoreMemorySectionCandidate,
    valid_memory_ids: set[str],
) -> str | None:
    if not candidate.content.strip():
        return "content 为空"
    if candidate.confidence < MIN_CORE_CONFIDENCE:
        return f"confidence {candidate.confidence} 低于核心记忆阈值 {MIN_CORE_CONFIDENCE}"
    if not candidate.evidence_memory_ids:
        return "缺少 evidence_memory_ids"
    if not any(memory_id in valid_memory_ids for memory_id in candidate.evidence_memory_ids):
        return "evidence_memory_ids 不在输入记忆中"
    return None
