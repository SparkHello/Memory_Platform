from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_memory_search_service, get_memory_store, get_user_id, require_api_key
from app.memory.search import MemorySearchService
from app.memory.store import MemoryStore

router = APIRouter(
    prefix="/memories",
    tags=["memories"],
    dependencies=[Depends(require_api_key)],
)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)


@router.get("")
def list_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, list[dict]]:
    memories = store.list_memories(user_id=user_id)
    return {"data": [memory.model_dump(exclude={"embedding_json"}) for memory in memories]}


@router.get("/decision-logs")
def list_decision_logs(
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    conversation_id: str | None = None,
    limit: int = 100,
) -> dict[str, list[dict]]:
    logs = store.list_decision_logs(conversation_id=conversation_id, limit=limit)
    return {"data": [log.model_dump() for log in logs]}


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

