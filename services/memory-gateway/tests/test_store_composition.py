from __future__ import annotations

from inspect import Signature, signature

from app.knowledge.store import KnowledgeStore
from app.knowledge.store import documents as knowledge_documents
from app.knowledge.store import export_import as knowledge_export
from app.knowledge.store import helpers as knowledge_helpers
from app.knowledge.store import references as knowledge_references
from app.knowledge.store import search as knowledge_search
from app.knowledge.store import status as knowledge_status
from app.knowledge.store import uploads as knowledge_uploads
from app.memory.store import MemoryStore
from app.memory.store import chat_finalize
from app.memory.store import conversation
from app.memory.store import core_memory
from app.memory.store import crud
from app.memory.store import decision_logs
from app.memory.store import digest
from app.memory.store import export_import as memory_export
from app.memory.store import fts
from app.memory.store import helpers as memory_helpers
from app.memory.store import lifecycle_purge
from app.memory.store import merge
from app.memory.store import spaces
from app.memory.store import temporal


MEMORY_BINDINGS = {
    chat_finalize: (
        "claim_chat_side_effect",
        "release_chat_side_effect_claim",
        "enqueue_chat_finalize_job",
        "mark_chat_finalize_job",
        "claim_chat_finalize_job",
        "prune_chat_finalize_jobs",
    ),
    crud: (
        "create_memory",
        "update_memory",
        "get_memory",
        "list_memory_timeline",
        "list_memories",
        "list_memories_for_resolution",
        "memory_recall_snapshot",
        "get_memories_max_updated_at",
        "get_active_memory_count",
        "list_archived_memories",
        "explain_memory_source",
        "archive_memory",
        "restore_memory",
        "update_memory_embedding",
        "archive_expired_memories",
        "mark_memories_used",
        "update_memory_statuses",
    ),
    temporal: ("restore_temporal_memory", "get_next_temporal_boundary"),
    fts: ("keyword_candidate_memories",),
    memory_export: (
        "list_all_memories_for_export",
        "read_memory_export_snapshot",
        "read_memory_selection_export_snapshot",
        "prepare_memory_space_import",
        "import_memory_space",
        "plan_memory_import_ids",
        "filter_existing_memory_ids",
        "prune_dangling_memory_references",
        "restore_prepared_export",
        "prepare_memory_import_record",
        "import_memory_record",
    ),
    core_memory: (
        "list_core_memory_sections",
        "get_core_memory_section",
        "upsert_core_memory_section",
        "archive_core_memory_section",
        "list_core_memory_section_history",
    ),
    merge: ("merge_memories",),
    conversation: (
        "get_recent_context_summary",
        "get_recent_context_summary_for_conversation",
        "list_recent_context_summaries",
        "upsert_recent_context_summary",
        "upsert_recent_context_state",
        "get_conversation_branch_node",
        "list_conversation_branch_nodes",
        "count_conversation_branch_nodes",
        "archive_conversation_branch_subtree",
        "restore_conversation_branch_subtree",
        "upsert_conversation_branch_node",
    ),
    lifecycle_purge: (
        "preview_archived_memory_purge",
        "commit_archived_memory_purge",
        "purge_archived_memory",
        "list_purge_affected_core_sections",
    ),
    spaces: (
        "upsert_memory_space",
        "list_memory_spaces",
        "list_memory_space_summaries",
        "get_memory_space",
        "create_memory_space",
        "update_memory_space",
        "set_memory_space_archived",
        "delete_memory_space",
        "list_memories_for_space",
        "replace_memory_spaces",
    ),
    digest: (
        "list_undigested_memories",
        "get_digest_source_memories",
        "apply_memory_digest",
    ),
    decision_logs: ("create_decision_log", "list_decision_logs"),
}

KNOWLEDGE_BINDINGS = {
    knowledge_uploads: (
        "begin_upload",
        "append_upload",
        "commit_upload",
        "cancel_upload",
    ),
    knowledge_documents: (
        "list_documents",
        "resolve_document_refs",
        "get_document_detail",
        "get_version",
        "update_document",
        "soft_delete_document",
        "restore_document",
        "purge_document",
        "restore_version",
        "reindex_version",
    ),
    knowledge_search: (
        "search_chunks",
        "egress_override_confirmed",
        "list_chunks_for_embedding",
        "set_version_embedding_status",
        "replace_chunk_embeddings",
        "search_chunks_by_embedding",
        "get_chunks_by_refs",
    ),
    knowledge_references: ("read_reference",),
    knowledge_export: ("list_versions", "export_user", "restore_export"),
    knowledge_status: ("counts", "status"),
}


def _bound_signature(function) -> Signature:
    parameters = tuple(signature(function).parameters.values())[1:]
    return signature(function).replace(parameters=parameters)


def _assert_direct_bindings(store_class, store, bindings) -> set[str]:
    names: set[str] = set()
    for module, module_names in bindings.items():
        for name in module_names:
            implementation = getattr(module, name)
            assert getattr(store_class, name) is implementation
            assert signature(getattr(store, name)) == _bound_signature(implementation)
            names.add(name)
    return names


def test_memory_store_keeps_public_methods_as_direct_repository_bindings() -> None:
    store = MemoryStore(":memory:")
    expected = _assert_direct_bindings(MemoryStore, store, MEMORY_BINDINGS)
    public_callables = {
        name
        for name in dir(MemoryStore)
        if not name.startswith("_") and callable(getattr(MemoryStore, name))
    }
    assert public_callables == expected | {"init_db"}
    assert tuple(signature(MemoryStore).parameters) == ("database_path",)


def test_knowledge_store_keeps_public_methods_as_direct_repository_bindings() -> None:
    store = KnowledgeStore(":memory:", max_document_bytes=1024)
    expected = _assert_direct_bindings(KnowledgeStore, store, KNOWLEDGE_BINDINGS)
    public_callables = {
        name
        for name in dir(KnowledgeStore)
        if not name.startswith("_") and callable(getattr(KnowledgeStore, name))
    }
    assert public_callables == expected | {"init_db"}
    assert tuple(signature(KnowledgeStore).parameters) == (
        "database_path",
        "max_document_bytes",
    )


def test_repository_protocols_have_stable_domain_names() -> None:
    assert hasattr(memory_helpers, "ConnectionProvider")
    assert hasattr(memory_helpers, "MemoryLookupProvider")
    assert not hasattr(memory_helpers, "_ConnectableStore")
    assert hasattr(knowledge_helpers, "ConnectionProvider")
    assert hasattr(knowledge_helpers, "DocumentSizeProvider")
    assert hasattr(knowledge_helpers, "VersionIndexProvider")
    assert hasattr(knowledge_helpers, "KnowledgeWriteProvider")
    assert not hasattr(knowledge_helpers, "_ConnectableStore")
