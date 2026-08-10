from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading
from typing import Iterable, Protocol

from starlette.types import ASGIApp, Receive, Scope, Send


MIB = 1024 * 1024
SMALL_VOLUME_BYTES = 1024 * MIB
MIN_ADAPTIVE_RESERVE_BYTES = 64 * 1024
LEDGER_FIXED_WRITE_BYTES = 256 * 1024
LEDGER_ATTEMPT_WRITE_BYTES = 64 * 1024

_LOGGER = logging.getLogger(__name__)


class StorageSettings(Protocol):
    disk_soft_reserve_bytes: int
    disk_hard_reserve_bytes: int


class StorageCapacityError(RuntimeError):
    """A durable write cannot safely stay above the configured reserve."""


@dataclass(frozen=True, slots=True)
class DiskCapacity:
    total_bytes: int
    free_bytes: int
    soft_reserve_bytes: int
    hard_reserve_bytes: int


class StorageFaultMonitor:
    """Keep an unexpected post-send ledger failure visible to readiness.

    Capacity and file probes remain the source of truth.  The one-shot latch
    prevents a transient provider-billed/ledger-failed race from disappearing
    before an operator or orchestrator can observe at least one failed readyz.
    No exception text, path, request body or provider payload is retained.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason = ""

    def mark_unavailable(self) -> None:
        with self._lock:
            self._reason = "disk_unavailable"

    def consume_after_successful_probe(self) -> str:
        with self._lock:
            reason = self._reason
            self._reason = ""
            return reason


class StorageErrorMiddleware:
    """Map pre-response storage exhaustion/unavailability to a stable safe 507."""

    def __init__(self, app: ASGIApp, *, monitor: StorageFaultMonitor) -> None:
        self.app = app
        self.monitor = monitor

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
            if not is_storage_exhausted(exc):
                raise
            self.monitor.mark_unavailable()
            _LOGGER.error(
                "durable storage write failed (%s)",
                type(exc).__name__,
            )
            if response_started:
                raise
            await send_insufficient_storage(
                send,
                openai_compatible=str(scope.get("path") or "").startswith("/v1/"),
            )


def configured_storage_paths(paths: object) -> tuple[Path, Path, Path]:
    return (
        Path(getattr(paths, "config")),
        Path(getattr(paths, "secrets")),
        Path(getattr(paths, "usage_db")),
    )


def disk_capacity_for_path(path: Path, settings: StorageSettings) -> DiskCapacity:
    usage = shutil.disk_usage(_existing_disk_probe(path.expanduser()))
    total = int(usage.total)
    return DiskCapacity(
        total_bytes=total,
        free_bytes=int(usage.free),
        soft_reserve_bytes=_effective_reserve_bytes(
            configured=settings.disk_soft_reserve_bytes,
            total=total,
            small_volume_divisor=16,
        ),
        hard_reserve_bytes=_effective_reserve_bytes(
            configured=settings.disk_hard_reserve_bytes,
            total=total,
            small_volume_divisor=64,
        ),
    )


def storage_readiness_reason(
    paths: object,
    settings: StorageSettings,
    *,
    usage_probe: object | None = None,
) -> str:
    """Return a safe reason code without exposing paths or byte counts."""

    checked_volumes: set[int | str] = set()
    try:
        for path in configured_storage_paths(paths):
            _assert_existing_regular_writable(path)
            probe = _existing_disk_probe(path)
            identity = _volume_identity(probe)
            if identity in checked_volumes:
                continue
            checked_volumes.add(identity)
            capacity = disk_capacity_for_path(path, settings)
            if (
                capacity.soft_reserve_bytes
                and capacity.free_bytes < capacity.soft_reserve_bytes
            ):
                return "disk_low"
        if usage_probe is not None:
            probe = getattr(usage_probe, "probe_writable", None)
            if callable(probe):
                probe()
    except Exception as exc:
        if is_capacity_exhausted(exc):
            return "disk_low"
        if is_storage_exhausted(exc) or isinstance(
            exc, (OSError, sqlite3.Error, StorageCapacityError)
        ):
            return "disk_unavailable"
        raise
    return ""


def ensure_write_capacity(
    paths: Iterable[Path],
    settings: StorageSettings,
    *,
    expected_write_bytes: int,
) -> None:
    """Fail before a durable write would consume the emergency hard reserve."""

    expected = max(0, int(expected_write_bytes))
    checked_volumes: set[int | str] = set()
    try:
        for path in paths:
            candidate = Path(path).expanduser()
            _assert_existing_regular_writable(candidate)
            probe = _existing_disk_probe(candidate)
            identity = _volume_identity(probe)
            if identity in checked_volumes:
                continue
            checked_volumes.add(identity)
            capacity = disk_capacity_for_path(candidate, settings)
            if (
                capacity.hard_reserve_bytes
                and capacity.free_bytes - expected < capacity.hard_reserve_bytes
            ):
                raise StorageCapacityError("insufficient storage reserve")
    except StorageCapacityError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise StorageCapacityError("storage capacity unavailable") from exc


def ensure_ledger_write_capacity(
    path: Path,
    settings: StorageSettings,
    *,
    expected_write_bytes: int,
    usage_probe: object,
) -> None:
    ensure_write_capacity(
        (path,),
        settings,
        expected_write_bytes=expected_write_bytes,
    )
    try:
        probe = getattr(usage_probe, "probe_writable")
        probe()
    except StorageCapacityError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise StorageCapacityError("usage ledger unavailable") from exc


def estimated_ledger_write_bytes(*, body_bytes: int, attempts: int) -> int:
    """Conservative bound for one request and its metadata-only attempt ledger."""

    return (
        max(0, int(body_bytes))
        + LEDGER_FIXED_WRITE_BYTES
        + max(1, int(attempts)) * LEDGER_ATTEMPT_WRITE_BYTES
    )


def is_storage_exhausted(exc: BaseException) -> bool:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, StorageCapacityError):
            return True
        if isinstance(current, OSError) and current.errno in {
            errno.ENOSPC,
            errno.EDQUOT,
            errno.EROFS,
            errno.EIO,
        }:
            return True
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if isinstance(code, int) and code & 0xFF in {
                sqlite3.SQLITE_FULL,
                sqlite3.SQLITE_READONLY,
                sqlite3.SQLITE_CANTOPEN,
                sqlite3.SQLITE_IOERR,
            }:
                return True
        message = str(current).casefold()
        if any(
            marker in message
            for marker in (
                "database or disk is full",
                "no space left on device",
                "disk quota exceeded",
                "attempt to write a readonly database",
                "unable to open database file",
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


def is_capacity_exhausted(exc: BaseException) -> bool:
    """Recognize only quota/free-space failures, not read-only/unavailable I/O."""

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, StorageCapacityError):
            return True
        if isinstance(current, OSError) and current.errno in {
            errno.ENOSPC,
            errno.EDQUOT,
        }:
            return True
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_FULL:
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


async def send_insufficient_storage(
    send: Send,
    *,
    openai_compatible: bool,
) -> None:
    if openai_compatible:
        payload = {
            "error": {
                "message": "Model Gateway 可用存储空间不足，请释放空间后重试",
                "type": "gateway_error",
                "code": "model_gateway_insufficient_storage",
                "attempts": 0,
            }
        }
    else:
        payload = {
            "detail": {
                "code": "model_gateway_insufficient_storage",
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
                (b"x-content-type-options", b"nosniff"),
                (b"x-model-gateway-attempts", b"0"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _assert_existing_regular_writable(path: Path) -> None:
    flags = os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "storage target is not a regular file")
    finally:
        os.close(descriptor)


def _existing_disk_probe(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _volume_identity(path: Path) -> int | str:
    try:
        return int(path.stat().st_dev)
    except OSError:
        return str(path.resolve())


def _effective_reserve_bytes(
    *,
    configured: int,
    total: int,
    small_volume_divisor: int,
) -> int:
    configured = max(0, int(configured))
    total = max(0, int(total))
    if configured == 0 or total >= SMALL_VOLUME_BYTES:
        return configured
    cap = max(MIN_ADAPTIVE_RESERVE_BYTES, total // small_volume_divisor)
    cap = min(cap, total // 4)
    return min(configured, cap)
