from __future__ import annotations

from app.config import Settings


class SigningSecretNotConfigured(RuntimeError):
    pass


def require_signing_secret(settings: Settings) -> str:
    """Return the dedicated signing secret without access-key fallbacks."""

    secret = settings.gateway_signing_secret
    if not secret:
        raise SigningSecretNotConfigured(
            "GATEWAY_SIGNING_SECRET 未配置；游标签名与体检预览功能不可用"
        )
    return secret
