"""/memories routes: core."""
from __future__ import annotations

from app.api.memories.common import *  # noqa: F403

@router.get("/core")
def list_core_memory(
    response: Response,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, list[dict]]:
    sections = store.list_core_memory_sections(user_id=user_id)
    response.headers["ETag"] = _core_memory_collection_etag(sections)
    return {"data": [section.model_dump() for section in sections]}

@router.get("/core/history")
def list_core_memory_history(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    section: CoreMemorySectionName | None = None,
    limit: int = Query(default=50, ge=1, le=5000),
) -> dict[str, list[dict]]:
    history = store.list_core_memory_section_history(
        user_id=user_id,
        section=section,
        limit=limit,
    )
    return {"data": [item.model_dump() for item in history]}

@router.get("/core/{section}")
def get_core_memory_section(
    section: CoreMemorySectionName,
    response: Response,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, dict]:
    core_memory = store.get_core_memory_section(user_id=user_id, section=section)
    if core_memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Core Memory 分区不存在",
        )
    response.headers["ETag"] = _revision_etag(
        "core-memory",
        core_memory.id,
        core_memory.revision,
    )
    return {"core_memory": core_memory.model_dump()}

@router.patch("/core/{section}")
def update_core_memory_section(
    section: CoreMemorySectionName,
    body: CoreMemoryUpdateRequest,
    response: Response,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, object]:
    existing = store.get_core_memory_section(user_id=user_id, section=section)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Core Memory 分区不存在",
        )
    if "content" in body.model_fields_set:
        if body.content is None or not body.content.strip():
            raise HTTPException(status_code=422, detail="content 不能为空")
        content = body.content.strip()
    else:
        content = existing.content
    try:
        action, core_memory = store.upsert_core_memory_section(
            user_id=user_id,
            section=section,
            content=content,
            evidence_memory_ids=(
                body.evidence_memory_ids
                if body.evidence_memory_ids is not None
                else existing.evidence_memory_ids
            ),
            confidence=(
                body.confidence
                if body.confidence is not None
                else existing.confidence
            ),
            expected_revision=body.expected_revision,
        )
    except RevisionConflictError as exc:
        _raise_revision_conflict(exc)
    response.headers["ETag"] = _revision_etag(
        "core-memory",
        core_memory.id,
        core_memory.revision,
    )
    return {
        "updated": action != "ignore",
        "action": action,
        "core_memory": core_memory.model_dump(),
    }

@router.post("/core/consolidate")
async def consolidate_core_memory(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
) -> dict:
    consolidator = CoreMemoryConsolidator(store=store, llm_client=llm_client)
    result = await consolidator.consolidate(user_id=user_id)
    return result.model_dump()
