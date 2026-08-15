"""共享的 HMAC-SHA256 预览签名 token 工具。

purge_preview（永久删除预览）与 review_revision（体检修改预览）曾各自
抄写一份 sign/verify 与 _canonical_json/_b64/_unb64，已收敛到此模块。
payload dict 由调用方构造（必须含 version 与 ISO 格式 expires_at；可选
kind 用于区分 token 用途），这里只负责签名、校验与过期判定。

错误统一为 PreviewTokenError，reason 区分 "unconfigured" / "invalid" /
"expired"，调用方各自映射为自己的领域异常（PurgePreviewTokenError /
ReviewRevisionError）与本地化文案。
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import hmac
import json


class PreviewTokenError(RuntimeError):
    """预览 token 签名/校验失败；reason ∈ {"unconfigured", "invalid", "expired"}。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sign_preview_token(*, secret: str, payload: dict) -> str:
    payload_bytes = _canonical_json(payload).encode("utf-8")
    signature = hmac.new(_secret_bytes(secret), payload_bytes, hashlib.sha256).digest()
    return f"{_b64(payload_bytes)}.{_b64(signature)}"


def verify_preview_token(
    *,
    secret: str,
    token: str,
    expected_version: int,
    expected_kind: str | None = None,
) -> dict:
    """校验签名、version/kind 与 expires_at，返回 payload；失败抛 PreviewTokenError。"""
    try:
        payload_part, signature_part = token.split(".", 1)
        payload_bytes = _unb64(payload_part)
        expected = hmac.new(_secret_bytes(secret), payload_bytes, hashlib.sha256).digest()
        actual = _unb64(signature_part)
    except PreviewTokenError:
        raise
    except Exception as exc:
        raise PreviewTokenError("invalid") from exc
    if not hmac.compare_digest(expected, actual):
        raise PreviewTokenError("invalid")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreviewTokenError("invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != expected_version:
        raise PreviewTokenError("invalid")
    if expected_kind is not None and payload.get("kind") != expected_kind:
        raise PreviewTokenError("invalid")
    try:
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise PreviewTokenError("invalid") from exc
    if expires_at <= datetime.now(UTC):
        raise PreviewTokenError("expired")
    return payload


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def _secret_bytes(secret: str) -> bytes:
    # Fail closed: never sign or verify with a well-known fallback key.  The
    # REST layer already returns 503 for an unset GATEWAY_SIGNING_SECRET; this
    # guard keeps any direct caller from silently using a forgeable constant.
    if not secret:
        raise PreviewTokenError("unconfigured")
    return secret.encode("utf-8")
