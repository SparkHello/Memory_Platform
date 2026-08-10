"""Memory persistence package.

Implementation currently lives in ``_monolith`` while it is being sliced into
focused modules. External code should keep importing from ``app.memory.store``.
"""

from __future__ import annotations

from typing import Any

from app.memory.classification import (  # noqa: F401
    normalize_classification_name,
    normalize_classification_names,
)
from app.memory.store import _monolith as _impl
from app.memory.store._monolith import (  # noqa: F401
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


def __getattr__(name: str) -> Any:
    """Expose monolith internals for tests and gradual extraction."""
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(dir(_impl)))
