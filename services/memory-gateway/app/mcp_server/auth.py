import json

from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.tokens import AuthPrincipal
from app.mcp_server.context import current_user_id


class MCPAuthMiddleware:
    """Bridge the early-auth principal into MCP's request context."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        principal = scope.get("state", {}).get("auth_principal")
        if not isinstance(principal, AuthPrincipal):
            await _send_json(
                send,
                401,
                {"detail": "Authorization Bearer token 无效"},
                extra_headers=[(b"www-authenticate", b"Bearer")],
            )
            return
        token = current_user_id.set(principal.user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_id.reset(token)


async def _send_json(
    send: Send,
    status: int,
    payload: dict,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("latin-1")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
