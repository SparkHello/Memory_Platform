"""Shared MemoryStore constants."""
from __future__ import annotations

from app.sensitivity import SENSITIVITY_RANK as _SENSITIVITY_RANK

_UNSET = object()
_DECISION_LOG_RETENTION_LIMIT = 5000
_CONVERSATION_BRANCH_NODE_RETENTION_LIMIT = 5000
_MEMORY_DB_INIT_LOCK = __import__("threading").Lock()
