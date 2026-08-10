from __future__ import annotations

from dataclasses import dataclass
import errno
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings


MIB = 1024 * 1024
_SMALL_VOLUME_BYTES = 1024 * MIB
_MIN_ADAPTIVE_RESERVE_BYTES = 64 * 1024
_READ_ONLY_MEMORY_POSTS = frozenset(
    {
        "/memories/export/selection",
        "/memories/surface",
        "/memories/network",
        "/memories/network/traverse",
        "/memories/review",
        "/memories/deleted/purge/preview",
    }
)
_USAGE_ONLY_MEMORY_POSTS = frozenset(
    {
        "/memories/review/revise/preview",
        "/memories/review/revise/related",
        "/memories/context",
        "/memories/context/explain",
    }
)


class DiskCapacityError(RuntimeError):
    """A write cannot safely proceed without consuming the disk reserve."""


@dataclass(frozen=True)
class DiskCapacity:
    total_bytes: int
    free_bytes: int
    soft_reserve_bytes: int
    hard_reserve_bytes: int


class DiskCapacityMiddleware:
    """Convert storage exhaustion from any inner ASGI layer into stable 507.

    This middleware is intentionally outermost. It therefore also catches an
    ENOSPC raised while a chunked request is being spooled before routing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response_started = False

        async def tracked_send(message: dict) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as exc:
            if response_started or not is_storage_exhausted(exc):
                raise
            await send_insufficient_storage(
                send,
                openai_compatible=str(scope.get("path") or "").startswith("/v1/"),
            )


def configured_storage_paths(settings: Settings) -> tuple[tuple[str, str], ...]:
    """Return logical storage domains, even when domains share one SQLite file."""

    return (
        ("memory", settings.database_path),
        ("knowledge", settings.knowledge_database_path),
        ("auth", settings.auth_database_path),
        ("usage", settings.database_path),
    )


def disk_capacity_for_path(path: str, settings: Settings) -> DiskCapacity:
    usage = shutil.disk_usage(_existing_disk_probe(Path(path).expanduser()))
    return DiskCapacity(
        total_bytes=int(usage.total),
        free_bytes=int(usage.free),
        soft_reserve_bytes=_effective_reserve_bytes(
            configured=settings.disk_soft_reserve_bytes,
            total=int(usage.total),
            small_volume_divisor=16,
        ),
        hard_reserve_bytes=_effective_reserve_bytes(
            configured=settings.disk_hard_reserve_bytes,
            total=int(usage.total),
            small_volume_divisor=64,
        ),
    )


def disk_readiness_code(settings: Settings) -> str:
    """Return only a safe reason code; never include a host path or byte count."""

    checked: set[str] = set()
    try:
        for _, raw_path in configured_storage_paths(settings):
            probe = _existing_disk_probe(Path(raw_path).expanduser())
            key = str(probe.resolve())
            if key in checked:
                continue
            checked.add(key)
            capacity = disk_capacity_for_path(raw_path, settings)
            if capacity.free_bytes < capacity.soft_reserve_bytes:
                return "disk_low"
    except OSError:
        return "disk_unavailable"
    return ""


def ensure_request_write_capacity(
    settings: Settings,
    *,
    method: str,
    path: str,
    body_bytes: int,
    route_class: str,
) -> None:
    domains = request_write_domains(method=method, path=path)
    if not domains:
        return
    expected_bytes = _estimated_write_bytes(
        body_bytes=max(0, int(body_bytes)),
        route_class=route_class,
    )
    ensure_write_capacity(
        settings,
        domains=domains,
        expected_write_bytes=expected_bytes,
    )


def ensure_write_capacity(
    settings: Settings,
    *,
    domains: Iterable[str],
    expected_write_bytes: int = 0,
) -> None:
    wanted = set(domains)
    checked: set[str] = set()
    try:
        for name, raw_path in configured_storage_paths(settings):
            if name not in wanted:
                continue
            probe = _existing_disk_probe(Path(raw_path).expanduser())
            key = str(probe.resolve())
            if key in checked:
                continue
            checked.add(key)
            capacity = disk_capacity_for_path(raw_path, settings)
            remaining = capacity.free_bytes - max(0, int(expected_write_bytes))
            if remaining < capacity.hard_reserve_bytes:
                raise DiskCapacityError("insufficient storage reserve")
    except DiskCapacityError:
        raise
    except OSError as exc:
        raise DiskCapacityError("storage capacity unavailable") from exc


def request_write_domains(*, method: str, path: str) -> tuple[str, ...]:
    method = method.upper()
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return ()
    if path == "/v1/chat/completions":
        return ("memory", "usage")
    if path == "/mcp" or path.startswith("/mcp/"):
        return ("memory", "usage")
    if path == "/auth/tokens" or path.startswith("/auth/tokens/"):
        return ("auth",)
    if path == "/knowledge/search":
        return ("usage",)
    if path == "/knowledge/read":
        return ()
    if path == "/knowledge" or path.startswith("/knowledge/"):
        return ("knowledge", "usage")
    if path == "/memories/search":
        return ("memory", "usage")
    if method == "POST" and path in _USAGE_ONLY_MEMORY_POSTS:
        return ("usage",)
    if method == "POST" and path in _READ_ONLY_MEMORY_POSTS:
        return ()
    if path == "/memories" or path.startswith("/memories/"):
        return ("memory", "usage")
    return ()


def is_storage_exhausted(exc: BaseException) -> bool:
    """Recognize OS quota/full errors, SQLite FULL and wrapped exception groups."""

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, DiskCapacityError):
            return True
        if isinstance(current, OSError) and current.errno in {errno.ENOSPC, errno.EDQUOT}:
            return True
        if isinstance(current, sqlite3.Error):
            raw_code = getattr(current, "sqlite_errorcode", None)
            if isinstance(raw_code, int) and raw_code & 0xFF == sqlite3.SQLITE_FULL:
                return True
        message = str(current).casefold()
        if any(
            marker in message
            for marker in (
                "database or disk is full",
                "no space left on device",
                "disk quota exceeded",
            )
        ):
            return True
        nested = getattr(current, "exceptions", ())
        if isinstance(nested, (list, tuple)):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if isinstance(cause, BaseException):
            pending.append(cause)
        if isinstance(context, BaseException):
            pending.append(context)
    return False


async def send_insufficient_storage(send: Send, *, openai_compatible: bool) -> None:
    if openai_compatible:
        payload = {
            "error": {
                "message": "Memory Gateway 可用存储空间不足，请释放空间后重试",
                "type": "gateway_error",
                "code": "memory_gateway_insufficient_storage",
            }
        }
    else:
        payload = {
            "detail": {
                "code": "insufficient_storage",
                "message": "可用存储空间不足，请释放空间后重试",
            }
        }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 507,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _existing_disk_probe(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _effective_reserve_bytes(
    *,
    configured: int,
    total: int,
    small_volume_divisor: int,
) -> int:
    configured = max(0, int(configured))
    total = max(0, int(total))
    if configured == 0 or total >= _SMALL_VOLUME_BYTES:
        return configured
    # Fixed production defaults must not make a small CI tmpfs or appliance
    # volume permanently unready. Below 1 GiB, cap them to a conservative
    # fraction while still preserving a useful SQLite emergency reserve.
    cap = max(_MIN_ADAPTIVE_RESERVE_BYTES, total // small_volume_divisor)
    cap = min(cap, total // 4)
    return min(configured, cap)


def _estimated_write_bytes(*, body_bytes: int, route_class: str) -> int:
    multiplier = {
        "knowledge_upload": 4,
        "knowledge_restore": 3,
        "memory_restore": 2,
        "knowledge_part": 2,
    }.get(route_class, 1)
    return body_bytes * multiplier
