"""Privacy-safe Model Gateway usage attribution.

Memory Gateway no longer keeps a local token/cost ledger. Console
``/usage/summary`` proxies Model Gateway; these helpers only attach
opaque HMAC metadata to outbound central requests.
"""

from app.usage.attribution import (
    model_gateway_usage_headers,
    model_gateway_user_tag,
)
from app.usage.context import current_usage_context, model_usage_scope

__all__ = [
    "current_usage_context",
    "model_gateway_usage_headers",
    "model_gateway_user_tag",
    "model_usage_scope",
]
