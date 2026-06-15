from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import (
    get_embedding_client,
    get_llm_client,
    get_memory_search_service,
    get_memory_store,
    get_user_id,
    require_api_key,
)
from app.llm.client import OpenAICompatibleClient
from app.llm.prompts import (
    render_core_memory_context,
    render_memory_context,
    render_recent_context_summary_context,
)
from app.memory.core import CoreMemoryConsolidator
from app.memory.extractor import validate_candidate_for_save
from app.memory.ingest import MemoryIngestService
from app.memory.models import (
    CandidateMemory,
    CoreMemorySectionName,
    MemorySensitivity,
    MemoryStability,
    MemoryType,
    RecentContextSummary,
)
from app.memory.resolver import MemoryResolver
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


class MemorySaveRequest(BaseModel):
    """直接保存一条结构化记忆，对齐 MCP save_memory。"""
    content: str = Field(min_length=1)
    type: MemoryType = "fact"
    importance: int = Field(default=5, ge=1, le=10)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    stability: MemoryStability = "stable"
    sensitivity: MemorySensitivity = "normal"
    source_quote: str = ""
    valid_until: str | None = None
    review_after: str | None = None


class MemoryForgetRequest(BaseModel):
    """按自然语言搜索并批量软删除，对齐 MCP forget_memories。"""
    query: str = ""
    limit: int = Field(default=5, ge=1, le=10)


class MemoryContextRequest(BaseModel):
    """一站式上下文检索。"""
    query: str = ""
    include_core_memory: bool = True
    include_recent_context: bool = True
    search_limit: int = Field(default=5, ge=1, le=20)
    conversation_id: str | None = None
    format: Literal["json", "markdown"] = "json"


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


@router.post("")
async def save_memory(
    body: MemorySaveRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
) -> dict:
    """直接保存一条结构化记忆，跳过 LLM 提取。对齐 MCP save_memory。"""
    try:
        candidate = CandidateMemory(
            action="create",
            memory=body.content.strip(),
            type=body.type,
            importance=body.importance,
            confidence=body.confidence,
            stability=body.stability,
            sensitivity=body.sensitivity,
            source_quote=body.source_quote.strip(),
            valid_until=body.valid_until,
            review_after=body.review_after,
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]
        field = ".".join(str(p) for p in first_error.get("loc", ()))
        return {"action": "ignore", "reason": f"参数不合法（字段 {field}）"}

    rejection = validate_candidate_for_save(candidate)
    if rejection:
        return {"action": "ignore", "reason": rejection}

    resolver = MemoryResolver(store=store, embedding_client=embedding_client)
    result = await resolver.resolve(
        user_id=user_id,
        candidate=candidate,
        source_message=candidate.source_quote,
        conversation_id=None,
    )
    return {
        "action": result.action,
        "relation": result.relation,
        "reason": result.reason,
        "memory_id": result.memory.id if result.memory else None,
    }


@router.post("/forget")
async def forget_memories(
    body: MemoryForgetRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict:
    """按自然语言搜索并批量软删除。对齐 MCP forget_memories。"""
    normalized_query = body.query.strip()
    if not normalized_query:
        return {"deleted_count": 0, "deleted": [], "query": body.query}

    matches = await search_service.search(
        query=normalized_query,
        user_id=user_id,
        limit=body.limit,
        record_usage=False,
    )
    deleted: list[dict] = []
    for memory in matches:
        if store.archive_memory(memory_id=memory.id, user_id=user_id):
            deleted.append(memory.model_dump(exclude={"embedding_json"}))
    return {
        "deleted_count": len(deleted),
        "deleted": deleted,
        "query": normalized_query,
    }


@router.post("/context", response_model=None)
async def get_memory_context(
    body: MemoryContextRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict | PlainTextResponse:
    """一站式上下文检索：核心记忆 + RAG 检索 + 近期上下文。"""
    core_sections: list = []
    search_results: list = []
    search_results_raw: list = []
    recent_context: dict = {"found": False, "summary": ""}

    search_query = body.query.strip()
    if not search_query and body.conversation_id:
        recent = store.get_recent_context_summary(
            user_id=user_id,
            conversation_id=body.conversation_id,
        )
        if recent and recent.summary:
            lines = recent.summary.splitlines()
            if lines:
                last_line = lines[-1].strip()
                if last_line.startswith("用户："):
                    search_query = last_line[len("用户："):].strip()

    if search_query and body.include_core_memory is not False:
        core_sections = store.list_core_memory_sections(user_id=user_id)

    if search_query:
        search_results_raw = await search_service.search(
            query=search_query,
            user_id=user_id,
            limit=body.search_limit,
            record_usage=False,
        )
        search_results = [
            m.model_dump(exclude={"embedding_json"}) for m in search_results_raw
        ]
    elif body.include_core_memory:
        core_sections = store.list_core_memory_sections(user_id=user_id)

    if body.include_recent_context:
        recent = store.get_recent_context_summary(
            user_id=user_id,
            conversation_id=body.conversation_id,
        )
        if recent:
            recent_context = {"found": True, "summary": recent.summary}

    if body.format == "markdown":
        core_md = render_core_memory_context(core_sections) if core_sections else ""
        recent_md = ""
        if recent_context["found"]:
            recent_obj = RecentContextSummary(
                id="", user_id=user_id, conversation_id=body.conversation_id,
                summary=recent_context["summary"],
                created_at="", updated_at="", archived=0,
            )
            recent_md = render_recent_context_summary_context(recent_obj)
        search_md = ""
        if search_results_raw:
            search_md = render_memory_context(search_results_raw)
        blocks = [b for b in (core_md, recent_md, search_md) if b]
        return PlainTextResponse("\n\n".join(blocks), media_type="text/markdown")

    return {
        "core_memory": [s.model_dump() for s in core_sections] if core_sections else [],
        "search_results": search_results,
        "recent_context": recent_context,
    }


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
