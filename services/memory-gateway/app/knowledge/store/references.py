"""Exact reference reads with signed pagination cursors."""

from __future__ import annotations

from typing import Any

from app.knowledge.chunking import _last_touched_line, _line_at
from app.knowledge.store.constants import (
    _CHUNK_PREFIX,
    _READ_MAX_CHARS,
    _VERSION_PREFIX,
)
from app.knowledge.store.errors import KnowledgeValidationError
from app.knowledge.store.helpers import (
    _ConnectableStore,
    _chunk_id,
    _get_chunk_row,
    _get_version_row,
    _version_id,
)
from app.knowledge.store.utils import (
    _bounded_int,
    _chunk_ref,
    _decode_cursor,
    _document_ref,
    _encode_cursor,
    _json_string_list,
    _required_text,
    _version_ref,
)


def read_reference(
    store: _ConnectableStore,
    user_id: str,
    reference: str,
    cursor: str = "",
    max_chars: int = 12_000,
    include_sensitive: bool = False,
    signing_key: str | bytes = "",
) -> dict[str, Any]:
    user_id = _required_text(user_id, "user_id", 256)
    max_chars = _bounded_int(max_chars, "max_chars", minimum=1, maximum=_READ_MAX_CHARS)
    if not isinstance(reference, str):
        raise KnowledgeValidationError("reference must be a string")
    if not isinstance(cursor, str) or len(cursor) > 4000:
        raise KnowledgeValidationError("cursor must not exceed 4000 characters")
    if reference.startswith(_CHUNK_PREFIX):
        if cursor:
            raise KnowledgeValidationError("chunk references do not accept a cursor")
        chunk_id = _chunk_id(reference)
        with store._connect() as connection:
            row = _get_chunk_row(
                connection,
                user_id=user_id,
                chunk_id=chunk_id,
                include_sensitive=include_sensitive,
            )
        content = row["content"]
        return {
            "reference": _chunk_ref(row["id"]),
            "document_ref": _document_ref(row["document_id"]),
            "version_ref": _version_ref(row["version_id"]),
            "chunk_ref": _chunk_ref(row["id"]),
            "title": row["title"],
            "title_path": _json_string_list(row["title_path_json"]),
            "content": content,
            "char_start": int(row["char_start"]),
            "char_end": int(row["char_end"]),
            "line_start": int(row["line_start"]),
            "line_end": int(row["line_end"]),
            "complete": True,
            "next_cursor": "",
        }
    if not reference.startswith(_VERSION_PREFIX):
        raise KnowledgeValidationError("reference must be a version or chunk reference")
    version_id = _version_id(reference)
    with store._connect() as connection:
        row = _get_version_row(
            connection,
            user_id=user_id,
            version_id=version_id,
            active_document=True,
            include_sensitive=include_sensitive,
        )
        title_row = connection.execute(
            """
            SELECT title FROM knowledge_documents
            WHERE id = ? AND user_id = ? AND status = 'active'
            """,
            (row["document_id"], user_id),
        ).fetchone()
    content = row["content"]
    offset = 0
    if cursor:
        payload = _decode_cursor(cursor, signing_key)
        if (
            payload.get("u") != user_id
            or payload.get("r") != _version_ref(version_id)
            or not isinstance(payload.get("o"), int)
        ):
            raise KnowledgeValidationError("cursor does not match this read request")
        offset = payload["o"]
        if offset < 0 or offset > len(content):
            raise KnowledgeValidationError("cursor offset is invalid")
    end = min(len(content), offset + max_chars)
    page = content[offset:end]
    complete = end >= len(content)
    next_cursor = ""
    if not complete:
        next_cursor = _encode_cursor(
            {"u": user_id, "r": _version_ref(version_id), "o": end},
            signing_key,
        )
    return {
        "reference": _version_ref(version_id),
        "document_ref": _document_ref(row["document_id"]),
        "version_ref": _version_ref(version_id),
        "chunk_ref": "",
        "title": title_row["title"] if title_row is not None else "",
        "title_path": [],
        "content": page,
        "char_start": offset,
        "char_end": end,
        "line_start": _line_at(content, offset),
        "line_end": _last_touched_line(content, offset, end),
        "complete": complete,
        "next_cursor": next_cursor,
    }
