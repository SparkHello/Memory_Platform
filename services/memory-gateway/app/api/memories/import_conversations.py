"""Batch import historical conversations into the memory pipeline."""
from __future__ import annotations

import uuid

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import (
    get_embedding_client,
    get_llm_client,
    get_memory_store,
    get_user_id,
)
from app.config import Settings, get_settings
from app.llm.client import OpenAICompatibleClient
from app.memory.conversation_import import (
    MAX_TURNS,
    parse_conversation_import,
)
from app.memory.ingest import MemoryIngestService
from app.memory.search import EmbeddingClient
from app.memory.store import MemoryStore


router = APIRouter()


class ConversationImportRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    max_turns: int = Field(default=MAX_TURNS, ge=1, le=MAX_TURNS)


@router.post("/import/conversations/preview")
def preview_conversation_import(body: ConversationImportRequest) -> dict:
    try:
        preview = parse_conversation_import(body.content, max_turns=body.max_turns)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    sample = [
        {
            "index": turn.index,
            "user_text": turn.user_text[:400],
            "assistant_text": (turn.assistant_text or "")[:400] or None,
            "user_chars": len(turn.user_text),
            "assistant_chars": len(turn.assistant_text or ""),
        }
        for turn in preview.turns[:8]
    ]
    return {
        "format": preview.format,
        "turn_count": preview.turn_count,
        "total_chars": preview.total_chars,
        "truncated": preview.truncated,
        "warnings": preview.warnings,
        "sample_turns": sample,
        "will_not_auto_pin": True,
        "note": (
            "提交后每轮用户消息走与 /memories/ingest 同源的提取门控；"
            "不会自动固定或写入核心记忆。可通过 batch_id 在决策日志中追溯。"
        ),
    }


@router.post("/import/conversations/commit")
async def commit_conversation_import(
    body: ConversationImportRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    llm_client: Annotated[OpenAICompatibleClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    try:
        preview = parse_conversation_import(body.content, max_turns=body.max_turns)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    batch_id = f"import-{uuid.uuid4().hex[:12]}"
    ingester = MemoryIngestService(
        store=store,
        embedding_client=embedding_client,
        llm_client=llm_client,
        allow_sensitive_egress=settings.allow_sensitive_egress,
    )
    turn_results: list[dict] = []
    created_total = 0
    updated_total = 0
    ignored_total = 0

    for turn in preview.turns:
        conversation_id = f"{batch_id}-t{turn.index}"
        try:
            result = await ingester.ingest(
                user_id=user_id,
                text=turn.user_text,
                assistant_message=turn.assistant_text,
                conversation_id=conversation_id,
                source="conversation_import",
            )
        except Exception:  # noqa: BLE001 - per-turn isolation
            turn_results.append(
                {
                    "index": turn.index,
                    "status": "error",
                    "error": "本轮导入失败",
                    "created": 0,
                    "updated": 0,
                    "ignored": 0,
                }
            )
            continue

        created_ids: list[str] = []
        turn_created = 0
        turn_updated = 0
        turn_ignored = 0
        for item in result.items:
            if item.action == "create":
                turn_created += 1
                created_total += 1
                if item.memory_id:
                    created_ids.append(item.memory_id)
            elif item.action == "update":
                turn_updated += 1
                updated_total += 1
                if item.memory_id:
                    created_ids.append(item.memory_id)
            elif item.action == "ignore":
                turn_ignored += 1
                ignored_total += 1

        turn_results.append(
            {
                "index": turn.index,
                "status": result.status,
                "reason": result.reason,
                "created": turn_created,
                "updated": turn_updated,
                "ignored": turn_ignored,
                "memory_ids": created_ids,
            }
        )

    return {
        "batch_id": batch_id,
        "format": preview.format,
        "turn_count": preview.turn_count,
        "truncated": preview.truncated,
        "warnings": preview.warnings,
        "created": created_total,
        "updated": updated_total,
        "ignored": ignored_total,
        "turns": turn_results,
    }
