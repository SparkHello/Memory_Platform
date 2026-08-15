"""Exception hierarchy for the isolated knowledge subsystem."""

from __future__ import annotations

from app.knowledge.models import KnowledgeSensitivity


class KnowledgeError(Exception):
    """Base exception for the isolated knowledge subsystem."""


class KnowledgeValidationError(KnowledgeError, ValueError):
    """The caller supplied malformed or unsafe input."""


class KnowledgeNotFoundError(KnowledgeError, LookupError):
    """A record is missing, belongs to another user, or is not readable."""


class KnowledgeConflictError(KnowledgeError):
    """The requested mutation conflicts with persistent state."""


class KnowledgeSensitivityConfirmationRequired(KnowledgeConflictError):
    """Local detection conflicts with the user's declared sensitivity."""

    def __init__(
        self,
        *,
        declared_sensitivity: KnowledgeSensitivity,
        detected_sensitivity: KnowledgeSensitivity,
    ) -> None:
        self.declared_sensitivity = declared_sensitivity
        self.detected_sensitivity = detected_sensitivity
        super().__init__(
            "local detection classified this document above the selected "
            "sensitivity; explicit user confirmation is required"
        )
