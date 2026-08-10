"""Shared store exception types."""
from __future__ import annotations


class RevisionConflictError(RuntimeError):
    """A caller attempted to mutate a stale persisted representation."""

    def __init__(
        self,
        *,
        resource: str,
        resource_id: str,
        expected_revision: int,
        current_revision: int,
    ) -> None:
        super().__init__(f"stale {resource} revision")
        self.resource = resource
        self.resource_id = resource_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision

