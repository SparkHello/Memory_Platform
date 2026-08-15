"""/memories routes: export."""
from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
import os
from pathlib import Path
import tempfile
from typing import Annotated, Literal
import zipfile

import anyio.to_thread
import httpx
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse, Response
from starlette.background import BackgroundTask

from app.api.deps import get_memory_store, get_user_id
from app.api.memories.common import (
    MemoryRestoreExportRequest,
    MemorySelectionExportRequest,
    _safe_download_filename_part,
)
from app.cli_config import cli_paths, default_model_gateway_home
from app.config import Settings, get_settings
from app.llm.runtime import ModelRuntimeConfigurationError, resolve_model_runtime
from app.memory.report import (
    MemorySelectionConflict,
    build_memory_export,
    build_memory_report,
    build_memory_selection_export,
    build_obsidian_markdown_zip,
    format_memory_export,
    restore_memory_export,
)
from app.memory.store import MemoryStore
from app.stack_backup import (
    create_stack_backup,
    validate_stack_backup,
)


router = APIRouter()

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


@router.post("/stack-backup", response_model=None)
async def download_stack_backup(
    settings: Annotated[Settings, Depends(get_settings)],
    model_gateway_admin_key: Annotated[
        str | None,
        Header(
            alias="X-Model-Gateway-Admin-Key",
            description="当本机读不到 Model Gateway 数据目录时，用 admin 密钥拉取脱敏配置",
        ),
    ] = None,
) -> Response:
    """Download a portable stack zip (secrets excluded).

    The archive contains the complete databases of **every user** on this
    deployment, not only the requesting identity: stack backup is a whole
    instance migration tool, not a per-user export.

    Source installs usually have ``MODEL_GATEWAY_HOME`` on disk. Split Docker
    Memory containers do not mount Model volumes; pass the Model admin key so
    the gateway can fetch ``/admin/portable-config`` over the private network.
    """
    paths = cli_paths(os.environ.get("MEMGW_HOME", "").strip())
    memory_db = Path(settings.database_path).expanduser()
    knowledge_db = Path(settings.knowledge_database_path).expanduser()
    auth_db = Path(settings.auth_database_path).expanduser()

    model_home = Path(
        os.environ.get("MODEL_GATEWAY_HOME", "").strip() or default_model_gateway_home()
    ).expanduser()
    local_config = model_home / "config.json"
    model_config_override: Path | None = None
    override_cleanup: Path | None = None
    try:
        if local_config.is_file():
            model_gateway_home = model_home
        else:
            model_gateway_home = None
            admin_key = (model_gateway_admin_key or "").strip()
            if not admin_key:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "model_gateway_home_unavailable",
                        "message": (
                            "本进程读不到 Model Gateway 数据目录。"
                            "请在请求头提供 X-Model-Gateway-Admin-Key，"
                            "或在源码/同机部署中设置 MODEL_GATEWAY_HOME；"
                            "Docker 也可用 maintenance 配置文件执行 memgw stack backup。"
                        ),
                    },
                )
            model_config_override = await _fetch_model_portable_config(
                settings=settings,
                admin_key=admin_key,
            )
            override_cleanup = model_config_override

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = (
            Path(tempfile.gettempdir())
            / f"memory-stack-backup-{stamp}-{os.getpid()}.zip"
        )
        try:
            # Snapshotting and zipping the databases is blocking, potentially
            # multi-second work; keep the event loop free for other requests.
            await anyio.to_thread.run_sync(
                partial(
                    create_stack_backup,
                    destination=destination,
                    paths=paths,
                    memory_database=memory_db,
                    knowledge_database=knowledge_db,
                    auth_database=auth_db,
                    model_gateway_home=model_gateway_home,
                    model_config_override=model_config_override,
                    force=True,
                )
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "stack_backup_failed", "message": str(exc)},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail={
                    "code": "stack_backup_storage",
                    "message": f"无法写入备份：{type(exc).__name__}",
                },
            ) from exc

        filename = f"memory-stack-backup-{stamp}.zip"
        # Stream from disk instead of buffering the whole archive in memory;
        # delete the temporary file only after the response has been sent.
        return FileResponse(
            destination,
            media_type="application/zip",
            filename=filename,
            headers={
                "Cache-Control": "no-store",
                "X-Backup-Scope": "all-users",
            },
            background=BackgroundTask(destination.unlink, missing_ok=True),
        )
    finally:
        if override_cleanup is not None:
            override_cleanup.unlink(missing_ok=True)


@router.post("/stack-backup/validate")
async def validate_uploaded_stack_backup(
    file: UploadFile = File(..., description="便携整栈备份 zip"),
) -> dict:
    """Dry-run validate a portable stack backup without writing production data.

    Console-only (via /memories auth). Does not restore or stop services.
    """
    suffix = Path(file.filename or "backup.zip").suffix.lower()
    if suffix and suffix != ".zip":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "stack_backup_invalid_type",
                "message": "请上传 .zip 格式的整栈便携备份",
            },
        )

    handle = tempfile.NamedTemporaryFile(
        prefix="stack-backup-validate-",
        suffix=".zip",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        try:
            return await anyio.to_thread.run_sync(
                partial(validate_stack_backup, archive_path=temp_path)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "stack_backup_invalid",
                    "message": str(exc),
                    "ok": False,
                    "restorable": False,
                },
            ) from exc
        except zipfile.BadZipFile as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "stack_backup_invalid",
                    "message": "不是有效的 zip 备份文件",
                    "ok": False,
                    "restorable": False,
                },
            ) from exc
    finally:
        temp_path.unlink(missing_ok=True)
        await file.close()


async def _fetch_model_portable_config(
    *,
    settings: Settings,
    admin_key: str,
) -> Path:
    try:
        runtime = resolve_model_runtime(settings)
    except ModelRuntimeConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "model_runtime_unavailable", "message": str(exc)},
        ) from exc
    if not runtime.is_central:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "model_runtime_unavailable",
                "message": "当前未启用中央 Model Gateway，无法远程拉取配置",
            },
        )
    base = runtime.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.get(
                f"{base}/admin/portable-config",
                headers={
                    "Authorization": f"Bearer {admin_key}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "model_gateway_unreachable",
                "message": f"无法连接 Model Gateway：{type(exc).__name__}",
            },
        ) from exc
    if response.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "model_admin_unauthorized",
                "message": "Model Gateway admin 密钥无效",
            },
        )
    if response.status_code == 403:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "model_admin_forbidden",
                "message": "该密钥不是 Model Gateway admin 客户端",
            },
        )
    if not response.is_success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "model_portable_config_failed",
                "message": f"拉取 Model 配置失败（HTTP {response.status_code}）",
            },
        )
    try:
        # Validate schema before packaging.
        from model_gateway.models import GatewayConfig

        GatewayConfig.model_validate_json(response.content)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "model_portable_config_invalid",
                "message": "Model 返回的配置无法通过 schema 校验",
            },
        ) from exc
    handle, name = tempfile.mkstemp(prefix="memgw-model-config-", suffix=".json")
    os.close(handle)
    path = Path(name)
    path.write_bytes(response.content)
    os.chmod(path, 0o600)
    return path
