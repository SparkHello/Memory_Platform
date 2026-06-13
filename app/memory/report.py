from app.memory.models import MemoryRecord, utc_now_iso
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
    deleted_memories = (
        store.list_archived_memories(user_id=user_id, limit=10000)
        if include_deleted
        else []
    )
    return {
        "version": 1,
        "exported_at": utc_now_iso(),
        "user_id": user_id,
        "embedding_included": False,
        "memories": [
            _memory_to_public_dict(memory)
            for memory in store.list_memories(user_id=user_id, limit=10000)
        ],
        "deleted_memories": [
            _memory_to_public_dict(memory) for memory in deleted_memories
        ],
        "core_memory_sections": [
            section.model_dump()
            for section in store.list_core_memory_sections(user_id=user_id)
        ],
        "core_memory_section_history": [
            item.model_dump()
            for item in store.list_core_memory_section_history(
                user_id=user_id,
                limit=10000,
            )
        ],
        "recent_context_summaries": [
            summary.model_dump()
            for summary in store.list_recent_context_summaries(
                user_id=user_id,
                limit=10000,
            )
        ],
        "decision_logs": [
            log.model_dump()
            for log in store.list_decision_logs(user_id=user_id, limit=10000)
        ],
    }


def restore_memory_export(
    *,
    store: MemoryStore,
    user_id: str,
    export_data: dict,
    overwrite: bool = False,
    include_deleted: bool = False,
) -> dict:
    active_records = export_data.get("memories", [])
    deleted_records = export_data.get("deleted_memories", []) if include_deleted else []
    result = {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "invalid": 0,
        "restored_memories": [],
        "include_deleted": include_deleted,
        "overwrite": overwrite,
    }

    for raw_memory in active_records:
        _restore_one_memory(
            store=store,
            user_id=user_id,
            raw_memory=raw_memory,
            overwrite=overwrite,
            archived=0,
            result=result,
        )
    for raw_memory in deleted_records:
        _restore_one_memory(
            store=store,
            user_id=user_id,
            raw_memory=raw_memory,
            overwrite=overwrite,
            archived=1,
            result=result,
        )
    return result


def format_memory_report(report: dict) -> str:
    counts = report["counts"]
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
                lines.append(_format_memory_bullet(memory))
        else:
            lines.append("No active memories in this section.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_memory_export(export_data: dict) -> str:
    report = _report_from_export(export_data)
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
            lines.append(_format_memory_bullet(memory))
    else:
        lines.append("No deleted memories exported.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
    return memory.model_dump(exclude={"embedding_json"})


def _restore_one_memory(
    *,
    store: MemoryStore,
    user_id: str,
    raw_memory: object,
    overwrite: bool,
    archived: int,
    result: dict,
) -> None:
    if not isinstance(raw_memory, dict):
        result["invalid"] += 1
        return
    action, memory = store.import_memory_record(
        user_id=user_id,
        data=raw_memory,
        overwrite=overwrite,
        archived=archived,
    )
    if action in {"created", "updated", "skipped", "invalid"}:
        result[action] += 1
    else:
        result["invalid"] += 1
    if memory is not None:
        result["restored_memories"].append(_memory_to_public_dict(memory))


def _format_memory_bullet(memory: dict) -> str:
    metadata = (
        f"type={memory['type']}, importance={memory['importance']}, "
        f"confidence={memory['confidence']:.2f}"
    )
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
    if memory_type == "preference":
        return "preferences"
    if memory_type in {"person", "relationship"}:
        return "relationships"
    if memory_type in {"project", "learning"}:
        return "goals"
    if memory_type == "style":
        return "communication"
    content = str(memory.get("content", "")).lower()
    if any(term in content for term in ("routine", "habit", "sleep", "wake")):
        return "routines"
    if memory_type == "fact":
        return "profile"
    return "other"
