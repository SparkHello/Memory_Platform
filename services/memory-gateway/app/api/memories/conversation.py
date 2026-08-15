"""/memories routes: conversation."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_memory_store, get_user_id
from app.api.memories.common import RecentContextUpsertRequest
from app.memory.store import MemoryStore


router = APIRouter()

@router.get("/recent-context")
def list_recent_context_summaries(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, list[dict]]:
    summaries = store.list_recent_context_summaries(user_id=user_id, limit=limit)
    return {"data": [summary.model_dump() for summary in summaries]}

@router.post("/recent-context")
def upsert_recent_context_summary(
    body: RecentContextUpsertRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, dict]:
    summary_text = body.summary.strip()
    if not summary_text:
        raise HTTPException(
            status_code=422,
            detail="summary must not be empty",
        )
    conversation_id = body.conversation_id.strip() if body.conversation_id else None
    summary = store.upsert_recent_context_summary(
        user_id=user_id,
        conversation_id=conversation_id or None,
        summary=summary_text,
    )
    return {"data": summary.model_dump()}

@router.get("/conversation-branches")
def list_conversation_branches(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    limit: int = Query(default=500, ge=1, le=1000),
    status_filter: Literal["active", "archived"] = Query(
        default="active",
        alias="status",
    ),
) -> dict[str, object]:
    archived = status_filter == "archived"
    nodes = store.list_conversation_branch_nodes(
        user_id=user_id,
        limit=limit,
        archived=archived,
    )
    total = store.count_conversation_branch_nodes(
        user_id=user_id,
        archived=archived,
    )
    return {
        "data": [node.model_dump() for node in nodes],
        "meta": {
            "status": status_filter,
            "total": total,
            "returned": len(nodes),
            "truncated": total > len(nodes),
        },
    }

@router.delete("/conversation-branches/{node_id}")
def archive_conversation_branch(
    node_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, object]:
    archived_count = store.archive_conversation_branch_subtree(
        node_id=node_id,
        user_id=user_id,
    )
    if archived_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对话分支不存在或已清理",
        )
    return {
        "id": node_id,
        "archived": True,
        "archived_count": archived_count,
    }

@router.post("/conversation-branches/{node_id}/restore")
def restore_conversation_branch(
    node_id: str,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, object]:
    restored_count = store.restore_conversation_branch_subtree(
        node_id=node_id,
        user_id=user_id,
    )
    if restored_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="已清理的对话分支不存在或已经恢复",
        )
    return {
        "id": node_id,
        "restored": True,
        "restored_count": restored_count,
    }
