"""/memories routes: search."""
from __future__ import annotations

from app.api.memories.common import *  # noqa: F403

@router.get("/cache-stats")
def memory_search_cache_stats(
    user_id: Annotated[str, Depends(get_user_id)],
) -> dict[str, object]:
    return search_cache_stats(user_id)

@router.post("/search")
async def search_memories(
    body: MemorySearchRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict[str, list[dict]]:
    hits = await search_service.search_hits(
        query=body.query,
        user_id=user_id,
        limit=body.limit,
        include_sensitive=body.include_sensitive,
    )
    return {
        "data": [
            _search_hit_to_dict(hit, redact_sensitive=body.redact_sensitive)
            for hit in hits
        ]
    }

@router.post("/ingest")
async def ingest_memory_text(
    body: MemoryIngestRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    ingester = MemoryIngestService(
        store=store,
        embedding_client=embedding_client,
        llm_client=llm_client,
        allow_sensitive_egress=settings.allow_sensitive_egress,
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

@router.get("/health")
def memory_database_health(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    checker = MemoryHealthChecker(
        store=store,
        expected_embedding_dimensions=settings.embedding_dimensions,
        embedding_enabled=embedding_runtime_enabled(settings),
    )
    return checker.check(user_id=user_id).model_dump()

@router.post("/re-embed")
async def re_embed_memories(
    body: MemoryReEmbedRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """重新生成记忆的 embedding 向量。

    支持两种模式：
    - 指定 memory_ids：对列表中每条活跃记忆重新生成 embedding
    - scan=true：扫描缺失/无效/维度不匹配的活跃记忆并重新生成
    """
    if isinstance(embedding_client, NullEmbeddingClient):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未配置 embedding 服务，无法重新生成 embedding",
        )

    embedding_space_id = embedding_space_id_for(embedding_client)
    if not embedding_space_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="embedding 服务未声明可验证的向量空间，拒绝写入记忆向量",
        )

    memory_ids: list[str] = []
    if body.memory_ids:
        memory_ids = list(dict.fromkeys(body.memory_ids))  # 去重保序
        if not memory_ids:
            raise HTTPException(
                status_code=422,
                detail="memory_ids 不能为空列表",
            )
    elif body.scan:
        memory_ids = _find_memories_needing_embedding(
            store=store,
            user_id=user_id,
            expected_dimensions=settings.embedding_dimensions,
            expected_space_id=embedding_space_id,
        )
    else:
        raise HTTPException(
            status_code=422,
            detail="请指定 memory_ids 或设置 scan=true",
        )

    if not memory_ids:
        return {"re_embedded": 0, "memory_ids": [], "failed_ids": []}

    succeeded: list[str] = []
    failed: list[str] = []
    for memory_id in memory_ids:
        memory = store.get_memory(memory_id=memory_id, user_id=user_id)
        if memory is None:
            failed.append(memory_id)
            continue
        if memory.sensitivity != "normal" and not body.include_sensitive:
            failed.append(memory_id)
            continue
        with model_usage_scope(user_id=user_id, operation="memory_reembed"):
            embedding = await embedding_client.embed(memory.content)
        if embedding is None:
            failed.append(memory_id)
            continue
        embedding_json = json.dumps(embedding, ensure_ascii=False)
        if store.update_memory_embedding(
            memory_id=memory_id,
            user_id=user_id,
            embedding_json=embedding_json,
            embedding_space_id=embedding_space_id,
        ):
            succeeded.append(memory_id)
        else:
            failed.append(memory_id)

    return {
        "re_embedded": len(succeeded),
        "memory_ids": succeeded,
        "failed_ids": failed,
    }

@router.post("/archive-expired")
def archive_expired_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    """归档所有 valid_until 已过期的活跃记忆。

    valid_until 会按 ISO 8601 解析并统一比较实际时刻，支持不同时区偏移。
    """
    count = store.archive_expired_memories(user_id=user_id)
    return {"archived": count}

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

    search_query = await anyio.to_thread.run_sync(
        partial(
            _derive_context_search_query,
            query=body.query,
            user_id=user_id,
            store=store,
            conversation_id=body.conversation_id,
        )
    )

    if search_query and body.include_core_memory is not False:
        core_sections = await anyio.to_thread.run_sync(
            partial(_safe_core_sections, store=store, user_id=user_id)
        )

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
        core_sections = await anyio.to_thread.run_sync(
            partial(_safe_core_sections, store=store, user_id=user_id)
        )

    if body.include_recent_context:
        recent = await anyio.to_thread.run_sync(
            partial(
                store.get_recent_context_summary,
                user_id=user_id,
                conversation_id=body.conversation_id,
            )
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

@router.post("/context/explain")
async def explain_memory_context(
    body: MemoryContextExplainRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict:
    """解释上下文构建过程；该调试接口不会增加 usage_count。"""
    search_query = await anyio.to_thread.run_sync(
        partial(
            _derive_context_search_query,
            query=body.query,
            user_id=user_id,
            store=store,
            conversation_id=body.conversation_id,
        )
    )
    core_sections = (
        await anyio.to_thread.run_sync(
            partial(_safe_core_sections, store=store, user_id=user_id)
        )
        if body.include_core_memory
        else []
    )
    core_payload = [section.model_dump() for section in core_sections]
    recent_context = await anyio.to_thread.run_sync(
        partial(
            _recent_context_payload,
            store=store,
            user_id=user_id,
            conversation_id=body.conversation_id,
            include_recent_context=body.include_recent_context,
        )
    )

    search_hits = []
    if search_query:
        explain_limit = min(20, max(body.limit + 5, body.limit * 2))
        search_hits = await search_service.search_hits(
            query=search_query,
            user_id=user_id,
            limit=explain_limit,
            record_usage=False,
            include_sensitive=body.include_sensitive,
        )

    selected_hits = search_hits[: body.limit]
    search_results = [
        _search_hit_to_dict(hit, redact_sensitive=body.redact_sensitive)
        for hit in selected_hits
    ]
    candidate_pool = [
        _search_hit_to_dict(hit, redact_sensitive=body.redact_sensitive)
        for hit in search_hits
    ]
    excluded_candidates: list[dict] = []
    for hit in search_hits[body.limit:]:
        payload = _search_hit_to_dict(hit, redact_sensitive=body.redact_sensitive)
        payload["excluded_reason"] = "rank_below_limit"
        excluded_candidates.append(payload)

    context_package = {
        "query": search_query,
        "core_memory": core_payload,
        "search_results": search_results,
        "recent_context": recent_context,
    }
    return {
        "context_package": context_package,
        "core_memory": core_payload,
        "search_results": search_results,
        "recent_context": recent_context,
        "candidate_pool": candidate_pool,
        "excluded_candidates": excluded_candidates,
    }

@router.post("/search-feedback")
def create_search_feedback(
    body: MemorySearchFeedbackRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    memory_id = body.memory_id.strip() if body.memory_id else None
    if body.feedback != "missing" and not memory_id:
        raise HTTPException(
            status_code=422,
            detail="memory_id 是必填项",
        )
    if memory_id and store.get_memory(memory_id=memory_id, user_id=user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或已删除",
        )

    log = store.create_decision_log(
        user_id=user_id,
        conversation_id=None,
        candidate_json=json.dumps(
            {
                "source": "search_feedback",
                "query": body.query.strip(),
                "memory_id": memory_id,
                "feedback": body.feedback,
                "note": (body.note or "").strip(),
            },
            ensure_ascii=False,
        ),
        decision="ignore",
        reason=f"召回反馈：{body.feedback}",
    )
    return {"recorded": True, "log": log.model_dump()}
