"""/memories routes: review."""
from __future__ import annotations

from app.api.memories.common import *  # noqa: F403

@router.post("/review")
def review_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    reviewer = MemoryReviewer(store=store)
    return reviewer.review(user_id=user_id, limit=limit).model_dump()

@router.post("/review/actions")
def apply_memory_review_action(
    body: MemoryReviewActionRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    memories = _load_review_action_memories(
        store=store,
        user_id=user_id,
        memory_ids=body.memory_ids,
    )
    affected_core_sections = _affected_core_sections_for_memory_ids(
        store=store,
        user_id=user_id,
        memory_ids=[memory.id for memory in memories],
    )
    before = [_memory_audit_payload(memory) for memory in memories]
    default_review_after = (datetime.now(UTC) + timedelta(days=15)).isoformat()
    review_after = body.review_after or default_review_after
    results: list[dict] = []

    if body.action in {"confirm_valid", "snooze"}:
        for memory in memories:
            updated = store.update_memory(
                memory_id=memory.id,
                user_id=user_id,
                content=memory.content,
                type=memory.type,
                importance=memory.importance,
                confidence=memory.confidence,
                valence=memory.valence,
                arousal=memory.arousal,
                source_message=memory.source_message,
                source_conversation_id=memory.source_conversation_id,
                embedding_json=memory.embedding_json,
                stability=memory.stability,
                valid_from=memory.valid_from,
                valid_until=memory.valid_until,
                review_after=review_after,
                sensitivity=memory.sensitivity,
                evidence_memory_ids=memory.evidence_memory_ids,
                temporal_subject=memory.temporal_subject,
                temporal_predicate=memory.temporal_predicate,
            )
            if updated is not None:
                results.append(
                    {
                        "operation": body.action,
                        "memory_id": updated.id,
                        "memory": updated.model_dump(exclude={"embedding_json"}),
                    }
                )
        decision = "ignore" if body.action == "snooze" else "update"
        audit_reason = body.reason or (
            "体检建议已稍后提醒" if body.action == "snooze" else "体检建议已确认仍有效"
        )
    elif body.action == "lower_importance":
        for memory in memories:
            updated = store.update_memory(
                memory_id=memory.id,
                user_id=user_id,
                content=memory.content,
                type=memory.type,
                importance=max(1, memory.importance - 1),
                confidence=memory.confidence,
                valence=memory.valence,
                arousal=memory.arousal,
                source_message=memory.source_message,
                source_conversation_id=memory.source_conversation_id,
                embedding_json=memory.embedding_json,
                stability=memory.stability,
                valid_from=memory.valid_from,
                valid_until=memory.valid_until,
                review_after=memory.review_after,
                sensitivity=memory.sensitivity,
                evidence_memory_ids=memory.evidence_memory_ids,
                temporal_subject=memory.temporal_subject,
                temporal_predicate=memory.temporal_predicate,
            )
            if updated is not None:
                results.append(
                    {
                        "operation": "lower_importance",
                        "memory_id": updated.id,
                        "memory": updated.model_dump(exclude={"embedding_json"}),
                    }
                )
        decision = "update"
        audit_reason = body.reason or "体检建议已降低记忆重要度"
    elif body.action == "move_to_trash":
        archived_ids: list[str] = []
        for memory in memories:
            if store.archive_memory(memory_id=memory.id, user_id=user_id):
                archived_ids.append(memory.id)
        results.append({"operation": "move_to_trash", "archived_memory_ids": archived_ids})
        decision = "update"
        audit_reason = body.reason or "体检建议已移入回收站"
    elif body.action == "merge":
        result = store.merge_memories(
            user_id=user_id,
            memory_ids=[memory.id for memory in memories],
            content=body.content,
        )
        if result.memory is None:
            raise HTTPException(
                status_code=422,
                detail=result.reason,
            )
        results.append(
            {
                "operation": "merge",
                "memory_id": result.memory.id,
                "memory": result.memory.model_dump(exclude={"embedding_json"}),
                "archived_memory_ids": result.archived_memory_ids,
            }
        )
        decision = "update"
        audit_reason = body.reason or "体检建议已合并记忆"

    after = _review_action_after_payload(
        store=store,
        user_id=user_id,
        memory_ids=[memory.id for memory in memories],
    )
    store.create_decision_log(
        user_id=user_id,
        conversation_id=None,
        candidate_json=json.dumps(
            {
                "source": "review_action",
                "action": body.action,
                "memory_ids": [memory.id for memory in memories],
                "risk_tags": body.risk_tags,
                "severity": body.severity,
                "before": before,
                "after": after,
            },
            ensure_ascii=False,
        ),
        decision=decision,
        reason=audit_reason,
    )
    return {
        "applied": True,
        "action": body.action,
        "results": results,
        "affected_core_sections": [
            section.model_dump() for section in affected_core_sections
        ],
    }

@router.post("/review/revise/preview")
async def preview_memory_review_revision(
    body: MemoryReviewRevisionPreviewRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    signing_secret: Annotated[str, Depends(get_signing_secret)],
) -> dict:
    if not settings.allow_sensitive_egress:
        selected = [
            store.get_memory(memory_id=memory_id, user_id=user_id)
            for memory_id in body.memory_ids
        ]
        review_text = "\n".join(
            part
            for part in (
                body.user_note,
                body.recommendation_reason or "",
                body.suggested_content or "",
                *(
                    text
                    for memory in selected
                    if memory is not None
                    for text in (
                        memory.content,
                        memory.source_message or "",
                        *memory.entities,
                    )
                ),
            )
            if part
        )
        if any(memory and memory.sensitivity != "normal" for memory in selected) or (
            detect_text_sensitivity(review_text) != "normal"
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "敏感内容未发送给远程体检模型；如确需处理，请显式启用 "
                    "ALLOW_SENSITIVE_EGRESS"
                ),
            )
    try:
        preview = await preview_review_revision(
            user_id=user_id,
            store=store,
            llm_client=llm_client,
            secret=signing_secret,
            memory_ids=body.memory_ids,
            user_note=body.user_note,
            recommendation_reason=body.recommendation_reason,
            relation=body.relation,
            suggested_content=body.suggested_content,
            risk_tags=body.risk_tags,
            severity=body.severity,
        )
    except ReviewRevisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return preview.model_dump()

@router.post("/review/revise/related")
async def related_memory_review_revision(
    body: MemoryReviewRevisionRelatedRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
) -> dict:
    try:
        return await find_related_review_revision_memories(
            user_id=user_id,
            store=store,
            search_service=search_service,
            memory_ids=body.memory_ids,
            user_note=body.user_note,
            recommendation_reason=body.recommendation_reason,
            suggested_content=body.suggested_content,
            limit=body.limit,
        )
    except ReviewRevisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

@router.post("/review/revise/apply")
def apply_memory_review_revision(
    body: MemoryReviewRevisionApplyRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    signing_secret: Annotated[str, Depends(get_signing_secret)],
) -> dict:
    try:
        return apply_review_revision(
            user_id=user_id,
            store=store,
            secret=signing_secret,
            memory_ids=body.memory_ids,
            operations=body.operations,
            preview_token=body.preview_token,
            risk_tags=body.risk_tags,
            severity=body.severity,
        )
    except ReviewRevisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

@router.post("/deleted/purge/preview")
def preview_deleted_memory_purge(
    body: MemoryBatchPurgePreviewRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    signing_secret: Annotated[str, Depends(get_signing_secret)],
) -> dict:
    try:
        plan = store.preview_archived_memory_purge(
            memory_ids=body.memory_ids,
            user_id=user_id,
        )
    except PurgePreviewConflictError as exc:
        raise _purge_preview_http_conflict(exc) from exc
    requested_ids = list(plan["requested_memory_ids"])
    purge_ids = list(plan["purge_memory_ids"])
    fingerprint = str(plan["fingerprint"])
    token, expires_at = sign_purge_preview(
        secret=signing_secret,
        user_id=user_id,
        requested_memory_ids=requested_ids,
        purge_memory_ids=purge_ids,
        fingerprint=fingerprint,
    )
    return {
        **plan,
        "preview_token": token,
        "expires_at": expires_at,
    }
