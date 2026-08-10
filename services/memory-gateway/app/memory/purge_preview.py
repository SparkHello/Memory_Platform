from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json


_PURGE_PREVIEW_TOKEN_VERSION = 1
_PURGE_PREVIEW_TTL = timedelta(minutes=10)


class PurgePreviewTokenError(RuntimeError):
    pass


def sign_purge_preview(
    *,
    secret: str,
    user_id: str,
    requested_memory_ids: list[str],
    purge_memory_ids: list[str],
    fingerprint: str,
) -> tuple[str, str]:
    now = datetime.now(UTC)
    expires_at = now + _PURGE_PREVIEW_TTL
    payload = {
        "version": _PURGE_PREVIEW_TOKEN_VERSION,
        "kind": "memory_purge_preview",
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "user_id": user_id,
        "requested_memory_ids": requested_memory_ids,
        "purge_memory_ids_sha256": purge_memory_ids_digest(purge_memory_ids),
        "purge_memory_count": len(purge_memory_ids),
        "fingerprint": fingerprint,
    }
    payload_bytes = _canonical_json(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return f"{_b64(payload_bytes)}.{_b64(signature)}", expires_at.isoformat()


def verify_purge_preview(*, secret: str, token: str) -> dict:
    try:
        payload_part, signature_part = token.split(".", 1)
        payload_bytes = _unb64(payload_part)
        actual_signature = _unb64(signature_part)
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).digest()
    except Exception as exc:
        raise PurgePreviewTokenError("永久删除预览 token 无效") from exc
    if not hmac.compare_digest(expected_signature, actual_signature):
        raise PurgePreviewTokenError("永久删除预览 token 无效")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PurgePreviewTokenError("永久删除预览 token 无效") from exc
    if not isinstance(payload, dict) or (
        payload.get("version") != _PURGE_PREVIEW_TOKEN_VERSION
        or payload.get("kind") != "memory_purge_preview"
    ):
        raise PurgePreviewTokenError("永久删除预览 token 无效")
    try:
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise PurgePreviewTokenError("永久删除预览 token 无效") from exc
    if expires_at <= datetime.now(UTC):
        raise PurgePreviewTokenError("永久删除预览 token 已过期")
    return payload


def purge_memory_ids_digest(memory_ids: list[str]) -> str:
    canonical_ids = json.dumps(
        sorted(memory_ids),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_ids).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + ("=" * (-len(value) % 4))).encode("ascii"))
