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


def _utc_before(*, hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


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
    # Knowledge documents are long third-party texts: a manual that explains
    # how to reset a password must not be filed as a credential.  Only values
    # (``密码：Abc123``, ``api_key=sk-...``) count as sensitive here; memory
    # keeps the stricter mention-only floor.
    return detect_text_sensitivity(  # type: ignore[return-value]
        text,
        credential_requires_value=True,
        contact_requires_value=True,
    )


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


_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+-]+|[\u3400-\u9fff]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_MAX_FTS_TERMS = 256


def _query_terms(query: str) -> tuple[list[str], list[str]]:
    """Split a natural-language query into trigram-safe and short terms.

    Returns ``(fts_terms, short_terms)``.  ``fts_terms`` are phrases of at
    least three characters that the trigram FTS table can match: the whole
    query, Latin/number tokens, every CJK run of three or more characters,
    and every character trigram inside those runs.  ``short_terms`` are the
    two-character CJK words (``端口``, ``密码``, ``导出``) that carry most of
    the meaning in Chinese queries but are invisible to a trigram index; the
    caller matches them with a substring scan instead of dropping them.
    """
    query = query.strip()
    fts_terms: list[str] = []
    short_terms: list[str] = []
    if len(query) >= 3:
        fts_terms.append(query)
    for token in _QUERY_TOKEN_RE.findall(query):
        if _CJK_RUN_RE.fullmatch(token):
            if len(token) == 2:
                short_terms.append(token)
                continue
            if len(token) < 3:
                continue
            fts_terms.append(token)
            if len(token) > 3:
                fts_terms.extend(
                    token[index : index + 3] for index in range(len(token) - 2)
                )
        elif len(token) >= 3:
            fts_terms.append(token)
    unique_fts = list(dict.fromkeys(fts_terms))[:_MAX_FTS_TERMS]
    unique_short = list(dict.fromkeys(short_terms))[:64]
    return unique_fts, unique_short


def _cjk_bigrams(query: str, *, exclude: Sequence[str] = ()) -> list[str]:
    """Two-character windows of longer CJK runs, used when trigrams miss.

    ``配置端口`` written as one run never matches a document that says
    ``端口……配置``; its bigrams ``配置`` and ``端口`` do.
    """
    excluded = set(exclude)
    bigrams: list[str] = []
    for run in _CJK_RUN_RE.findall(query):
        if len(run) < 4:
            continue
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram not in excluded:
                bigrams.append(bigram)
    return list(dict.fromkeys(bigrams))[:64]


def _fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression; user input never becomes FTS syntax."""
    fts_terms, _ = _query_terms(query)
    if not fts_terms:
        fts_terms = [query.strip()]
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in fts_terms
    )


def _excerpt(
    content: str,
    query: str,
    maximum: int,
    *,
    title_path: str = "",
) -> tuple[str, int, int]:
    if len(content) <= maximum:
        return content, 0, len(content)
    folded = content.casefold()
    position = folded.find(query.casefold())
    if position >= 0:
        start = max(0, position - maximum // 3)
    else:
        start = _densest_window_start(
            folded, query, maximum, title_path=title_path.casefold()
        )
    end = min(len(content), start + maximum)
    start = max(0, end - maximum)
    return content[start:end], start, end


def _densest_window_start(
    folded: str,
    query: str,
    maximum: int,
    *,
    title_path: str = "",
) -> int:
    """Start offset of the ``maximum``-wide window covering the most query terms.

    The earliest-match heuristic used to anchor the excerpt on whatever
    generic word appeared first ("用户", "系统"), which for long requests
    pointed the window at preamble while the answer sat further down the
    chunk.  Counting distinct matched terms per window keeps the excerpt on
    the densest span instead.
    """
    fts_terms, short_terms = _query_terms(query)
    terms = [term.casefold() for term in fts_terms[1:] + short_terms]
    terms = [term for term in terms if term and term != query.casefold()]
    # Words that merely repeat the section heading (a request usually names
    # the document it is asking about) say little about where the answer is;
    # longer phrases and Latin tokens are more specific than CJK trigrams.
    weights: list[float] = []
    for term in terms:
        weight = 2.0 if len(term) >= 4 else 1.0
        if title_path and term in title_path:
            weight *= 0.1
        weights.append(weight)
    matches: list[tuple[int, int]] = []
    for term_index, term in enumerate(terms):
        offset = folded.find(term)
        seen = 0
        while offset >= 0 and seen < 24:
            matches.append((offset, term_index))
            seen += 1
            offset = folded.find(term, offset + 1)
    if not matches:
        return 0
    matches.sort()
    counts: dict[int, int] = {}
    best_start = matches[0][0]
    best_span_end = matches[0][0]
    best_score = 0.0
    left = 0
    for position, term_index in matches:
        counts[term_index] = counts.get(term_index, 0) + 1
        while position - matches[left][0] >= maximum:
            left_index = matches[left][1]
            counts[left_index] -= 1
            if counts[left_index] == 0:
                del counts[left_index]
            left += 1
        score = sum(weights[index] for index in counts)
        if score > best_score:
            best_score = score
            best_start = matches[left][0]
            best_span_end = position
    # Centre the matched span inside the window when there is slack.
    slack = maximum - (best_span_end - best_start + 1)
    return max(0, best_start - max(0, slack) // 3)


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
