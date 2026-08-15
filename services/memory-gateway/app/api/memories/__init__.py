"""Composed ``/memories`` HTTP API."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_api_key

from app.api.memories import (  # noqa: F401
    conversation,
    core,
    crud,
    evaluation,
    export,
    graph,
    import_conversations,
    purge,
    review,
    search,
    item,  # /{memory_id} must be last
)

router = APIRouter(
    tags=["memories"],
    dependencies=[Depends(require_api_key)],
)

# Child routers have no registration side effects. Static paths remain ahead
# of the catch-all item routes for Starlette's ordered matching.
for domain_router in (
    conversation.router,
    core.router,
    crud.router,
    evaluation.router,
    export.router,
    graph.router,
    import_conversations.router,
    purge.router,
    review.router,
    search.router,
    item.router,
):
    router.include_router(domain_router, prefix="/memories")

__all__ = ["router"]
