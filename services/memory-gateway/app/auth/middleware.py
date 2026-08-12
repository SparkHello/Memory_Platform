from __future__ import annotations

from collections import defaultdict, deque
import hmac
import json
import math
import sqlite3
import threading
import time
from typing import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.tokens import AuthPrincipal, AuthTokenStore
from app.config import Settings, get_settings
from app.disk_capacity import DiskCapacityError, is_storage_exhausted


_ROLE_LIMITS = {
    "chat": (60, 4),
    "mcp": (120, 8),
    "console": (120, 8),
}
_WINDOW_SECONDS = 60.0
_IRREVERSIBLE_LIMIT = 10
_AUTH_TOKEN_MAX_LENGTH = 4096


class ProcessAccessGate:
    """Per-process sliding-window and in-flight limits, keyed by credential."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._irreversible: dict[str, deque[float]] = defaultdict(deque)
        self._active: dict[tuple[str, str], int] = defaultdict(int)

    def acquire(
        self,
        *,
        identity: str,
        role: str,
        irreversible: bool,
    ) -> tuple[bool, int, str]:
        limit, concurrency = _ROLE_LIMITS[role]
        key = (identity, role)
        now = self._clock()
        cutoff = now - _WINDOW_SECONDS
        with self._lock:
            requests = self._requests[key]
            _prune(requests, cutoff)
            destructive = self._irreversible[identity]
            _prune(destructive, cutoff)
            if len(requests) >= limit:
                return False, _retry_after(requests[0], now), "rate"
            if irreversible and len(destructive) >= _IRREVERSIBLE_LIMIT:
                return False, _retry_after(destructive[0], now), "irreversible"
            if self._active[key] >= concurrency:
                return False, 1, "concurrency"
            requests.append(now)
            if irreversible:
                destructive.append(now)
            self._active[key] += 1
        return True, 0, ""

    def release(self, *, identity: str, role: str) -> None:
        key = (identity, role)
        with self._lock:
            active = self._active.get(key, 0)
            if active <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] = active - 1


class EarlyAuthMiddleware:
    """Authenticate protected routes before any request body is consumed."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings_provider: Callable[[], Settings] = get_settings,
        gate: ProcessAccessGate | None = None,
    ) -> None:
        self.app = app
        self.settings_provider = settings_provider
        self.gate = gate or ProcessAccessGate()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        role = _role_for_path(str(scope.get("path") or ""))
        if role is None:
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        authorization_values = _header_values(headers, b"authorization")
        if len(authorization_values) != 1:
            await _send_auth_error(send, 401, "Authorization Bearer token 无效")
            return
        access_token = _parse_bearer(authorization_values[0])
        if access_token is None:
            await _send_auth_error(send, 401, "Authorization Bearer token 无效")
            return

        settings = self.settings_provider()
        principal = _authenticate(access_token, settings)
        if principal is None:
            await _send_auth_error(send, 401, "Authorization Bearer token 无效")
            return
        if not principal.legacy and principal.role != role:
            await _send_auth_error(
                send,
                403,
                f"当前 token scope 不能访问 {role} 接口",
                authenticate=False,
            )
            return

        user_headers = _header_values(headers, b"x-user-id")
        if len(user_headers) > 1:
            await _send_auth_error(
                send,
                400,
                "X-User-Id 不能重复",
                authenticate=False,
            )
            return
        requested_user_id = ""
        if user_headers:
            requested_user_id = user_headers[0].decode("latin-1").strip()
            if not _valid_user_id(requested_user_id):
                await _send_auth_error(
                    send,
                    400,
                    "X-User-Id 格式无效",
                    authenticate=False,
                )
                return
        principal = _bind_user(principal, requested_user_id, settings)
        if principal is None:
            await _send_auth_error(
                send,
                403,
                "X-User-Id 与当前凭证绑定的用户不匹配",
                authenticate=False,
            )
            return

        identity = principal.identity
        irreversible = role == "console" and _is_irreversible(scope)
        admitted, retry_after, reason = self.gate.acquire(
            identity=identity,
            role=role,
            irreversible=irreversible,
        )
        if not admitted:
            details = {
                "rate": "请求频率超过当前 token 的角色限制",
                "irreversible": "不可逆管理操作超过每分钟 10 次限制",
                "concurrency": "并发请求超过当前 token 的角色限制",
            }
            await _send_auth_error(
                send,
                429,
                details[reason],
                authenticate=False,
                retry_after=retry_after,
            )
            return

        state = scope.setdefault("state", {})
        state["auth_principal"] = principal
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.release(identity=identity, role=role)


def _authenticate(token: str, settings: Settings) -> AuthPrincipal | None:
    legacy_key = settings.gateway_api_key
    if (
        settings.gateway_legacy_api_key_enabled
        and legacy_key
        and _constant_time_text_equal(token, legacy_key)
    ):
        bound_user_id = settings.gateway_user_id.strip() or "default"
        return AuthPrincipal(
            identity="legacy",
            token_id="legacy",
            name="legacy gateway key",
            user_id=bound_user_id,
            role="legacy",
            legacy=True,
            memory_access="read-write",
        )
    if not token.startswith("mgw_"):
        return None
    try:
        record = AuthTokenStore(settings.auth_database_path).authenticate(token)
    except (OSError, sqlite3.Error, ValueError) as exc:
        if is_storage_exhausted(exc):
            raise DiskCapacityError("auth storage exhausted") from exc
        return None
    if record is None:
        return None
    return AuthPrincipal(
        identity=f"token:{record.token_id}",
        token_id=record.token_id,
        name=record.name,
        user_id=record.user_id,
        role=record.role,
        legacy=False,
        memory_access=record.memory_access,
    )


def _bind_user(
    principal: AuthPrincipal,
    requested_user_id: str,
    settings: Settings,
) -> AuthPrincipal | None:
    if not requested_user_id or requested_user_id == principal.user_id:
        return principal
    if not principal.legacy or not settings.gateway_allow_user_id_header:
        return None
    return AuthPrincipal(
        identity=principal.identity,
        token_id=principal.token_id,
        name=principal.name,
        user_id=requested_user_id,
        role=principal.role,
        legacy=True,
        memory_access=principal.memory_access,
    )


def _role_for_path(path: str) -> str | None:
    if _path_belongs_to(path, "/v1"):
        return "chat"
    if _path_belongs_to(path, "/mcp"):
        return "mcp"
    if any(
        _path_belongs_to(path, prefix)
        for prefix in ("/auth", "/memories", "/knowledge", "/providers", "/usage")
    ):
        return "console"
    return None


def _path_belongs_to(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _is_irreversible(scope: Scope) -> bool:
    method = str(scope.get("method") or "").upper()
    path = str(scope.get("path") or "")
    if (
        method == "DELETE"
        or path.endswith("/purge")
        or path == "/memories/deleted/purge/commit"
    ):
        return True
    if path.startswith("/providers"):
        return method in {"POST", "PUT", "PATCH", "DELETE"} and not (
            path.endswith("/check")
            or path
            in {
                "/providers/routes/validate",
                "/providers/channels/discover",
                "/providers/channel-bundles/validate",
                # 只读连通性探测：不修改任何配置，不应占用不可逆操作预算。
                "/providers/live-probe",
            }
        )
    return path in {
        "/memories/restore",
        "/memories/merge",
        "/memories/forget",
        "/memories/archive-expired",
        "/memories/review/actions",
        "/memories/review/revise/apply",
        "/knowledge/restore",
    }


def _header_values(
    headers: list[tuple[bytes, bytes]],
    expected: bytes,
) -> list[bytes]:
    return [value for name, value in headers if name.lower() == expected]


def _parse_bearer(raw_value: bytes) -> str | None:
    try:
        value = raw_value.decode("utf-8")
    except UnicodeDecodeError:
        value = raw_value.decode("latin-1")
    parts = value.split(" ")
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not parts[1]
        or len(parts[1]) > _AUTH_TOKEN_MAX_LENGTH
        or any(character.isspace() for character in parts[1])
    ):
        return None
    return parts[1]


def _constant_time_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _valid_user_id(user_id: str) -> bool:
    return bool(user_id) and len(user_id) <= 128 and not any(
        ord(character) < 32 or ord(character) == 127 for character in user_id
    )


def _prune(values: deque[float], cutoff: float) -> None:
    while values and values[0] <= cutoff:
        values.popleft()


def _retry_after(oldest: float, now: float) -> int:
    return max(1, math.ceil(_WINDOW_SECONDS - (now - oldest)))


async def _send_auth_error(
    send: Send,
    status: int,
    detail: str,
    *,
    authenticate: bool = True,
    retry_after: int = 0,
) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if status == 401 and authenticate:
        headers.append((b"www-authenticate", b"Bearer"))
    if retry_after:
        headers.append((b"retry-after", str(retry_after).encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
