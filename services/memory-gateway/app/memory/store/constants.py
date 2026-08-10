"""Shared MemoryStore constants."""
from __future__ import annotations

_UNSET = object()
_TIME_RIPPLE_MAX_CANDIDATES = 100
_SENSITIVITY_RANK = {"normal": 0, "private": 1, "sensitive": 2}
_DECISION_LOG_RETENTION_LIMIT = 5000
_CONVERSATION_BRANCH_NODE_RETENTION_LIMIT = 5000
_MEMORY_DB_INIT_LOCK = __import__("threading").Lock()
