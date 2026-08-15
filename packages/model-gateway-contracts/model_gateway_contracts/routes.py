"""Stable route identifiers used by the Memory Gateway integration."""

MEMORY_CHAT_ROUTE = "memory.chat"
MEMORY_EXTRACT_ROUTE = "memory.extract"
MEMORY_COMPACT_ROUTE = "memory.compact"
MEMORY_CORE_ROUTE = "memory.core"
MEMORY_REVIEW_ROUTE = "memory.review"
KNOWLEDGE_FAST_ROUTE = "knowledge.fast"
KNOWLEDGE_PRO_ROUTE = "knowledge.pro"
MEMORY_EMBEDDING_ROUTE = "memory.embedding"

DEFAULT_MEMORY_CHAT_ROUTES: tuple[str, ...] = (
    MEMORY_CHAT_ROUTE,
    MEMORY_EXTRACT_ROUTE,
    MEMORY_COMPACT_ROUTE,
    MEMORY_CORE_ROUTE,
    MEMORY_REVIEW_ROUTE,
    KNOWLEDGE_FAST_ROUTE,
    KNOWLEDGE_PRO_ROUTE,
)

# Deliberately exact: provisioning a backend client must not grant access to a
# future route merely because its name shares a prefix.
DEFAULT_MEMORY_GATEWAY_ROUTES: tuple[str, ...] = (
    *DEFAULT_MEMORY_CHAT_ROUTES,
    MEMORY_EMBEDDING_ROUTE,
)

__all__ = [
    "DEFAULT_MEMORY_CHAT_ROUTES",
    "DEFAULT_MEMORY_GATEWAY_ROUTES",
    "KNOWLEDGE_FAST_ROUTE",
    "KNOWLEDGE_PRO_ROUTE",
    "MEMORY_CHAT_ROUTE",
    "MEMORY_COMPACT_ROUTE",
    "MEMORY_CORE_ROUTE",
    "MEMORY_EMBEDDING_ROUTE",
    "MEMORY_EXTRACT_ROUTE",
    "MEMORY_REVIEW_ROUTE",
]
