from __future__ import annotations

import asyncio
from collections import deque
import io
import json
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.request_limits import (
    CHAT_BODY_LIMIT,
    KNOWLEDGE_PART_BODY_LIMIT,
    KNOWLEDGE_RESTORE_BODY_LIMIT,
    KNOWLEDGE_UPLOAD_OVERHEAD,
    MEMORY_RESTORE_BODY_LIMIT,
    NORMAL_JSON_BODY_LIMIT,
    ChatRequestBodyLimitMiddleware,
    RouteAwareRequestBodyLimitMiddleware,
    initialize_request_spool_directories,
)


@pytest.mark.asyncio
async def test_chunked_chat_body_is_limited_without_content_length() -> None:
    downstream_called = False

    async def downstream(scope, receive, send) -> None:
        nonlocal downstream_called
        downstream_called = True

    incoming = deque(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]
    )
    outgoing = []

    async def receive():
        return incoming.popleft()

    async def send(message):
        outgoing.append(message)

    middleware = ChatRequestBodyLimitMiddleware(downstream, max_body_bytes=6)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        },
        receive,
        send,
    )

    assert downstream_called is False
    assert outgoing[0]["type"] == "http.response.start"
    assert outgoing[0]["status"] == 413


@pytest.mark.asyncio
async def test_chunked_body_at_exact_limit_is_replayed_without_truncation() -> None:
    incoming = deque(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56", "more_body": False},
        ]
    )
    replayed = bytearray()
    outgoing: list[dict] = []

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            replayed.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return incoming.popleft()

    async def send(message):
        outgoing.append(message)

    await ChatRequestBodyLimitMiddleware(downstream, max_body_bytes=6)(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        },
        receive,
        send,
    )

    assert bytes(replayed) == b"123456"
    assert outgoing[0]["status"] == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "limit", "route_class"),
    [
        ("/memories/search", "POST", NORMAL_JSON_BODY_LIMIT, "json"),
        ("/mcp", "POST", NORMAL_JSON_BODY_LIMIT, "json"),
        ("/v1/chat/completions", "POST", CHAT_BODY_LIMIT, "chat"),
        (
            "/knowledge/uploads/upload-1/parts/0",
            "PUT",
            KNOWLEDGE_PART_BODY_LIMIT,
            "knowledge_part",
        ),
        ("/memories/restore", "POST", MEMORY_RESTORE_BODY_LIMIT, "memory_restore"),
        (
            "/memories/stack-backup/validate",
            "POST",
            MEMORY_RESTORE_BODY_LIMIT,
            "memory_restore",
        ),
        (
            "/knowledge/restore",
            "POST",
            KNOWLEDGE_RESTORE_BODY_LIMIT,
            "knowledge_restore",
        ),
    ],
)
async def test_route_specific_content_length_is_rejected_without_reading_body(
    path,
    method,
    limit,
    route_class,
) -> None:
    receive_calls = 0
    downstream_called = False
    outgoing: list[dict] = []

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        raise AssertionError("oversized declared body must not be read")

    async def send(message):
        outgoing.append(message)

    settings = Settings(
        _env_file=None,
        CHAT_GATEWAY_MAX_REQUEST_BODY_BYTES=CHAT_BODY_LIMIT,
    )
    middleware = RouteAwareRequestBodyLimitMiddleware(
        downstream,
        settings_provider=lambda: settings,
    )
    await middleware(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"content-length", str(limit + 1).encode())],
        },
        receive,
        send,
    )

    assert receive_calls == 0
    assert downstream_called is False
    assert outgoing[0]["status"] == 413
    payload = json.loads(outgoing[1]["body"])
    if route_class == "chat":
        assert payload["error"]["code"] == "memory_gateway_request_too_large"
    else:
        assert payload["detail"]["route_class"] == route_class
        assert payload["detail"]["limit_bytes"] == limit


@pytest.mark.asyncio
async def test_chunked_normal_json_is_spooled_and_rejected_at_one_mib() -> None:
    downstream_called = False
    outgoing: list[dict] = []
    incoming = deque(
        [
            {"type": "http.request", "body": b"x" * NORMAL_JSON_BODY_LIMIT, "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ]
    )

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        return incoming.popleft()

    async def send(message):
        outgoing.append(message)

    middleware = RouteAwareRequestBodyLimitMiddleware(downstream)
    await middleware(
        {"type": "http", "method": "POST", "path": "/memories/search", "headers": []},
        receive,
        send,
    )

    assert downstream_called is False
    assert outgoing[0]["status"] == 413


@pytest.mark.asyncio
async def test_duplicate_or_non_decimal_content_length_is_rejected() -> None:
    async def downstream(scope, receive, send):
        raise AssertionError("invalid framing must not reach downstream")

    async def receive():
        raise AssertionError("invalid framing must not consume body")

    for headers in (
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"1, 1")],
        [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
    ):
        outgoing: list[dict] = []

        async def send(message):
            outgoing.append(message)

        await RouteAwareRequestBodyLimitMiddleware(downstream)(
            {
                "type": "http",
                "method": "POST",
                "path": "/memories/search",
                "headers": headers,
            },
            receive,
            send,
        )
        assert outgoing[0]["status"] == 400
        assert json.loads(outgoing[1]["body"])["detail"]["code"] == "invalid_content_length"


@pytest.mark.asyncio
async def test_knowledge_binary_upload_limit_tracks_document_limit_plus_one_mib() -> None:
    settings = Settings(_env_file=None, KNOWLEDGE_MAX_DOCUMENT_BYTES=2048)
    outgoing: list[dict] = []
    receive_calls = 0

    async def downstream(scope, receive, send):
        raise AssertionError("oversized upload must not reach endpoint")

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        raise AssertionError("oversized declared upload must not be read")

    async def send(message):
        outgoing.append(message)

    await RouteAwareRequestBodyLimitMiddleware(
        downstream,
        settings_provider=lambda: settings,
    )(
        {
            "type": "http",
            "method": "POST",
            "path": "/knowledge/import",
            "headers": [
                (
                    b"content-length",
                    str(2048 + KNOWLEDGE_UPLOAD_OVERHEAD + 1).encode(),
                )
            ],
        },
        receive,
        send,
    )

    assert receive_calls == 0
    assert outgoing[0]["status"] == 413
    detail = json.loads(outgoing[1]["body"])["detail"]
    assert detail["limit_bytes"] == 2048 + KNOWLEDGE_UPLOAD_OVERHEAD
    assert detail["route_class"] == "knowledge_upload"


@pytest.mark.asyncio
async def test_large_request_spool_uses_private_matching_data_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=str(tmp_path / "memory" / "memory.db"),
        KNOWLEDGE_DATABASE_PATH=str(tmp_path / "knowledge" / "knowledge.db"),
        AUTH_DATABASE_PATH=str(tmp_path / "auth" / "auth.db"),
    )
    captured: list[dict] = []

    class MemorySpool(io.BytesIO):
        pass

    def fake_spool(**kwargs):
        captured.append(kwargs)
        return MemorySpool()

    monkeypatch.setattr(
        "app.request_limits.tempfile.SpooledTemporaryFile",
        fake_spool,
    )
    incoming = deque(
        [{"type": "http.request", "body": b"{}", "more_body": False}]
    )

    async def receive():
        return incoming.popleft()

    async def downstream(scope, receive, send):
        assert (await receive())["body"] == b"{}"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    outgoing: list[dict] = []

    async def send(message):
        outgoing.append(message)

    await RouteAwareRequestBodyLimitMiddleware(
        downstream,
        settings_provider=lambda: settings,
    )(
        {
            "type": "http",
            "method": "POST",
            "path": "/knowledge/restore",
            "headers": [],
        },
        receive,
        send,
    )

    expected = tmp_path / "knowledge" / ".request-spool"
    assert captured[0]["dir"] == expected
    assert captured[0]["prefix"] == "memgw-request-"
    if os.name == "posix":
        assert stat_mode(expected) == 0o700
    assert outgoing[0]["status"] == 204


def test_spool_startup_cleanup_is_exact_and_does_not_follow_links(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=str(tmp_path / "memory" / "memory.db"),
        KNOWLEDGE_DATABASE_PATH=str(tmp_path / "knowledge" / "knowledge.db"),
        AUTH_DATABASE_PATH=str(tmp_path / "auth" / "auth.db"),
    )
    spool = tmp_path / "memory" / ".request-spool"
    spool.mkdir(parents=True)
    stale = spool / "memgw-request-stale"
    unrelated = spool / "keep-me"
    target = tmp_path / "outside"
    stale.write_bytes(b"stale")
    unrelated.write_bytes(b"safe")
    target.write_bytes(b"outside")
    link = spool / "memgw-request-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        link = None

    initialize_request_spool_directories(settings)

    assert not stale.exists()
    assert unrelated.read_bytes() == b"safe"
    assert target.read_bytes() == b"outside"
    if link is not None:
        assert link.is_symlink()


@pytest.mark.asyncio
async def test_restore_requests_are_serialized_across_memory_and_knowledge(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=str(tmp_path / "memory.db"),
        KNOWLEDGE_DATABASE_PATH=str(tmp_path / "knowledge.db"),
        AUTH_DATABASE_PATH=str(tmp_path / "auth.db"),
    )
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    entered: list[str] = []

    async def downstream(scope, receive, send):
        await receive()
        entered.append(scope["path"])
        if len(entered) == 1:
            first_entered.set()
            await release_first.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RouteAwareRequestBodyLimitMiddleware(
        downstream,
        settings_provider=lambda: settings,
    )

    async def run(path: str) -> None:
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        await middleware(
            {"type": "http", "method": "POST", "path": path, "headers": []},
            receive,
            send,
        )

    first = asyncio.create_task(run("/memories/restore"))
    await first_entered.wait()
    second = asyncio.create_task(run("/knowledge/restore"))
    await asyncio.sleep(0)
    assert entered == ["/memories/restore"]
    release_first.set()
    await asyncio.gather(first, second)
    assert entered == ["/memories/restore", "/knowledge/restore"]


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
