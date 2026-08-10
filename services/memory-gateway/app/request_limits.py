from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings
from app.disk_capacity import ensure_request_write_capacity


MIB = 1024 * 1024
NORMAL_JSON_BODY_LIMIT = 1 * MIB
CHAT_BODY_LIMIT = 16 * MIB
KNOWLEDGE_PART_BODY_LIMIT = 5 * MIB
MEMORY_RESTORE_BODY_LIMIT = 72 * MIB
KNOWLEDGE_RESTORE_BODY_LIMIT = 128 * MIB
KNOWLEDGE_UPLOAD_OVERHEAD = 1 * MIB
_REPLAY_CHUNK_BYTES = 64 * 1024
_SPOOL_MEMORY_BYTES = 1 * MIB
_SPOOL_DIRECTORY_NAME = ".request-spool"
_SPOOL_FILE_PREFIX = "memgw-request-"
_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_KNOWLEDGE_PART_RE = re.compile(r"^/knowledge/uploads/[^/]+/parts/[^/]+/?$")
PUBLIC_PATH_SEGMENT_MAX_CHARS = 200


class RequestTargetLimitMiddleware:
    """Bound decoded path components before route parameter materialization."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and any(
            len(segment) > PUBLIC_PATH_SEGMENT_MAX_CHARS
            for segment in str(scope.get("path") or "").split("/")
        ):
            await _send_json(
                send,
                422,
                {
                    "detail": {
                        "code": "path_identifier_too_long",
                        "message": (
                            "路径标识符不能超过 "
                            f"{PUBLIC_PATH_SEGMENT_MAX_CHARS} 个字符"
                        ),
                    }
                },
            )
            return
        await self.app(scope, receive, send)


class RouteAwareRequestBodyLimitMiddleware:
    """Reject oversized request bodies before routing or JSON parsing.

    Bodies are spooled to an unlinked temporary file so chunked requests can
    be rejected before any endpoint side effect without retaining restore-sized
    payloads in process memory.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings_provider: Callable[[], Settings] = get_settings,
    ) -> None:
        self.app = app
        self.settings_provider = settings_provider
        # Restore bodies can be tens of MiB and the endpoint performs another
        # transactional copy. Serializing the two restore endpoints prevents
        # several valid requests from consuming the data volume reserve at once.
        self._restore_lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        route_class = _route_class(scope)
        if route_class is None:
            await self.app(scope, receive, send)
            return
        if route_class in {"memory_restore", "knowledge_restore"}:
            async with self._restore_lock:
                await self._handle_limited_request(
                    scope,
                    receive,
                    send,
                    route_class=route_class,
                )
            return
        await self._handle_limited_request(
            scope,
            receive,
            send,
            route_class=route_class,
        )

    async def _handle_limited_request(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        route_class: str,
    ) -> None:
        settings = self.settings_provider()
        max_body_bytes = _limit_for_route(route_class, settings)
        declared_length, content_length_valid = _content_length(scope)
        if not content_length_valid:
            await _send_invalid_content_length(send)
            return
        if declared_length is not None and declared_length > max_body_bytes:
            await _send_too_large(send, max_body_bytes, route_class=route_class)
            return
        ensure_request_write_capacity(
            settings,
            method=str(scope.get("method") or ""),
            path=str(scope.get("path") or ""),
            body_bytes=declared_length or 0,
            route_class=route_class,
        )

        spool = tempfile.SpooledTemporaryFile(
            max_size=_SPOOL_MEMORY_BYTES,
            mode="w+b",
            dir=_request_spool_directory(
                settings,
                path=str(scope.get("path") or ""),
            ),
            prefix=_SPOOL_FILE_PREFIX,
        )
        received_bytes = 0
        last_capacity_check = 0
        disconnected = False
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    break
                if message["type"] != "http.request":
                    continue
                body = message.get("body", b"")
                received_bytes += len(body)
                if received_bytes > max_body_bytes:
                    await _send_too_large(
                        send,
                        max_body_bytes,
                        route_class=route_class,
                    )
                    return
                if (
                    received_bytes - last_capacity_check >= MIB
                    or not message.get("more_body", False)
                ):
                    ensure_request_write_capacity(
                        settings,
                        method=str(scope.get("method") or ""),
                        path=str(scope.get("path") or ""),
                        body_bytes=received_bytes,
                        route_class=route_class,
                    )
                    last_capacity_check = received_bytes
                if body:
                    spool.write(body)
                if not message.get("more_body", False):
                    break

            spool.seek(0)
            replay_finished = False

            async def replay() -> Message:
                nonlocal replay_finished
                if not replay_finished:
                    chunk = spool.read(_REPLAY_CHUNK_BYTES)
                    if chunk:
                        more_body = spool.tell() < received_bytes
                        replay_finished = not more_body
                        return {
                            "type": "http.request",
                            "body": chunk,
                            "more_body": more_body,
                        }
                    replay_finished = True
                    if disconnected:
                        return {"type": "http.disconnect"}
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
                return await receive()

            await self.app(scope, replay, send)
        finally:
            spool.close()


def initialize_request_spool_directories(settings: Settings) -> None:
    """Create private data-volume spool dirs and remove exact stale files.

    A hard-killed process may leave a named temporary file on non-POSIX
    platforms. Only our exact randomized prefix is eligible for cleanup.
    """

    directories = {
        _request_spool_directory(settings, path="/memories/restore"),
        _request_spool_directory(settings, path="/knowledge/restore"),
        _request_spool_directory(settings, path="/auth/tokens"),
    }
    for directory in directories:
        for candidate in directory.glob(f"{_SPOOL_FILE_PREFIX}*"):
            try:
                candidate_stat = candidate.lstat()
                if stat.S_ISREG(candidate_stat.st_mode) and candidate_stat.st_nlink == 1:
                    candidate.unlink()
            except FileNotFoundError:
                continue


def _request_spool_directory(settings: Settings, *, path: str) -> Path:
    if path == "/knowledge" or path.startswith("/knowledge/"):
        database_path = settings.knowledge_database_path
    elif path == "/auth" or path.startswith("/auth/"):
        database_path = settings.auth_database_path
    else:
        database_path = settings.database_path
    parent = Path(database_path).expanduser().resolve(strict=False).parent
    directory = parent / _SPOOL_DIRECTORY_NAME
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_stat = directory.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(
            directory_stat.st_mode
        ):
            raise OSError("request spool path is not a private directory")
        if os.name == "posix":
            os.chmod(directory, 0o700)
    except OSError:
        # DiskCapacityMiddleware turns ENOSPC/EDQUOT into the public 507
        # contract. Other path-integrity errors intentionally fail closed.
        raise
    return directory


class ChatRequestBodyLimitMiddleware(RouteAwareRequestBodyLimitMiddleware):
    """Backward-compatible focused limiter used by older unit tests."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max(1, int(max_body_bytes))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() != "POST"
            or scope.get("path") != "/v1/chat/completions"
        ):
            await self.app(scope, receive, send)
            return
        settings = get_settings().model_copy(
            update={"chat_gateway_max_request_body_bytes": self.max_body_bytes}
        )
        middleware = RouteAwareRequestBodyLimitMiddleware(
            self.app,
            settings_provider=lambda: settings,
        )
        await middleware(scope, receive, send)


def _route_class(scope: Scope) -> str | None:
    if scope["type"] != "http" or str(scope.get("method") or "").upper() not in _BODY_METHODS:
        return None
    path = str(scope.get("path") or "")
    if path == "/v1/chat/completions":
        return "chat"
    if _KNOWLEDGE_PART_RE.fullmatch(path):
        return "knowledge_part"
    if path == "/memories/restore":
        return "memory_restore"
    if path == "/knowledge/restore":
        return "knowledge_restore"
    if path == "/knowledge/import":
        return "knowledge_upload"
    if any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in (
            "/v1",
            "/mcp",
            "/auth",
            "/memories",
            "/knowledge",
            "/providers",
            "/usage",
        )
    ):
        return "json"
    return None


def _limit_for_route(route_class: str, settings: Settings) -> int:
    if route_class == "chat":
        return min(CHAT_BODY_LIMIT, settings.chat_gateway_max_request_body_bytes)
    if route_class == "knowledge_part":
        return KNOWLEDGE_PART_BODY_LIMIT
    if route_class == "memory_restore":
        return MEMORY_RESTORE_BODY_LIMIT
    if route_class == "knowledge_restore":
        return KNOWLEDGE_RESTORE_BODY_LIMIT
    if route_class == "knowledge_upload":
        return settings.knowledge_max_document_bytes + KNOWLEDGE_UPLOAD_OVERHEAD
    return NORMAL_JSON_BODY_LIMIT


def _content_length(scope: Scope) -> tuple[int | None, bool]:
    values = [
        raw_value
        for raw_name, raw_value in scope.get("headers", [])
        if raw_name.lower() == b"content-length"
    ]
    has_transfer_encoding = any(
        raw_name.lower() == b"transfer-encoding"
        for raw_name, _ in scope.get("headers", [])
    )
    if values and has_transfer_encoding:
        return None, False
    if not values:
        return None, True
    if len(values) != 1:
        return None, False
    try:
        text = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None, False
    if not text or not text.isdigit():
        return None, False
    value = int(text)
    if value > 2**63 - 1:
        return None, False
    return value, True


async def _send_invalid_content_length(send: Send) -> None:
    await _send_json(
        send,
        400,
        {
            "detail": {
                "code": "invalid_content_length",
                "message": "Content-Length 必须是唯一的非负十进制整数",
            }
        },
    )


async def _send_too_large(
    send: Send,
    max_body_bytes: int,
    *,
    route_class: str,
) -> None:
    if route_class == "chat":
        payload = {
            "error": {
                "message": (
                    "Chat Completions 请求正文超过 Memory Gateway 限制 "
                    f"({max_body_bytes} bytes)"
                ),
                "type": "gateway_error",
                "code": "memory_gateway_request_too_large",
            }
        }
    else:
        payload = {
            "detail": {
                "code": "request_body_too_large",
                "message": f"请求正文超过 {max_body_bytes} bytes 限制",
                "route_class": route_class,
                "limit_bytes": max_body_bytes,
            }
        }
    await _send_json(send, 413, payload)


async def _send_json(send: Send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
