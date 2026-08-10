from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (
        b"content-security-policy",
        b"default-src 'self'; object-src 'none'; base-uri 'none'; "
        b"frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
        b"style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        # The Console only talks to its same-origin Memory API.  Provider and
        # Model control calls are server-side, so permitting arbitrary HTTP(S)
        # here would turn any future XSS into an easy scoped-token exfiltration
        # channel.
        b"font-src 'self' data:; connect-src 'self'",
    ),
)


class SecurityHeadersMiddleware:
    """Add browser hardening headers without buffering streamed responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in _SECURITY_HEADERS
                    if name not in existing
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
