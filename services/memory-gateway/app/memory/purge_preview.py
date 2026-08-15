from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

from app.memory.preview_token import (
    PreviewTokenError,
    sign_preview_token,
    verify_preview_token,
)


_PURGE_PREVIEW_TOKEN_VERSION = 1
_PURGE_PREVIEW_TOKEN_KIND = "memory_purge_preview"
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
        "kind": _PURGE_PREVIEW_TOKEN_KIND,
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "user_id": user_id,
        "requested_memory_ids": requested_memory_ids,
        "purge_memory_ids_sha256": purge_memory_ids_digest(purge_memory_ids),
        "purge_memory_count": len(purge_memory_ids),
        "fingerprint": fingerprint,
    }
    try:
        token = sign_preview_token(secret=secret, payload=payload)
    except PreviewTokenError as exc:
        raise PurgePreviewTokenError("GATEWAY_SIGNING_SECRET 未配置") from exc
    return token, expires_at.isoformat()


def verify_purge_preview(*, secret: str, token: str) -> dict:
    try:
        return verify_preview_token(
            secret=secret,
            token=token,
            expected_version=_PURGE_PREVIEW_TOKEN_VERSION,
            expected_kind=_PURGE_PREVIEW_TOKEN_KIND,
        )
    except PreviewTokenError as exc:
        if exc.reason == "unconfigured":
            raise PurgePreviewTokenError("GATEWAY_SIGNING_SECRET 未配置") from exc
        if exc.reason == "expired":
            raise PurgePreviewTokenError("永久删除预览 token 已过期") from exc
        raise PurgePreviewTokenError("永久删除预览 token 无效") from exc


def purge_memory_ids_digest(memory_ids: list[str]) -> str:
    canonical_ids = json.dumps(
        sorted(memory_ids),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_ids).hexdigest()
