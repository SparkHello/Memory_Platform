from __future__ import annotations

import hashlib
import hmac
import re
from uuid import uuid4

from app.usage.context import current_usage_context


MODEL_GATEWAY_CORRELATION_HEADER = "X-Model-Gateway-Correlation-ID"
MODEL_GATEWAY_OPERATION_HEADER = "X-Model-Gateway-Operation"
MODEL_GATEWAY_USER_TAG_HEADER = "X-Model-Gateway-User-Tag"
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_USER_TAG_DOMAIN = b"memory-gateway:model-usage-user:v1\0"


class UsageAttributionNotConfigured(RuntimeError):
    """Central usage attribution cannot safely be constructed."""


def model_gateway_usage_headers(
    *,
    signing_secret: str,
    operation: str = "",
    user_id: str = "",
) -> dict[str, str]:
    """Build body-free metadata for one central Model Gateway request.

    The user tag is stable for this Memory installation but irreversible to a
    Model Gateway operator who does not possess the Memory-only signing key.
    Neither the raw user ID nor request content is included in any header.
    """

    if not signing_secret:
        raise UsageAttributionNotConfigured(
            "GATEWAY_SIGNING_SECRET is required for central usage attribution"
        )
    context = current_usage_context()
    logical_user = (user_id or context.user_id or "default").strip() or "default"
    logical_operation = (
        operation or context.operation or "unspecified"
    ).strip() or "unspecified"
    if not _OPAQUE_ID_RE.fullmatch(logical_operation):
        logical_operation = "unspecified"
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        _USER_TAG_DOMAIN + logical_user.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        MODEL_GATEWAY_CORRELATION_HEADER: f"mgc_{uuid4().hex}",
        MODEL_GATEWAY_OPERATION_HEADER: logical_operation,
        MODEL_GATEWAY_USER_TAG_HEADER: f"usr_{digest}",
    }


def model_gateway_user_tag(*, signing_secret: str, user_id: str) -> str:
    return model_gateway_usage_headers(
        signing_secret=signing_secret,
        user_id=user_id,
        operation="usage.summary",
    )[MODEL_GATEWAY_USER_TAG_HEADER]
