import io
import json
import re
import zipfile

from app.memory.models import MemoryRecord, RecentContextTurn, utc_now_iso
from app.memory.store import MemoryStore


REPORT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("profile", "Life background"),
    ("preferences", "Preferences"),
    ("relationships", "Relationships"),
    ("routines", "Routines"),
    ("goals", "Goals and plans"),
    ("communication", "Communication style"),
    ("other", "Other memories"),
)


def build_memory_report(
    *,
    store: MemoryStore,
    user_id: str,
    limit: int = 500,
) -> dict:
    memories = store.list_memories(user_id=user_id, limit=max(1, min(limit, 1000)))
    deleted_count = len(store.list_archived_memories(user_id=user_id, limit=10000))
    core_sections = store.list_core_memory_sections(user_id=user_id)
    core_by_section = {section.section: section for section in core_sections}
    memory_spaces = [space.model_dump() for space in store.list_memory_spaces(user_id=user_id)]

    grouped: dict[str, list[MemoryRecord]] = {
        section: [] for section, _ in REPORT_SECTIONS
    }
    for memory in memories:
        grouped[_report_section_for_memory(memory)].append(memory)

    sections: list[dict] = []
    for section, title in REPORT_SECTIONS:
        core = core_by_section.get(section)
        section_memories = grouped[section]
        sections.append(
            {
                "section": section,
                "title": title,
                "core_summary": core.content if core else "",
                "core_confidence": core.confidence if core else None,
                "core_version": core.version if core else None,
                "memories": [_memory_to_public_dict(memory) for memory in section_memories],
            }
        )

    payload = {
        "user_id": user_id,
        "generated_at": utc_now_iso(),
        "counts": {
            "active_memories": len(memories),
            "deleted_memories": deleted_count,
            "core_sections": len(core_sections),
        },
        "memory_spaces": memory_spaces,
        "sections": sections,
    }
    payload["markdown"] = format_memory_report(payload)
    return payload


def build_memory_export(
    *,
    store: MemoryStore,
    user_id: str,
    include_deleted: bool = True,
) -> dict:
    snapshot = store.read_memory_export_snapshot(
        user_id=user_id,
        include_deleted=include_deleted,
    )
    return {
        "version": 3,
        "exported_at": utc_now_iso(),
        "user_id": user_id,
        "embedding_included": False,
        "restore_contract": {
            "restored_sections": [
                "memory_spaces",
                "memories",
                "deleted_memories",
                "recent_context_summaries",
                "conversation_branch_nodes",
            ],
            "snapshot_only_sections": [
                "core_memory_sections",
                "core_memory_section_history",
                "decision_logs",
            ],
            "note": (
                "核心记忆可从长期记忆重新整理；历史版本和决策日志仅供审计，"
                "恢复时不会写回。"
            ),
        },
        "memory_spaces": [
            space.model_dump()
            for space in snapshot["memory_spaces"]
        ],
        "memories": [
            _memory_to_public_dict(memory)
            for memory in snapshot["memories"]
        ],
        "deleted_memories": [
            _memory_to_public_dict(memory) for memory in snapshot["deleted_memories"]
        ],
        "core_memory_sections": [
            section.model_dump()
            for section in snapshot["core_memory_sections"]
        ],
        "core_memory_section_history": [
            item.model_dump()
            for item in snapshot["core_memory_section_history"]
        ],
        "recent_context_summaries": [
            summary.model_dump()
            for summary in snapshot["recent_context_summaries"]
        ],
        "conversation_branch_nodes": [
            node.model_dump()
            for node in snapshot["conversation_branch_nodes"]
        ],
        "decision_logs": [
            log.model_dump()
            for log in snapshot["decision_logs"]
        ],
    }


class MemorySelectionConflict(ValueError):
    def __init__(self, missing_memory_ids: list[str]) -> None:
        super().__init__("selection contains missing or inaccessible memories")
        self.missing_memory_ids = missing_memory_ids


def build_memory_selection_export(
    *,
    store: MemoryStore,
    user_id: str,
    memory_ids: list[str],
) -> dict:
    """Build a restore-compatible export containing only an exact selection."""
    snapshot = store.read_memory_selection_export_snapshot(
        user_id=user_id,
        memory_ids=memory_ids,
    )
    missing_ids = [str(value) for value in snapshot["missing_memory_ids"]]
    if missing_ids:
        raise MemorySelectionConflict(missing_ids)

    selected_ids = {
        memory.id for memory in snapshot["memories"] if isinstance(memory, MemoryRecord)
    }
    active: list[dict] = []
    deleted: list[dict] = []
    sanitized_references = 0
    for raw_memory in snapshot["memories"]:
        if not isinstance(raw_memory, MemoryRecord):
            continue
        memory = _memory_to_public_dict(raw_memory)
        evidence_before = list(memory.get("evidence_memory_ids") or [])
        evidence_after = [
            memory_id for memory_id in evidence_before if memory_id in selected_ids
        ]
        sanitized_references += len(evidence_before) - len(evidence_after)
        memory["evidence_memory_ids"] = evidence_after
        for field in ("supersedes", "superseded_by"):
            reference = memory.get(field)
            if reference and reference not in selected_ids:
                memory[field] = None
                sanitized_references += 1
        (deleted if raw_memory.archived else active).append(memory)

    return {
        "version": 3,
        "exported_at": utc_now_iso(),
        "user_id": user_id,
        "embedding_included": False,
        "restore_contract": {
            "restored_sections": ["memory_spaces", "memories", "deleted_memories"],
            "snapshot_only_sections": [],
            "note": "仅包含用户明确选择的记忆和必要空间元数据。",
        },
        "selection_contract": {
            "requested_count": len(memory_ids),
            "exported_count": len(active) + len(deleted),
            "sanitized_reference_count": sanitized_references,
        },
        "memory_spaces": [space.model_dump() for space in snapshot["memory_spaces"]],
        "memories": active,
        "deleted_memories": deleted,
        "core_memory_sections": [],
        "core_memory_section_history": [],
        "recent_context_summaries": [],
        "conversation_branch_nodes": [],
        "decision_logs": [],
    }


def restore_memory_export(
    *,
    store: MemoryStore,
    user_id: str,
    export_data: dict,
    overwrite: bool = False,
    include_deleted: bool = False,
    dry_run: bool = False,
) -> dict:
    active_records = (
        export_data.get("memories", [])
        if isinstance(export_data.get("memories", []), list)
        else []
    )
    deleted_value = export_data.get("deleted_memories", []) if include_deleted else []
    deleted_records = deleted_value if isinstance(deleted_value, list) else []
    space_records = export_data.get("memory_spaces", [])
    recent_context_records = export_data.get("recent_context_summaries", [])
    branch_node_records = export_data.get("conversation_branch_nodes", [])
    snapshot_only_sections = [
        name
        for name in (
            "core_memory_sections",
            "core_memory_section_history",
            "decision_logs",
        )
        if export_data.get(name)
    ]
    result = {
        "spaces_created": 0,
        "spaces_updated": 0,
        "spaces_skipped": 0,
        "spaces_invalid": 0,
        "recent_context_created": 0,
        "recent_context_updated": 0,
        "recent_context_skipped": 0,
        "recent_context_invalid": 0,
        "branch_nodes_created": 0,
        "branch_nodes_updated": 0,
        "branch_nodes_skipped": 0,
        "branch_nodes_invalid": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "invalid": 0,
        "restored_memories": [],
        "include_deleted": include_deleted,
        "overwrite": overwrite,
        "dry_run": dry_run,
        "not_restored_sections": snapshot_only_sections,
        "warnings": (
            [
                "以下导出分区仅供审计，未写回："
                + ", ".join(snapshot_only_sections)
                + "。核心记忆应在恢复长期记忆后重新整理。"
            ]
            if snapshot_only_sections
            else []
        ),
    }

    prepared_spaces: list[dict[str, object]] = []
    if isinstance(space_records, list):
        for raw_space in space_records:
            if not isinstance(raw_space, dict):
                result["spaces_invalid"] += 1
                continue
            prepared_space = store.prepare_memory_space_import(data=raw_space)
            if prepared_space is None:
                result["spaces_invalid"] += 1
                continue
            prepared_spaces.append(prepared_space)

    all_memory_records = [*(_dict_list(active_records)), *(_dict_list(deleted_records))]
    source_memory_ids = [
        str(raw_memory.get("id")).strip()
        for raw_memory in all_memory_records
        if raw_memory.get("id") is not None and str(raw_memory.get("id")).strip()
    ]
    prepared_memories: list[tuple[str, MemoryRecord]] = []
    for records, archived in ((active_records, 0), (deleted_records, 1)):
        for raw_memory in records:
            if not isinstance(raw_memory, dict):
                result["invalid"] += 1
                continue
            memory = store.prepare_memory_import_record(
                user_id=user_id,
                data=raw_memory,
                archived=archived,
            )
            if memory is None:
                result["invalid"] += 1
                continue
            source_id = str(raw_memory.get("id") or "").strip() or memory.id
            if source_id not in source_memory_ids:
                source_memory_ids.append(source_id)
            prepared_memories.append((source_id, memory))

    prepared_recent_contexts: list[dict[str, object]] = []
    if isinstance(recent_context_records, list):
        for raw_summary in recent_context_records:
            prepared = _prepare_recent_context_summary(raw_summary, result=result)
            if prepared is not None:
                prepared_recent_contexts.append(prepared)

    prepared_branch_nodes: list[dict[str, object]] = []
    if isinstance(branch_node_records, list):
        for raw_node in branch_node_records:
            prepared = _prepare_conversation_branch_node(raw_node, result=result)
            if prepared is not None:
                prepared_branch_nodes.append(prepared)

    exported_user_id = str(export_data.get("user_id") or "").strip()
    applied = store.restore_prepared_export(
        user_id=user_id,
        prepared_spaces=prepared_spaces,
        prepared_memories=prepared_memories,
        source_memory_ids=source_memory_ids,
        referenced_source_ids=_memory_reference_ids(all_memory_records),
        recent_contexts=prepared_recent_contexts,
        branch_nodes=prepared_branch_nodes,
        exported_user_id=exported_user_id,
        overwrite=overwrite,
        dry_run=dry_run,
    )

    for action, _space in applied["space_results"]:
        key = f"spaces_{action}"
        result[key if key in result else "spaces_invalid"] += 1

    for action, memory in applied["memory_results"]:
        result[action if action in {"created", "updated", "skipped", "invalid"} else "invalid"] += 1
        if isinstance(memory, MemoryRecord):
            result["restored_memories"].append(_memory_to_public_dict(memory))

    result["dangling_references_removed"] = int(
        applied["dangling_references_removed"]
    )
    final_existing_ids = set(applied["final_existing_ids"])
    for restored_memory in result["restored_memories"]:
        restored_memory["evidence_memory_ids"] = [
            memory_id
            for memory_id in _string_list(restored_memory.get("evidence_memory_ids"))
            if memory_id in final_existing_ids
        ]
        for field_name in ("supersedes", "superseded_by"):
            reference = restored_memory.get(field_name)
            if reference not in final_existing_ids:
                restored_memory[field_name] = None

    for action in applied["recent_context_actions"]:
        key = f"recent_context_{action}"
        result[key if key in result else "recent_context_invalid"] += 1
    for action in applied["branch_node_actions"]:
        key = f"branch_nodes_{action}"
        result[key if key in result else "branch_nodes_invalid"] += 1
    return result


def format_memory_report(report: dict) -> str:
    counts = report["counts"]
    space_names_by_id = _space_names_by_id(report)
    lines = [
        "# Memory Report",
        "",
        f"- User: {report['user_id']}",
        f"- Generated at: {report['generated_at']}",
        f"- Active memories: {counts['active_memories']}",
        f"- Deleted memories: {counts['deleted_memories']}",
        f"- Core sections: {counts['core_sections']}",
        "",
    ]
    for section in report["sections"]:
        lines.extend([f"## {section['title']}", ""])
        if section["core_summary"]:
            lines.extend(
                [
                    "Core summary:",
                    section["core_summary"],
                    "",
                ]
            )
        if section["memories"]:
            lines.append("Memories:")
            for memory in section["memories"]:
                lines.append(_format_memory_bullet(memory, space_names_by_id=space_names_by_id))
        else:
            lines.append("No active memories in this section.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_memory_export(export_data: dict) -> str:
    report = _report_from_export(export_data)
    space_names_by_id = _space_names_by_id(export_data)
    lines = [
        "# Memory Export",
        "",
        f"- User: {export_data['user_id']}",
        f"- Exported at: {export_data['exported_at']}",
        f"- Version: {export_data['version']}",
        f"- Embeddings included: {str(export_data['embedding_included']).lower()}",
        "",
        report,
        "## Deleted memories",
        "",
    ]
    deleted = export_data.get("deleted_memories", [])
    if deleted:
        for memory in deleted:
            lines.append(_format_memory_bullet(memory, space_names_by_id=space_names_by_id))
    else:
        lines.append("No deleted memories exported.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_obsidian_markdown_zip(export_data: dict) -> bytes:
    """Build a one-way Obsidian-friendly Markdown mirror as a zip archive."""
    memories = _dict_list(export_data.get("memories"))
    deleted_memories = _dict_list(export_data.get("deleted_memories"))
    core_sections = _dict_list(export_data.get("core_memory_sections"))
    spaces = _dict_list(export_data.get("memory_spaces"))
    space_names_by_id = _space_names_by_id(export_data)

    used_paths: set[str] = set()
    memory_paths: dict[str, str] = {}
    memory_note_links: dict[str, str] = {}
    by_type: dict[str, list[dict]] = {}
    by_space: dict[str, list[dict]] = {str(space["id"]): [] for space in spaces if space.get("id")}
    unassigned: list[dict] = []

    files: dict[str, str] = {}
    for memory in memories:
        memory_id = str(memory.get("id") or "")
        if not memory_id:
            continue
        memory_type = str(memory.get("type") or "memory")
        filename = f"{_safe_path_segment(memory_type, fallback='memory')}-{_short_id(memory_id)}.md"
        path = _unique_path(f"Memories/notes/{filename}", used_paths)
        memory_paths[memory_id] = path
        memory_note_links[memory_id] = _obsidian_link(path)
        files[path] = _format_obsidian_memory_note(memory, space_names_by_id=space_names_by_id)

        by_type.setdefault(memory_type, []).append(memory)
        memory_space_ids = [str(space_id) for space_id in memory.get("space_ids", [])]
        if memory_space_ids:
            for space_id in memory_space_ids:
                by_space.setdefault(space_id, []).append(memory)
        else:
            unassigned.append(memory)

    for memory_type, section_memories in sorted(by_type.items()):
        path = _unique_path(
            f"Memories/by-type/{_safe_path_segment(memory_type, fallback='memory')}.md",
            used_paths,
        )
        files[path] = _format_memory_index(
            title=f"Memories by type: {memory_type}",
            memories=section_memories,
            memory_paths=memory_paths,
        )

    space_paths: dict[str, str] = {}
    for space in spaces:
        space_id = str(space.get("id") or "")
        if not space_id:
            continue
        space_name = str(space.get("name") or space_id)
        path = _unique_path(
            f"Memories/by-space/{_safe_path_segment(space_name, fallback='space')}.md",
            used_paths,
        )
        space_paths[space_id] = path
        files[path] = _format_memory_index(
            title=f"Memories by space: {space_name}",
            memories=by_space.get(space_id, []),
            memory_paths=memory_paths,
        )
    if unassigned:
        files[_unique_path("Memories/by-space/unassigned.md", used_paths)] = _format_memory_index(
            title="Memories by space: Unassigned",
            memories=unassigned,
            memory_paths=memory_paths,
        )

    for section in core_sections:
        section_name = str(section.get("section") or "core-memory")
        path = _unique_path(
            f"Core Memory/{_safe_path_segment(section_name, fallback='core-memory')}.md",
            used_paths,
        )
        files[path] = _format_core_memory_note(section, memory_note_links=memory_note_links)

    files[_unique_path("Review/review-due.md", used_paths)] = _format_review_due_index(
        memories=memories,
        memory_paths=memory_paths,
    )
    files[_unique_path("Review/deleted-memories.md", used_paths)] = _format_deleted_memory_index(
        memories=deleted_memories,
    )
    files[_unique_path("Reports/memory-report.md", used_paths)] = format_memory_export(export_data)
    files[_unique_path("Reports/export-summary.md", used_paths)] = _format_obsidian_export_summary(
        export_data=export_data,
        memory_count=len(memories),
        deleted_count=len(deleted_memories),
        core_count=len(core_sections),
        space_count=len(spaces),
        space_paths=space_paths,
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.writestr(path, files[path])
    return buffer.getvalue()


def _report_from_export(export_data: dict) -> str:
    grouped: dict[str, list[dict]] = {section: [] for section, _ in REPORT_SECTIONS}
    for memory in export_data.get("memories", []):
        grouped[_report_section_for_memory_dict(memory)].append(memory)

    report = {
        "user_id": export_data["user_id"],
        "generated_at": export_data["exported_at"],
        "counts": {
            "active_memories": len(export_data.get("memories", [])),
            "deleted_memories": len(export_data.get("deleted_memories", [])),
            "core_sections": len(export_data.get("core_memory_sections", [])),
        },
        "memory_spaces": export_data.get("memory_spaces", []),
        "sections": [],
    }
    core_by_section = {
        section["section"]: section
        for section in export_data.get("core_memory_sections", [])
    }
    for section, title in REPORT_SECTIONS:
        core = core_by_section.get(section)
        report["sections"].append(
            {
                "section": section,
                "title": title,
                "core_summary": core.get("content", "") if core else "",
                "core_confidence": core.get("confidence") if core else None,
                "core_version": core.get("version") if core else None,
                "memories": grouped[section],
            }
        )
    return format_memory_report(report)


def _memory_to_public_dict(memory: MemoryRecord) -> dict:
    return memory.model_dump(exclude={"embedding_json", "embedding_space_id"})


def _memory_reference_ids(records: list[dict]) -> list[str]:
    references: list[str] = []
    for record in records:
        references.extend(_string_list(record.get("evidence_memory_ids")))
        for field_name in ("supersedes", "superseded_by"):
            raw_reference = record.get(field_name)
            reference = str(raw_reference).strip() if raw_reference is not None else ""
            if reference:
                references.append(reference)
    return list(dict.fromkeys(references))


def _prepare_recent_context_summary(
    raw_summary: object,
    *,
    result: dict,
) -> dict[str, object] | None:
    if not isinstance(raw_summary, dict):
        result["recent_context_invalid"] += 1
        return None
    summary_text = str(raw_summary.get("summary") or "").strip()
    compressed_summary = str(
        raw_summary.get("compressed_summary") or summary_text
    ).strip()
    raw_turns = raw_summary.get("recent_turns")
    recent_turns: list[RecentContextTurn] = []
    if isinstance(raw_turns, list):
        for raw_turn in raw_turns:
            try:
                recent_turns.append(RecentContextTurn.model_validate(raw_turn))
            except (TypeError, ValueError):
                result["recent_context_invalid"] += 1
                return None
    if not summary_text and not compressed_summary and not recent_turns:
        result["recent_context_invalid"] += 1
        return None
    try:
        archived = int(raw_summary.get("archived") or 0)
    except (TypeError, ValueError):
        result["recent_context_invalid"] += 1
        return None
    if archived != 0:
        result["recent_context_skipped"] += 1
        return None
    raw_conversation_id = raw_summary.get("conversation_id")
    conversation_id = (
        str(raw_conversation_id).strip()
        if raw_conversation_id is not None and str(raw_conversation_id).strip()
        else None
    )
    try:
        turn_count = max(
            len(recent_turns),
            int(raw_summary.get("turn_count") or 0),
        )
    except (TypeError, ValueError):
        result["recent_context_invalid"] += 1
        return None
    return {
        "conversation_id": conversation_id,
        "summary": summary_text,
        "compressed_summary": compressed_summary,
        "recent_turns": recent_turns,
        "turn_count": turn_count,
    }


def _prepare_conversation_branch_node(
    raw_node: object,
    *,
    result: dict,
) -> dict[str, object] | None:
    if not isinstance(raw_node, dict):
        result["branch_nodes_invalid"] += 1
        return None
    try:
        archived = int(raw_node.get("archived") or 0)
    except (TypeError, ValueError):
        result["branch_nodes_invalid"] += 1
        return None
    if archived != 0:
        result["branch_nodes_invalid"] += 1
        return None
    fingerprint_fields = (
        "history_fingerprint",
        "turn_fingerprint",
        "assistant_digest",
    )
    fingerprints = {
        name: str(raw_node.get(name) or "").strip()
        for name in fingerprint_fields
    }
    parent_fingerprint = str(
        raw_node.get("parent_history_fingerprint") or ""
    ).strip()
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in fingerprints.values()
    ):
        result["branch_nodes_invalid"] += 1
        return None
    if parent_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", parent_fingerprint):
        result["branch_nodes_invalid"] += 1
        return None
    raw_turns = raw_node.get("recent_turns")
    turns: list[RecentContextTurn] = []
    if isinstance(raw_turns, list):
        try:
            turns = [RecentContextTurn.model_validate(turn) for turn in raw_turns]
        except (TypeError, ValueError):
            result["branch_nodes_invalid"] += 1
            return None
    try:
        turn_count = max(len(turns), int(raw_node.get("turn_count") or 0))
    except (TypeError, ValueError):
        result["branch_nodes_invalid"] += 1
        return None
    raw_conversation_id = raw_node.get("conversation_id")
    conversation_id = (
        str(raw_conversation_id).strip()
        if raw_conversation_id is not None and str(raw_conversation_id).strip()
        else None
    )
    return {
        "conversation_id": conversation_id,
        "history_fingerprint": fingerprints["history_fingerprint"],
        "parent_history_fingerprint": parent_fingerprint,
        "turn_fingerprint": fingerprints["turn_fingerprint"],
        "assistant_digest": fingerprints["assistant_digest"],
        "summary": str(raw_node.get("summary") or ""),
        "compressed_summary": str(raw_node.get("compressed_summary") or ""),
        "recent_turns": turns,
        "turn_count": turn_count,
    }


def _format_memory_bullet(
    memory: dict,
    *,
    space_names_by_id: dict[str, str] | None = None,
) -> str:
    space_names_by_id = space_names_by_id or {}
    metadata = (
        f"type={memory['type']}, importance={memory['importance']}, "
        f"confidence={memory['confidence']:.2f}"
    )
    if memory.get("topics"):
        metadata += f", topics={','.join(memory['topics'])}"
    if memory.get("entities"):
        metadata += f", entities={','.join(memory['entities'])}"
    space_names = [
        space_names_by_id.get(str(space_id), str(space_id))
        for space_id in memory.get("space_ids", [])
    ]
    if space_names:
        metadata += f", spaces={','.join(space_names)}"
    if memory.get("stability"):
        metadata += f", stability={memory['stability']}"
    if memory.get("valid_until"):
        metadata += f", valid_until={memory['valid_until']}"
    if memory.get("archived_at"):
        metadata += f", archived_at={memory['archived_at']}"
    return f"- {memory['content']} ({metadata}; id={memory['id']})"


def _report_section_for_memory(memory: MemoryRecord) -> str:
    return _report_section_for_memory_dict(memory.model_dump())


def _report_section_for_memory_dict(memory: dict) -> str:
    memory_type = memory.get("type")
    if memory_type == "emotional":
        return "preferences"
    if memory_type == "procedural":
        return "routines"
    if memory_type == "reflective":
        return "communication"
    content = str(memory.get("content", "")).lower()
    if any(term in content for term in ("routine", "habit", "sleep", "wake")):
        return "routines"
    if memory_type in {"semantic", "episodic"}:
        return "profile"
    return "other"


def _space_names_by_id(payload: dict) -> dict[str, str]:
    spaces = payload.get("memory_spaces", [])
    if not isinstance(spaces, list):
        return {}
    return {
        str(space["id"]): str(space.get("name") or space["id"])
        for space in spaces
        if isinstance(space, dict) and space.get("id")
    }


def _dict_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _short_id(value: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]+", "", value)
    return (compact or "memory")[:8]


def _safe_path_segment(value: str, *, fallback: str, max_length: int = 80) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip(" .-")
    if not normalized:
        normalized = fallback
    return normalized[:max_length].rstrip(" .-") or fallback


def _unique_path(path: str, used_paths: set[str]) -> str:
    if path not in used_paths:
        used_paths.add(path)
        return path
    stem, dot, suffix = path.rpartition(".")
    base = stem if dot else path
    extension = f".{suffix}" if dot else ""
    index = 2
    while True:
        candidate = f"{base}-{index}{extension}"
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
        index += 1


def _obsidian_link(path: str) -> str:
    target = path.removesuffix(".md")
    label = target.rsplit("/", 1)[-1]
    return f"[[{target}|{label}]]"


def _format_obsidian_memory_note(memory: dict, *, space_names_by_id: dict[str, str]) -> str:
    memory_type = str(memory.get("type") or "memory")
    memory_id = str(memory.get("id") or "")
    space_ids = [str(space_id) for space_id in memory.get("space_ids", [])]
    frontmatter = {
        "id": memory_id,
        "type": memory_type,
        "importance": memory.get("importance"),
        "confidence": memory.get("confidence"),
        "stability": memory.get("stability"),
        "sensitivity": memory.get("sensitivity"),
        "valence": memory.get("valence"),
        "arousal": memory.get("arousal"),
        "topics": _string_list(memory.get("topics")),
        "entities": _string_list(memory.get("entities")),
        "space_ids": space_ids,
        "spaces": [space_names_by_id.get(space_id, space_id) for space_id in space_ids],
        "review_after": memory.get("review_after"),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
    }
    lines = [
        _yaml_frontmatter(frontmatter),
        f"# {memory_type} memory {_short_id(memory_id)}",
        "",
        str(memory.get("content") or "").strip(),
        "",
        "## Details",
        "",
        f"- ID: `{memory_id}`",
        f"- Type: {memory_type}",
        f"- Importance: {memory.get('importance')}",
        f"- Confidence: {memory.get('confidence')}",
        f"- Stability: {memory.get('stability')}",
        f"- Sensitivity: {memory.get('sensitivity')}",
        f"- Valence: {memory.get('valence')}",
        f"- Arousal: {memory.get('arousal')}",
        f"- Topics: {', '.join(_string_list(memory.get('topics'))) or '-'}",
        f"- Entities: {', '.join(_string_list(memory.get('entities'))) or '-'}",
        f"- Spaces: {', '.join(frontmatter['spaces']) or '-'}",
        f"- Review after: {memory.get('review_after') or '-'}",
        f"- Created at: {memory.get('created_at') or '-'}",
        f"- Updated at: {memory.get('updated_at') or '-'}",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _format_memory_index(*, title: str, memories: list[dict], memory_paths: dict[str, str]) -> str:
    lines = [f"# {title}", ""]
    if not memories:
        lines.append("No active memories in this view.")
        return "\n".join(lines).rstrip() + "\n"
    for memory in memories:
        memory_id = str(memory.get("id") or "")
        path = memory_paths.get(memory_id)
        if not path:
            continue
        lines.append(f"- {_obsidian_link(path)} - {_summary(memory.get('content'))}")
    return "\n".join(lines).rstrip() + "\n"


def _format_core_memory_note(section: dict, *, memory_note_links: dict[str, str]) -> str:
    section_name = str(section.get("section") or "core-memory")
    evidence_ids = [str(memory_id) for memory_id in section.get("evidence_memory_ids", [])]
    lines = [
        f"# Core Memory: {section_name}",
        "",
        str(section.get("content") or "").strip() or "No core memory content.",
        "",
        "## Metadata",
        "",
        f"- Confidence: {section.get('confidence')}",
        f"- Version: {section.get('version')}",
        f"- Updated at: {section.get('updated_at') or '-'}",
        "",
        "## Evidence",
        "",
    ]
    if evidence_ids:
        for memory_id in evidence_ids:
            lines.append(f"- {memory_note_links.get(memory_id, f'`{memory_id}`')}")
    else:
        lines.append("No evidence memories recorded.")
    return "\n".join(lines).rstrip() + "\n"


def _format_review_due_index(*, memories: list[dict], memory_paths: dict[str, str]) -> str:
    review_memories = [memory for memory in memories if memory.get("review_after")]
    review_memories.sort(key=lambda memory: str(memory.get("review_after") or ""))
    lines = ["# Review Due", ""]
    if not review_memories:
        lines.append("No active memories have a review_after value.")
        return "\n".join(lines).rstrip() + "\n"
    for memory in review_memories:
        memory_id = str(memory.get("id") or "")
        path = memory_paths.get(memory_id)
        if not path:
            continue
        lines.append(
            f"- {memory.get('review_after')}: {_obsidian_link(path)} - {_summary(memory.get('content'))}"
        )
    return "\n".join(lines).rstrip() + "\n"


def _format_deleted_memory_index(*, memories: list[dict]) -> str:
    lines = ["# Deleted Memories", ""]
    if not memories:
        lines.append("No deleted memories exported.")
        return "\n".join(lines).rstrip() + "\n"
    for memory in memories:
        metadata = [
            f"type={memory.get('type')}",
            f"importance={memory.get('importance')}",
            f"archived_at={memory.get('archived_at') or '-'}",
            f"id={memory.get('id')}",
        ]
        lines.append(f"- {_summary(memory.get('content'), limit=220)} ({'; '.join(metadata)})")
    return "\n".join(lines).rstrip() + "\n"


def _format_obsidian_export_summary(
    *,
    export_data: dict,
    memory_count: int,
    deleted_count: int,
    core_count: int,
    space_count: int,
    space_paths: dict[str, str],
) -> str:
    lines = [
        "# Obsidian Export Summary",
        "",
        f"- User: {export_data.get('user_id')}",
        f"- Exported at: {export_data.get('exported_at')}",
        f"- Active memories: {memory_count}",
        f"- Deleted memories included: {deleted_count}",
        f"- Core memory sections: {core_count}",
        f"- Memory spaces: {space_count}",
        "",
        "## Structure",
        "",
        "- `Memories/notes/` contains one full Markdown note per active memory.",
        "- `Memories/by-type/` and `Memories/by-space/` contain Obsidian index links.",
        "- `Core Memory/` contains current core-memory sections.",
        "- `Review/` contains review and deleted-memory indexes.",
        "- `Reports/` contains this summary and a Markdown report.",
        "",
        "## Space Indexes",
        "",
    ]
    if space_paths:
        for space_id, path in sorted(space_paths.items(), key=lambda item: item[1]):
            lines.append(f"- {_obsidian_link(path)} (`{space_id}`)")
    else:
        lines.append("No memory spaces exported.")
    return "\n".join(lines).rstrip() + "\n"


def _yaml_frontmatter(values: dict[str, object]) -> str:
    lines = ["---"]
    for key, value in values.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {_yaml_scalar(item)}")
            else:
                lines.append(f"{key}: []")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _summary(value: object, *, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}..."
