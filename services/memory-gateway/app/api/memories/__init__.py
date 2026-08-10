"""Memories HTTP API package.

Routes are split by domain modules that register on the shared
``common.router`` (prefix ``/memories``).

Import order matters: static paths must register before ``/{memory_id}``.
"""
from __future__ import annotations

from app.api.memories.common import router

# Static-path domains first, then parametric item routes last.
from app.api.memories import (  # noqa: F401
    conversation,
    core,
    crud,
    evaluation,
    export,
    graph,
    purge,
    review,
    search,
    item,  # /{memory_id} must be last
)

__all__ = ["router"]
