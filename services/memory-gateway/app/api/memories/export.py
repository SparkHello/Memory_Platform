"""/memories routes: export."""
from __future__ import annotations

from app.api.memories.common import *  # noqa: F403

@router.get("/report", response_model=None)
def get_memory_report(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    response_format: Literal["json", "markdown"] = Query(default="json", alias="format"),
) -> dict | PlainTextResponse:
    report = build_memory_report(store=store, user_id=user_id)
    if response_format == "markdown":
        return PlainTextResponse(report["markdown"], media_type="text/markdown")
    return report

@router.get("/export", response_model=None)
def export_memories(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    include_deleted: bool = True,
    response_format: Literal["json", "markdown", "obsidian_markdown"] = Query(
        default="json",
        alias="format",
    ),
) -> dict | PlainTextResponse | Response:
    export_data = build_memory_export(
        store=store,
        user_id=user_id,
        include_deleted=include_deleted,
    )
    if response_format == "markdown":
        return PlainTextResponse(
            format_memory_export(export_data),
            media_type="text/markdown",
        )
    if response_format == "obsidian_markdown":
        filename = f"memory-obsidian-export-{_safe_download_filename_part(user_id)}.zip"
        return Response(
            build_obsidian_markdown_zip(export_data),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return export_data

@router.post("/export/selection")
def export_memory_selection(
    body: MemorySelectionExportRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    try:
        return build_memory_selection_export(
            store=store,
            user_id=user_id,
            memory_ids=body.memory_ids,
        )
    except MemorySelectionConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "memory_selection_stale",
                "message": "部分所选记忆已不存在或不属于当前用户，请刷新后重试。",
                "missing_memory_ids": exc.missing_memory_ids,
            },
        ) from exc

@router.post("/restore")
def restore_memories_from_export(
    body: MemoryRestoreExportRequest,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict:
    return restore_memory_export(
        store=store,
        user_id=user_id,
        export_data=body.data,
        overwrite=body.overwrite,
        include_deleted=body.include_deleted,
        dry_run=body.dry_run,
    )
