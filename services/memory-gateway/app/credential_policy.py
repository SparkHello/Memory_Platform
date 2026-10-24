"""Shared rejection list for example and placeholder credentials.

The CLI already refused to import these values from a project ``.env``.
The server uses the same list so a copied example cannot become a live
all-scope legacy key.
"""

from __future__ import annotations


# Exact, case-insensitive values. ``your-`` is a prefix so example snippets
# such as ``your-api-key`` and ``your-local-...`` stay rejected together.
PLACEHOLDER_CREDENTIALS = frozenset(
    {
        "change-me",
        "your-api-key",
        "your-upstream-api-key",
        "replace-me",
    }
)

PLACEHOLDER_LEGACY_KEY_MESSAGE = (
    "GATEWAY_API_KEY 是公开示例或已知占位符，不能作为 legacy 全权限凭证。"
    "请留空并使用 memgw token create，或写入随机密钥；"
    "已知示例包括 change-me、replace-me 以及 your- 开头的值。"
)


def is_placeholder_credential(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return False
    return lowered in PLACEHOLDER_CREDENTIALS or lowered.startswith("your-")
