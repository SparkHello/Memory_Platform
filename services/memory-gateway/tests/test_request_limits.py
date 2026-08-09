from __future__ import annotations

from collections import deque

import pytest

from app.request_limits import ChatRequestBodyLimitMiddleware


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
