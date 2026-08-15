"""Memory persistence package.

The public API is MemoryStore (plus the re-exported helpers below). Its
implementation is composed from focused repository functions; external code
keeps importing from app.memory.store.
"""

from __future__ import annotations

from app.memory.classification import (  # noqa: F401
    normalize_classification_name,
    normalize_classification_names,
)
from app.memory.store.repository import (  # noqa: F401
    ClosingSQLiteConnection,
    MemoryStore,
)
from app.memory.store.errors import RevisionConflictError  # noqa: F401
from app.memory.store.purge_ops import PurgePreviewConflictError  # noqa: F401

__all__ = [
    "ClosingSQLiteConnection",
    "MemoryStore",
    "PurgePreviewConflictError",
    "RevisionConflictError",
    "normalize_classification_name",
    "normalize_classification_names",
]
