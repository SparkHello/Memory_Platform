"""/memories routes: purge."""
from __future__ import annotations

import json
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import (
    get_memory_search_service,
    get_memory_store,
    get_signing_secret,
    get_user_id,
)
from app.api.memories.common import (
    MemoryBatchPurgeCommitRequest,
    MemoryForgetRequest,
    MemoryPurgeRequest,
    _cleanup_eval_after_purge,
    _memory_to_response,
    _purge_preview_http_conflict,
)
from app.config import Settings, get_settings
from app.memory.evaluation_workspace import (
    evaluation_workspace_lock,
    restore_staged_eval_workspace,
    stage_user_eval_workspace,
)
from app.memory.purge_preview import (
    PurgePreviewTokenError,
    purge_memory_ids_digest,
    verify_purge_preview,
)
from app.memory.search import MemorySearchService
from app.memory.store import MemoryStore, PurgePreviewConflictError


router = APIRouter()

@router.get("/deleted")
def list_deleted_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=200, ge=1, le=1000),
    redact_sensitive: bool = False,
) -> dict[str, list[dict]]:
    memories = store.list_archived_memories(user_id=user_id, limit=limit)
    return {
        "data": [
            _memory_to_response(memory, redact_sensitive=redact_sensitive)
            for memory in memories
        ]
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

    def _archive_matches() -> list[dict]:
        archived: list[dict] = []
        for memory in matches:
            if store.archive_memory(memory_id=memory.id, user_id=user_id):
                archived.append(memory.model_dump(exclude={"embedding_json"}))
        return archived

    deleted = await anyio.to_thread.run_sync(_archive_matches)
    return {
        "deleted_count": len(deleted),
        "deleted": deleted,
        "query": normalized_query,
    }

@router.post("/deleted/purge/commit")
def commit_deleted_memory_purge(
    body: MemoryBatchPurgeCommitRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    signing_secret: Annotated[str, Depends(get_signing_secret)],
) -> dict:
    try:
        token_payload = verify_purge_preview(
            secret=signing_secret,
            token=body.preview_token,
        )
    except PurgePreviewTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "purge_preview_invalid",
                "message": str(exc),
            },
        ) from exc

    requested_ids = sorted(body.memory_ids)
    token_requested_ids = token_payload.get("requested_memory_ids")
    token_purge_digest = token_payload.get("purge_memory_ids_sha256")
    token_purge_count = token_payload.get("purge_memory_count")
    if (
        token_payload.get("user_id") != user_id
        or token_payload.get("fingerprint") != body.fingerprint
        or not isinstance(token_requested_ids, list)
        or not all(isinstance(item, str) for item in token_requested_ids)
        or sorted(token_requested_ids) != requested_ids
        or not isinstance(token_purge_digest, str)
        or len(token_purge_digest) != 64
        or not isinstance(token_purge_count, int)
        or token_purge_count < len(requested_ids)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "purge_preview_mismatch",
                "message": "永久删除提交与签名预览的用户、所选 ID 或 fingerprint 不一致。",
            },
        )
    with evaluation_workspace_lock(settings.eval_dir):
        try:
            current_plan = store.preview_archived_memory_purge(
                memory_ids=requested_ids,
                user_id=user_id,
            )
        except PurgePreviewConflictError as exc:
            raise _purge_preview_http_conflict(exc) from exc
        purge_ids = [str(item) for item in current_plan["purge_memory_ids"]]
        if (
            len(purge_ids) != token_purge_count
            or purge_memory_ids_digest(purge_ids) != token_purge_digest
            or current_plan.get("fingerprint") != body.fingerprint
        ):
            raise _purge_preview_http_conflict(
                PurgePreviewConflictError(
                    code="purge_preview_stale",
                    message=(
                        "永久删除预览已过期：所选记忆、依赖闭包或 Core 影响已变化。"
                    ),
                )
            )
        staged_eval = stage_user_eval_workspace(
            settings.eval_dir,
            user_id=user_id,
            target_memory_ids=purge_ids,
            database_path=settings.database_path,
        )
        try:
            result, log = store.commit_archived_memory_purge(
                memory_ids=requested_ids,
                user_id=user_id,
                expected_purge_memory_ids_digest=token_purge_digest,
                expected_purge_memory_count=token_purge_count,
                expected_fingerprint=body.fingerprint,
                call_source="rest_api",
            )
        except PurgePreviewConflictError as exc:
            restore_staged_eval_workspace(staged_eval)
            raise _purge_preview_http_conflict(exc) from exc
        except Exception:
            restore_staged_eval_workspace(staged_eval)
            raise

        evaluation_cleanup, warnings = _cleanup_eval_after_purge(
            staged=staged_eval,
        )
        payload = {
            "purged": True,
            **result,
            "audit_log_id": log.id,
            "evaluation_cleanup": evaluation_cleanup,
        }
        if warnings:
            payload["warnings"] = warnings
        return payload

@router.delete("/deleted/{memory_id}/purge")
def purge_deleted_memory(
    memory_id: str,
    body: MemoryPurgeRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    if body.confirm_memory_id != memory_id:
        raise HTTPException(
            status_code=422,
            detail="confirm_memory_id 必须与路径中的 memory_id 完全一致",
        )

    with evaluation_workspace_lock(settings.eval_dir):
        try:
            store.preview_archived_memory_purge(
                memory_ids=[memory_id],
                user_id=user_id,
            )
        except PurgePreviewConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Memory does not exist or is not deleted.",
            ) from exc
        affected_core_sections = store.list_purge_affected_core_sections(
            memory_id=memory_id,
            user_id=user_id,
        )
        affected_payload = [section.model_dump() for section in affected_core_sections]
        staged_eval = stage_user_eval_workspace(
            settings.eval_dir,
            user_id=user_id,
            target_memory_ids=[memory_id],
            database_path=settings.database_path,
        )
        try:
            result = store.purge_archived_memory(
                memory_id=memory_id,
                user_id=user_id,
                affected_core_sections=affected_payload,
                call_source="rest_api",
            )
        except Exception:
            restore_staged_eval_workspace(staged_eval)
            raise
        if result is None:
            restore_staged_eval_workspace(staged_eval)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Memory does not exist or is not deleted.",
            )
        _, log = result
        try:
            purge_audit = json.loads(log.candidate_json)
        except (TypeError, ValueError):
            purge_audit = {}
        actual_affected = purge_audit.get("affected_core_sections")
        if isinstance(actual_affected, list):
            affected_payload = actual_affected
        purge_effects = purge_audit.get("scrubbed_artifacts")
        eval_cleanup, warnings = _cleanup_eval_after_purge(
            staged=staged_eval,
        )
        payload = {
            "purged": True,
            "id": memory_id,
            "compatibility_mode": "legacy_single_purge_v1",
            "audit_log_id": log.id,
            "affected_core_memory_sections": affected_payload,
            "evaluation_cleanup": eval_cleanup,
        }
        if isinstance(purge_effects, dict):
            payload["purge_effects"] = purge_effects
        if warnings:
            payload["warnings"] = warnings
        return payload
