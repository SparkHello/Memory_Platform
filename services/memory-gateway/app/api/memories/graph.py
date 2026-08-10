"""/memories routes: graph."""
from __future__ import annotations

from app.api.memories.common import *  # noqa: F403

@router.post("/surface")
def surface_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    search_service: Annotated[MemorySearchService, Depends(get_memory_search_service)],
    body: MemorySurfaceRequest | None = None,
) -> dict[str, list[dict]]:
    hits = search_service.surface_memories(
        user_id=user_id,
        limit=body.limit if body else 8,
        mode=body.mode if body else "balanced",
        include_archived=body.include_archived if body else False,
        include_sensitive=body.include_sensitive if body else False,
    )
    return {
        "data": [
            _surface_hit_to_dict(
                hit,
                redact_sensitive=body.redact_sensitive if body else False,
            )
            for hit in hits
        ]
    }

@router.post("/network")
def memory_network(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    body: MemoryNetworkRequest | None = None,
) -> dict:
    request = body or MemoryNetworkRequest()
    return build_memory_network(
        store=store,
        user_id=user_id,
        limit=request.limit,
        similarity_threshold=request.similarity_threshold,
        max_similarity_edges=request.max_similarity_edges,
        space_id=request.space_id,
        memory_type=request.type,
        sensitivity=request.sensitivity,
        valence_min=request.valence_min,
        valence_max=request.valence_max,
        arousal_min=request.arousal_min,
        arousal_max=request.arousal_max,
        redact_sensitive=request.redact_sensitive,
    )

@router.post("/network/traverse")
def memory_network_traverse(
    body: MemoryNetworkTraverseRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    result = traverse_memory_network(
        store=store,
        user_id=user_id,
        seed_id=body.seed_id.strip(),
        depth=body.depth,
        limit=body.limit,
        similarity_threshold=body.similarity_threshold,
        max_candidates=body.max_candidates,
        max_edges=body.max_edges,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="seed memory not found or archived",
        )
    return {
        "seed": _memory_to_response(
            result.seed,
            redact_sensitive=body.redact_sensitive,
        ),
        "results": [
            {
                "memory": _memory_to_response(
                    item.memory,
                    redact_sensitive=body.redact_sensitive,
                ),
                "score": round(item.score, 6),
                "depth": item.depth,
                "path": [_traversal_edge_to_dict(edge) for edge in item.path],
            }
            for item in result.results
        ],
        "meta": {
            "depth": result.meta.depth,
            "limit": result.meta.limit,
            "similarity_threshold": result.meta.similarity_threshold,
            "candidate_count": result.meta.candidate_count,
            "edge_count": result.meta.edge_count,
            "reachable_count": result.meta.reachable_count,
            "iterations": result.meta.iterations,
            "converged": result.meta.converged,
        },
    }
