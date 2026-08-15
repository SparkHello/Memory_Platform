"""Knowledge persistence package.

The public API is KnowledgeStore (plus the re-exported helpers below).  Its
implementation is split across the focused modules under app.knowledge.store
and orchestrated by _monolith; external code keeps importing from
app.knowledge.store.  The package never reads or writes the memory database.
"""

from __future__ import annotations

# Re-exported so tests can monkeypatch the chunker/limit on this package
# namespace; implementations resolve both lazily from here.
from app.knowledge.chunking import chunk_knowledge_text  # noqa: F401
from app.knowledge.store._monolith import KnowledgeStore  # noqa: F401
from app.knowledge.store.constants import (  # noqa: F401
    _MAX_RESTORE_TOTAL_BYTES,
)
from app.knowledge.store.errors import (  # noqa: F401
    KnowledgeConflictError,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeSensitivityConfirmationRequired,
    KnowledgeValidationError,
)
from app.knowledge.store.utils import detect_knowledge_text_sensitivity  # noqa: F401

__all__ = [
    "KnowledgeConflictError",
    "KnowledgeError",
    "KnowledgeNotFoundError",
    "KnowledgeSensitivityConfirmationRequired",
    "KnowledgeStore",
    "KnowledgeValidationError",
    "chunk_knowledge_text",
    "detect_knowledge_text_sensitivity",
]
