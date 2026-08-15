"""Pure helpers shared by the knowledge store modules.

Everything here is storage-independent: validation, JSON codecs, timestamp
formatting, FTS query building, excerpt extraction and cursor signing.  None
of it touches SQLite or ``app.memory``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import hmac
import json
import math
import re
from typing import Any
from uuid import uuid4

from app.knowledge.models import KnowledgeSensitivity
from app.knowledge.store.constants import (
    _CHUNK_PREFIX,
    _CONTENT_TYPES,
    _DOCUMENT_PREFIX,
    _SENSITIVITIES,
    _VERSION_PREFIX,
)
from app.knowledge.store.errors import (
    KnowledgeConflictError,
    KnowledgeValidationError,
)
from app.sensitivity import SENSITIVITY_RANK as _SENSITIVITY_RANK, detect_text_sensitivity


def _document_ref(document_id: str) -> str:
    return f"{_DOCUMENT_PREFIX}{document_id}"


def _version_ref(version_id: str) -> str:
    return f"{_VERSION_PREFIX}{version_id}"


def _chunk_ref(chunk_id: str) -> str:
    return f"{_CHUNK_PREFIX}{chunk_id}"


def _new_id() -> str:
    return uuid4().hex


def _one_reference(primary: str, alias: str, label: str) -> str:
    if primary and alias and primary != alias:
        raise KnowledgeValidationError(f"conflicting {label} identifiers")
    value = primary or alias
    if not value:
        raise KnowledgeValidationError(f"{label} identifier must not be blank")
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_after(*, hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise KnowledgeConflictError("stored upload expiry is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_exported_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise KnowledgeValidationError("exported timestamp must be an ISO string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KnowledgeValidationError("exported timestamp must be an ISO string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _required_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise KnowledgeValidationError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise KnowledgeValidationError(f"{field} must not be blank")
    if len(value) > maximum:
        raise KnowledgeValidationError(f"{field} must not exceed {maximum} characters")
    if "\x00" in value:
        raise KnowledgeValidationError(f"{field} must not contain NUL")
    return value


def _optional_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise KnowledgeValidationError(f"{field} must be a string")
    value = value.strip()
    if len(value) > maximum:
        raise KnowledgeValidationError(f"{field} must not exceed {maximum} characters")
    if "\x00" in value:
        raise KnowledgeValidationError(f"{field} must not contain NUL")
    return value


def _required_embedding_space_id(value: str) -> str:
    return " ".join(_required_text(value, "embedding space id", 300).split())


def _optional_embedding_space_id(value: str) -> str:
    return " ".join(_optional_text(value, "embedding space id", 300).split())


def _bounded_int(value: int, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise KnowledgeValidationError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )
    return value


def _validate_content_type(value: str) -> str:
    if value not in _CONTENT_TYPES:
        raise KnowledgeValidationError("content_type must be text/plain or text/markdown")
    return value


def _validate_sensitivity(value: str) -> KnowledgeSensitivity:
    if value not in _SENSITIVITIES:
        raise KnowledgeValidationError("sensitivity must be normal, private, or sensitive")
    return value  # type: ignore[return-value]


def _validate_tags(values: Sequence[str] | Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise KnowledgeValidationError("tags must be a list of strings")
    if len(values) > 32:
        raise KnowledgeValidationError("tags must not contain more than 32 items")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = _required_text(value, "tag", 80)
        normalized = tag.casefold()
        if normalized not in seen:
            seen.add(normalized)
            result.append(tag)
    return result


def _validate_metadata(value: Any) -> dict[str, str | int | float | bool]:
    if not isinstance(value, dict):
        raise KnowledgeValidationError("metadata must be an object")
    if len(value) > 50:
        raise KnowledgeValidationError("metadata must not contain more than 50 fields")
    result: dict[str, str | int | float | bool] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, "metadata key", 80)
        if key.startswith("_"):
            raise KnowledgeValidationError("metadata keys must not start with underscore")
        if isinstance(raw_value, bool):
            result[key] = raw_value
        elif isinstance(raw_value, str):
            result[key] = _optional_text(raw_value, f"metadata.{key}", 500)
        elif isinstance(raw_value, int):
            result[key] = raw_value
        elif isinstance(raw_value, float) and math.isfinite(raw_value):
            result[key] = raw_value
        else:
            raise KnowledgeValidationError(
                "metadata values must be strings, numbers, or booleans"
            )
    return result


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_metadata(value: str) -> dict[str, str | int | float | bool]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    try:
        return _validate_metadata(parsed)
    except KnowledgeValidationError:
        return {}


def _validated_vector(values: Sequence[float] | Any) -> list[float]:
    if not isinstance(values, (list, tuple)) or not values:
        raise KnowledgeValidationError("embedding vector must be a non-empty list")
    if len(values) > 16_384:
        raise KnowledgeValidationError("embedding vector is too large")
    result: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise KnowledgeValidationError("embedding values must be finite numbers")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise KnowledgeValidationError(
                "embedding values must be finite numbers"
            ) from exc
        if not math.isfinite(number):
            raise KnowledgeValidationError("embedding values must be finite numbers")
        result.append(number)
    return result


def _detect_sensitivity(text: str) -> KnowledgeSensitivity:
    return detect_text_sensitivity(text)  # type: ignore[return-value]


def detect_knowledge_text_sensitivity(text: str) -> KnowledgeSensitivity:
    """Public local detector used by storage and knowledge-agent egress gates."""

    if not isinstance(text, str):
        raise KnowledgeValidationError("text must be a string")
    return _detect_sensitivity(text)


def _detected_sensitivity(*texts: str | None) -> KnowledgeSensitivity:
    return _detect_sensitivity("\n".join(value for value in texts if value))


def _higher_sensitivity(left: str, right: str) -> KnowledgeSensitivity:
    value = max((left, right), key=_SENSITIVITY_RANK.__getitem__)
    return value  # type: ignore[return-value]


def _safe_error(exc: Exception, *, max_length: int = 500) -> str:
    text = str(exc).replace("\x00", "").strip()
    return (text or exc.__class__.__name__)[:max_length]


def _json_string_list(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str)]


def _fts_query(query: str) -> str:
    query = query.strip()
    terms: list[str] = []
    # Exact phrase first; trigrams then make natural-language requests less
    # brittle without letting user input become FTS syntax.
    if len(query) >= 3:
        terms.append(query)
    for token in re.findall(r"[A-Za-z0-9_./:+-]+|[\u3400-\u9fff]+", query):
        if len(token) < 3:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]+", token) and len(token) > 3:
            terms.extend(token[index : index + 3] for index in range(len(token) - 2))
        else:
            terms.append(token)
    unique = list(dict.fromkeys(terms))[:32]
    if not unique:
        unique = [query]
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in unique)


def _excerpt(content: str, query: str, maximum: int) -> tuple[str, int, int]:
    if len(content) <= maximum:
        return content, 0, len(content)
    position = content.casefold().find(query.casefold())
    if position < 0:
        positions = [
            content.casefold().find(term.casefold())
            for term in re.findall(r"[A-Za-z0-9_./:+-]{3,}|[\u3400-\u9fff]{3,}", query)
        ]
        positions = [value for value in positions if value >= 0]
        position = min(positions) if positions else 0
    start = max(0, position - maximum // 3)
    end = min(len(content), start + maximum)
    start = max(0, end - maximum)
    return content[start:end], start, end


def _cursor_key(signing_key: str | bytes) -> bytes:
    if isinstance(signing_key, str):
        key = signing_key.encode("utf-8")
    elif isinstance(signing_key, bytes):
        key = signing_key
    else:
        raise KnowledgeValidationError("signing_key must be text or bytes")
    if not key:
        raise KnowledgeValidationError("signing_key must not be blank for paginated reads")
    return key


def _encode_cursor(payload: dict[str, Any], signing_key: str | bytes) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    encoded = base64.urlsafe_b64encode(body).rstrip(b"=")
    signature = hmac.new(_cursor_key(signing_key), encoded, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{encoded.decode('ascii')}.{encoded_signature.decode('ascii')}"


def _decode_cursor(cursor: str, signing_key: str | bytes) -> dict[str, Any]:
    if not isinstance(cursor, str) or cursor.count(".") != 1:
        raise KnowledgeValidationError("cursor is invalid")
    try:
        encoded, encoded_signature = (
            part.encode("ascii", "strict") for part in cursor.split(".", 1)
        )
    except UnicodeEncodeError as exc:
        raise KnowledgeValidationError("cursor is invalid") from exc
    expected = hmac.new(_cursor_key(signing_key), encoded, hashlib.sha256).digest()
    try:
        supplied = base64.urlsafe_b64decode(encoded_signature + b"=" * (-len(encoded_signature) % 4))
    except Exception as exc:
        raise KnowledgeValidationError("cursor is invalid") from exc
    if not hmac.compare_digest(expected, supplied):
        raise KnowledgeValidationError("cursor signature is invalid")
    try:
        body = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise KnowledgeValidationError("cursor is invalid") from exc
    if not isinstance(payload, dict):
        raise KnowledgeValidationError("cursor is invalid")
    return payload
