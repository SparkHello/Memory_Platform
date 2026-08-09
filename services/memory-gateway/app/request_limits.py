from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ChatRequestBodyLimitMiddleware:
    """Bound Chat Completions bodies before FastAPI materializes their JSON.

    The public chat route accepts multimodal data URLs, so a normal JSON model
    would otherwise let a valid client allocate unbounded process memory before
    the downstream Model Gateway can apply its own limit.
    """

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

        declared_length = _content_length(scope)
        if declared_length is not None and declared_length > self.max_body_bytes:
            await _send_too_large(send, self.max_body_bytes)
            return

        messages: list[Message] = []
        received_bytes = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received_bytes += len(message.get("body", b""))
            if received_bytes > self.max_body_bytes:
                await _send_too_large(send, self.max_body_bytes)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            # StreamingResponse listens for a later disconnect while it sends
            # the response.  Once the buffered request has been replayed, hand
            # control back to the original receive callable so that listener
            # can block normally instead of spinning on empty request events.
            return await receive()

        await self.app(scope, replay, send)


def _content_length(scope: Scope) -> int | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"content-length":
            continue
        try:
            value = int(raw_value.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
        return max(0, value)
    return None


async def _send_too_large(send: Send, max_body_bytes: int) -> None:
    body = json.dumps(
        {
            "error": {
                "message": (
                    "Chat Completions 请求正文超过 Memory Gateway 限制 "
                    f"({max_body_bytes} bytes)"
                ),
                "type": "gateway_error",
                "code": "memory_gateway_request_too_large",
            }
        },
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
