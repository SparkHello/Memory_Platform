"""Independent long-form knowledge storage and retrieval.

Knowledge records deliberately live outside ``app.memory``.  Importing this
package never creates a database or changes the memory retrieval pipeline.
"""

from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeCommitResult,
    KnowledgeDocument,
    KnowledgeIndexStatus,
    KnowledgeSearchHit,
    KnowledgeSensitivity,
    KnowledgeUploadPart,
    KnowledgeUploadSession,
    KnowledgeVersion,
)
from app.knowledge.store import (
    KnowledgeConflictError,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeStore,
    KnowledgeValidationError,
)

__all__ = [
    "KnowledgeChunk",
    "KnowledgeCommitResult",
    "KnowledgeConflictError",
    "KnowledgeDocument",
    "KnowledgeError",
    "KnowledgeIndexStatus",
    "KnowledgeNotFoundError",
    "KnowledgeSearchHit",
    "KnowledgeSensitivity",
    "KnowledgeStore",
    "KnowledgeUploadPart",
    "KnowledgeUploadSession",
    "KnowledgeValidationError",
    "KnowledgeVersion",
]
