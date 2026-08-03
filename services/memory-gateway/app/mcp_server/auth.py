import json

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.mcp_server.context import current_user_id


class MCPAuthMiddleware:
    """MCP 子应用以 ASGI 方式挂载，不经过 FastAPI 依赖注入，鉴权在这一层完成。

    校验规则与 REST 层 require_api_key 保持一致：
    未配置 GATEWAY_API_KEY 返回 500，Bearer token 不匹配返回 401。
    通过后把 X-User-Id（缺省 default）写入 contextvar 供工具函数读取。
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        settings = get_settings()
        if not settings.gateway_api_key:
            await _send_json(send, 500, {"detail": "GATEWAY_API_KEY 未配置"})
            return
        if headers.get("authorization") != f"Bearer {settings.gateway_api_key}":
            await _send_json(
                send,
                401,
                {"detail": "Authorization Bearer token 无效"},
                extra_headers=[(b"www-authenticate", b"Bearer")],
            )
            return

        token = current_user_id.set(headers.get("x-user-id") or "default")
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
