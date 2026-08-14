"""Independent export/restore helpers for the long-form knowledge database.

The payload intentionally contains canonical text and immutable version
history, but never derived chunks or FTS rows.  Restore always binds imported
records to the authenticated ``user_id`` and lets :class:`KnowledgeStore`
rebuild the local index.
"""

from __future__ import annotations

from typing import Any

from app.knowledge.store import KnowledgeStore, KnowledgeValidationError


def build_knowledge_export(*, store: KnowledgeStore, user_id: str) -> dict[str, Any]:
    payload = store.export_user(user_id=user_id)
    if not isinstance(payload, dict):
        raise KnowledgeValidationError("knowledge export produced an invalid payload")
    return payload

def restore_knowledge_export(
    *,
    store: KnowledgeStore,
    user_id: str,
    export_data: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(export_data, dict):
        raise KnowledgeValidationError("knowledge restore data must be an object")
    payload = store.restore_export(user_id=user_id, export_data=export_data)
    if not isinstance(payload, dict):
        raise KnowledgeValidationError("knowledge restore produced an invalid result")
    return payload
