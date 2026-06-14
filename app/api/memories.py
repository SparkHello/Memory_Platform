from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.api.deps import (
    get_embedding_client,
    get_llm_client,
    get_memory_search_service,
    get_memory_store,
    get_user_id,
    require_api_key,
)
from app.llm.client import OpenAICompatibleClient
from app.memory.core import CoreMemoryConsolidator
from app.memory.ingest import MemoryIngestService
from app.memory.models import (
    CoreMemorySectionName,
    MemorySensitivity,
    MemoryStability,
    MemoryType,
)
from app.memory.review import MemoryReviewer
from app.memory.report import (
    build_memory_export,
    build_memory_report,
    format_memory_export,
    restore_memory_export,
)
from app.memory.search import EmbeddingClient, MemorySearchService
from app.memory.store import MemoryStore

router = APIRouter(
    prefix="/memories",
    tags=["memories"],
    dependencies=[Depends(require_api_key)],
)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)


class MemoryMergeRequest(BaseModel):
    memory_ids: list[str] = Field(min_length=2)
    content: str | None = None


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    type: MemoryType | None = None
    importance: int | None = Field(default=None, ge=1, le=10)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    stability: MemoryStability | None = None
    valid_until: str | None = None
    review_after: str | None = None
    sensitivity: MemorySensitivity | None = None
    source_message: str | None = None
    source_conversation_id: str | None = None


class MemoryRestoreExportRequest(BaseModel):
    data: dict
    overwrite: bool = False
    include_deleted: bool = False


class MemoryIngestRequest(BaseModel):
    text: str = Field(min_length=1)
    conversation_id: str | None = None


@router.get("")
def list_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, list[dict]]:
    memories = store.list_memories(user_id=user_id)
    return {"data": [memory.model_dump(exclude={"embedding_json"}) for memory in memories]}


@router.get("/deleted")
def list_deleted_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, list[dict]]:
    memories = store.list_archived_memories(user_id=user_id, limit=limit)
    return {"data": [memory.model_dump(exclude={"embedding_json"}) for memory in memories]}


@router.get("/report", response_model=None)
def get_memory_report(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    response_format: Literal["json", "markdown"] = Query(default="json", alias="format"),
) -> dict | PlainTextResponse:
    report = build_memory_report(store=store, user_id=user_id)
    if response_format == "markdown":
        return PlainTextResponse(report["markdown"], media_type="text/markdown")
    return report


@router.get("/export", response_model=None)
def export_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    include_deleted: bool = True,
    response_format: Literal["json", "markdown"] = Query(default="json", alias="format"),
) -> dict | PlainTextResponse:
    export_data = build_memory_export(
        store=store,
        user_id=user_id,
        include_deleted=include_deleted,
    )
    if response_format == "markdown":
        return PlainTextResponse(
            format_memory_export(export_data),
            media_type="text/markdown",
        )
    return export_data


@router.post("/restore")
def restore_memories_from_export(
    body: MemoryRestoreExportRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    return restore_memory_export(
        store=store,
        user_id=user_id,
        export_data=body.data,
        overwrite=body.overwrite,
        include_deleted=body.include_deleted,
    )


@router.get("/decision-logs")
def list_decision_logs(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    conversation_id: str | None = None,
    limit: int = 100,
) -> dict[str, list[dict]]:
    logs = store.list_decision_logs(
        user_id=user_id,
        conversation_id=conversation_id,
        limit=limit,
    )
    return {"data": [log.model_dump() for log in logs]}


@router.get("/recent-context")
def list_recent_context_summaries(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = 20,
) -> dict[str, list[dict]]:
    summaries = store.list_recent_context_summaries(user_id=user_id, limit=limit)
    return {"data": [summary.model_dump() for summary in summaries]}


@router.get("/core")
def list_core_memory(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, list[dict]]:
    sections = store.list_core_memory_sections(user_id=user_id)
    return {"data": [section.model_dump() for section in sections]}


@router.get("/core/history")
def list_core_memory_history(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    section: CoreMemorySectionName | None = None,
    limit: int = 50,
) -> dict[str, list[dict]]:
    history = store.list_core_memory_section_history(
        user_id=user_id,
        section=section,
        limit=limit,
    )
    return {"data": [item.model_dump() for item in history]}


@router.post("/core/consolidate")
async def consolidate_core_memory(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
) -> dict:
    consolidator = CoreMemoryConsolidator(store=store, llm_client=llm_client)
    result = await consolidator.consolidate(user_id=user_id)
    return result.model_dump()


@router.post("/search")
async def search_memories(
    body: MemorySearchRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict[str, list[dict]]:
    memories = await search_service.search(
        query=body.query,
        user_id=user_id,
        limit=body.limit,
    )
    return {"data": [memory.model_dump(exclude={"embedding_json"}) for memory in memories]}


@router.post("/ingest")
async def ingest_memory_text(
    body: MemoryIngestRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
) -> dict:
    ingester = MemoryIngestService(
        store=store,
        embedding_client=embedding_client,
        llm_client=llm_client,
    )
    result = await ingester.ingest(
        user_id=user_id,
        text=body.text,
        conversation_id=body.conversation_id,
        source="rest_ingest",
    )
    return result.model_dump()


@router.post("/merge")
def merge_memories(
    body: MemoryMergeRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    result = store.merge_memories(
        user_id=user_id,
        memory_ids=body.memory_ids,
        content=body.content,
    )
    payload = result.model_dump()
    if result.memory:
        payload["memory"] = result.memory.model_dump(exclude={"embedding_json"})
    return payload


@router.post("/review")
def review_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = 200,
) -> dict:
    reviewer = MemoryReviewer(store=store)
    return reviewer.review(user_id=user_id, limit=limit).model_dump()


@router.patch("/{memory_id}")
def update_memory(
    memory_id: str,
    body: MemoryUpdateRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    existing = store.get_memory(memory_id=memory_id, user_id=user_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )

    updates = body.model_dump(exclude_unset=True)
    if "content" in body.model_fields_set:
        if body.content is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="content 不能为 null",
            )
        content = body.content.strip()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="content 不能为空",
            )
        updates["content"] = content

    content = updates.get("content", existing.content)
    embedding_json = None if content != existing.content else existing.embedding_json

    memory = store.update_memory(
        memory_id=memory_id,
        user_id=user_id,
        content=content,
        type=updates.get("type", existing.type),
        importance=updates.get("importance", existing.importance),
        confidence=updates.get("confidence", existing.confidence),
        source_message=updates.get("source_message", existing.source_message),
        source_conversation_id=updates.get(
            "source_conversation_id",
            existing.source_conversation_id,
        ),
        embedding_json=embedding_json,
        stability=updates.get("stability", existing.stability),
        valid_until=updates.get("valid_until", existing.valid_until),
        review_after=updates.get("review_after", existing.review_after),
        sensitivity=updates.get("sensitivity", existing.sensitivity),
        evidence_memory_ids=existing.evidence_memory_ids,
    )
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    return {"updated": True, "memory": memory.model_dump(exclude={"embedding_json"})}


@router.post("/{memory_id}/restore")
def restore_memory(
    memory_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    memory = store.restore_memory(memory_id=memory_id, user_id=user_id)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory does not exist or is not deleted.",
        )
    return {"restored": True, "memory": memory.model_dump(exclude={"embedding_json"})}


@router.get("/{memory_id}/why")
def why_remember(
    memory_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    explanation = store.explain_memory_source(memory_id=memory_id, user_id=user_id)
    if explanation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    return explanation.model_dump()


@router.delete("/{memory_id}")
def delete_memory(
    memory_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, str | bool]:
    archived = store.archive_memory(memory_id=memory_id, user_id=user_id)
    if not archived:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )
    return {"id": memory_id, "archived": True}
