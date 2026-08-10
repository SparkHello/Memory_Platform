"""/memories routes: crud."""
from __future__ import annotations

from app.api.memories.common import *  # noqa: F403


@router.get("")
def list_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    redact_sensitive: bool = False,
    status_filter: Annotated[MemoryStatus | Literal["all"] | None, Query(alias="status")] = None,
) -> dict[str, list[dict]]:
    memories = store.list_memories(user_id=user_id, status=status_filter)
    return {
        "data": [
            _memory_to_response(memory, redact_sensitive=redact_sensitive)
            for memory in memories
        ]
    }

@router.get("/spaces")
def list_memory_spaces(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, list[dict]]:
    return {"data": store.list_memory_space_summaries(user_id=user_id)}

@router.get("/spaces/{space_id}")
def get_memory_space(
    space_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=200, ge=1, le=1000),
    redact_sensitive: bool = False,
) -> dict:
    space = store.get_memory_space(user_id=user_id, space_id=space_id)
    if space is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="空间不存在或已归档",
        )
    memories = store.list_memories_for_space(
        user_id=user_id,
        space_id=space_id,
        limit=limit,
    )
    return {
        "space": space.model_dump(),
        "memories": [
            _memory_to_response(memory, redact_sensitive=redact_sensitive)
            for memory in memories
        ],
    }

@router.get("/decision-logs")
def list_decision_logs(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    conversation_id: Annotated[
        str | None,
        Query(max_length=PUBLIC_ID_MAX_CHARS),
    ] = None,
    memory_id: Annotated[
        str | None,
        Query(max_length=PUBLIC_ID_MAX_CHARS),
    ] = None,
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict[str, list[dict]]:
    logs = store.list_decision_logs(
        user_id=user_id,
        conversation_id=conversation_id,
        memory_id=memory_id,
        limit=limit,
    )
    return {"data": [log.model_dump() for log in logs]}

@router.get("/timeline")
def get_memory_timeline(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    subject: Annotated[str, Query(min_length=1)],
    predicate: str | None = None,
    include_archived: bool = False,
    redact_sensitive: bool = False,
) -> dict[str, object]:
    memories = store.list_memory_timeline(
        user_id=user_id,
        subject=subject,
        predicate=predicate,
        include_archived=include_archived,
    )
    return {
        "subject": normalize_optional_text(subject),
        "predicate": normalize_optional_text(predicate),
        "data": [
            _memory_to_response(memory, redact_sensitive=redact_sensitive)
            for memory in memories
        ],
    }

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
            valence=body.valence,
            arousal=body.arousal,
            stability=body.stability,
            sensitivity=sensitivity_floor(
                body.sensitivity,
                body.content,
                body.source_quote,
                *(body.entities or []),
            ),
            source_quote=body.source_quote.strip(),
            valid_from=body.valid_from,
            valid_until=body.valid_until,
            review_after=body.review_after,
            temporal_subject=body.temporal_subject,
            temporal_predicate=body.temporal_predicate,
            topics=body.topics or [],
            entities=body.entities or [],
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]
        field = ".".join(str(p) for p in first_error.get("loc", ()))
        return {"action": "ignore", "reason": f"参数不合法（字段 {field}）"}

    rejection = validate_candidate_for_save(candidate)
    if rejection:
        return {"action": "ignore", "reason": rejection}

    resolver = MemoryResolver(store=store, embedding_client=embedding_client)
    fields_set = getattr(body, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(body, "__fields_set__", set())
    explicit_classification = bool({"topics", "entities"} & set(fields_set))
    result = await resolver.resolve(
        user_id=user_id,
        candidate=candidate,
        source_message=candidate.source_quote,
        conversation_id=None,
        auto_classify=not explicit_classification,
    )
    return {
        "action": result.action,
        "relation": result.relation,
        "reason": result.reason,
        "memory_id": result.memory.id if result.memory else None,
    }

