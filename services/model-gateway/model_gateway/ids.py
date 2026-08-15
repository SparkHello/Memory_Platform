"""Shared internal ID helpers for config entities.

These were historically copied between the quickstart, admin, CLI and user
console modules; they live here so every layer derives identical slugs,
collision-free IDs and secret references.
"""

from __future__ import annotations

from hashlib import sha256
import re

from model_gateway.models import validate_id


def slug_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._:-]+", "-", value.lower()).strip("-._:")
    if not normalized:
        normalized = "item"
    if len(normalized) > 120:
        digest = sha256(normalized.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized[:110].rstrip('-._:')}-{digest}"
    return normalized


def unique_id(candidate: str, records: object) -> str:
    container = records if hasattr(records, "__contains__") else {}
    if candidate not in container:  # type: ignore[operator]
        return candidate
    index = 2
    while True:
        suffix = f"-{index}"
        alternate = candidate[: 120 - len(suffix)].rstrip("-._:") + suffix
        if alternate not in container:  # type: ignore[operator]
            return alternate
        index += 1


def default_secret_ref(prefix: str, item_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", item_id).strip("_").upper()
    value = f"{prefix}_{slug}_API_KEY"
    if len(value) <= 120:
        return validate_id(value, "secret_ref")
    digest = sha256(item_id.encode("utf-8")).hexdigest()[:8].upper()
    return validate_id(f"{value[:111]}_{digest}", "secret_ref")
