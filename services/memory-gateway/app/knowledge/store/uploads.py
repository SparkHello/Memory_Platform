"""Segmented upload lifecycle: begin, append, commit, cancel."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import hmac
from typing import Any

from app.knowledge.models import (
    KnowledgeCommitResult,
    KnowledgeSensitivity,
    KnowledgeUploadPart,
    KnowledgeUploadSession,
)
from app.knowledge.store.constants import (
    _SHA256_RE,
    _UPLOAD_PART_MAX_CHARS,
    _UPLOAD_TTL_HOURS,
)
from app.knowledge.store.errors import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgeSensitivityConfirmationRequired,
    KnowledgeValidationError,
)
from app.knowledge.store.helpers import (
    ConnectionProvider,
    DocumentSizeProvider,
    KnowledgeWriteProvider,
    _document_id,
    _get_document_row,
    _load_document_model,
    _plain_id,
    _require_open_upload,
    _upload_part_from_row,
    _upload_session_from_row,
    _version_from_row,
    _version_id,
)
from app.knowledge.store.utils import (
    _document_ref,
    _detected_sensitivity,
    _json_dump,
    _json_metadata,
    _json_string_list,
    _new_id,
    _optional_text,
    _required_text,
    _utc_after,
    _utc_now,
    _validate_content_type,
    _validate_metadata,
    _validate_sensitivity,
    _validate_tags,
    _version_ref,
)
from app.sensitivity import SENSITIVITY_RANK as _SENSITIVITY_RANK


def begin_upload(
    store: ConnectionProvider,
    user_id: str,
    title: str,
    *,
    content_type: str = "text/markdown",
    source_name: str = "",
    replace_document_ref: str = "",
    sensitivity: KnowledgeSensitivity = "normal",
    tags: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> KnowledgeUploadSession:
    user_id = _required_text(user_id, "user_id", 256)
    title = _required_text(title, "title", 300)
    source_name = _optional_text(source_name, "source_name", 1000)
    content_type = _validate_content_type(content_type)
    sensitivity = _validate_sensitivity(sensitivity)
    validated_tags = _validate_tags(tags) if tags is not None else None
    validated_metadata = _validate_metadata(metadata) if metadata is not None else None
    now = _utc_now()
    expires_at = _utc_after(hours=_UPLOAD_TTL_HOURS)
    replace_id: str | None = None
    expected_version_id: str | None = None

    with store._connect() as connection:
        connection.execute(
            """
            DELETE FROM knowledge_upload_sessions
            WHERE user_id = ? AND status IN ('open', 'expired') AND expires_at < ?
            """,
            (user_id, now),
        )
        if replace_document_ref:
            replace_id = _document_id(replace_document_ref)
            row = _get_document_row(
                connection,
                user_id=user_id,
                document_id=replace_id,
                include_deleted=False,
            )
            expected_version_id = row["current_version_id"]
            if validated_tags is None:
                validated_tags = _json_string_list(row["tags_json"])
            if validated_metadata is None:
                validated_metadata = _json_metadata(row["metadata_json"])
        if validated_tags is None:
            validated_tags = []
        if validated_metadata is None:
            validated_metadata = {}
        upload_id = _new_id()
        connection.execute(
            """
            INSERT INTO knowledge_upload_sessions (
                id, user_id, title, content_type, source_name, sensitivity,
                tags_json, metadata_json,
                replace_document_id, expected_current_version_id, status,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                upload_id,
                user_id,
                title,
                content_type,
                source_name,
                sensitivity,
                _json_dump(validated_tags),
                _json_dump(validated_metadata),
                replace_id,
                expected_version_id,
                now,
                now,
                expires_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM knowledge_upload_sessions WHERE id = ?",
            (upload_id,),
        ).fetchone()
    return _upload_session_from_row(row)


def append_upload(
    store: DocumentSizeProvider,
    user_id: str,
    upload_id: str,
    sequence: int,
    text: str,
) -> KnowledgeUploadPart:
    user_id = _required_text(user_id, "user_id", 256)
    upload_id = _plain_id(upload_id, "upload")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or sequence >= 100_000
    ):
        raise KnowledgeValidationError(
            "sequence must be an integer between 0 and 99999"
        )
    if not isinstance(text, str) or not text:
        raise KnowledgeValidationError("text must not be empty")
    if "\x00" in text:
        raise KnowledgeValidationError("text must not contain NUL")
    if len(text) > _UPLOAD_PART_MAX_CHARS:
        raise KnowledgeValidationError(
            f"upload part must not exceed {_UPLOAD_PART_MAX_CHARS} characters"
        )
    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    now = _utc_now()

    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_open_upload(connection, user_id=user_id, upload_id=upload_id)
        existing = connection.execute(
            """
            SELECT * FROM knowledge_upload_parts
            WHERE upload_id = ? AND sequence = ?
            """,
            (upload_id, sequence),
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != digest or existing["content"] != text:
                raise KnowledgeConflictError(
                    "an upload part with this sequence already has different content"
                )
            return _upload_part_from_row(existing, duplicate=True)

        total = connection.execute(
            """
            SELECT COALESCE(SUM(byte_size), 0) AS total
            FROM knowledge_upload_parts WHERE upload_id = ?
            """,
            (upload_id,),
        ).fetchone()["total"]
        if int(total) + len(encoded) > store.max_document_bytes:
            raise KnowledgeValidationError(
                f"document exceeds {store.max_document_bytes} UTF-8 bytes"
            )
        connection.execute(
            """
            INSERT INTO knowledge_upload_parts (
                upload_id, sequence, content, character_count, byte_size,
                content_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (upload_id, sequence, text, len(text), len(encoded), digest, now),
        )
        connection.execute(
            "UPDATE knowledge_upload_sessions SET updated_at = ? WHERE id = ?",
            (now, upload_id),
        )
        row = connection.execute(
            """
            SELECT * FROM knowledge_upload_parts
            WHERE upload_id = ? AND sequence = ?
            """,
            (upload_id, sequence),
        ).fetchone()
    return _upload_part_from_row(row)


def commit_upload(
    store: KnowledgeWriteProvider,
    user_id: str,
    upload_id: str,
    expected_parts: int,
    expected_sha256: str = "",
    confirm_sensitivity_override: bool = False,
) -> KnowledgeCommitResult:
    user_id = _required_text(user_id, "user_id", 256)
    upload_id = _plain_id(upload_id, "upload")
    if (
        isinstance(expected_parts, bool)
        or not isinstance(expected_parts, int)
        or expected_parts < 1
        or expected_parts > 100_000
    ):
        raise KnowledgeValidationError(
            "expected_parts must be an integer between 1 and 100000"
        )
    if expected_sha256 and not _SHA256_RE.fullmatch(expected_sha256):
        raise KnowledgeValidationError("expected_sha256 must be a 64-character hex digest")

    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT * FROM knowledge_upload_sessions
            WHERE id = ? AND user_id = ?
            """,
            (upload_id, user_id),
        ).fetchone()
        if existing is None:
            raise KnowledgeNotFoundError("upload session not found")
        if existing["status"] == "committed":
            committed_document_id = _document_id(
                existing["committed_document_ref"]
            )
            committed_version_id = _version_id(
                existing["committed_version_ref"]
            )
            version_row = connection.execute(
                "SELECT * FROM knowledge_versions WHERE id = ? AND user_id = ?",
                (committed_version_id, user_id),
            ).fetchone()
            if version_row is None:
                raise KnowledgeNotFoundError("knowledge version not found")
            document_model = _load_document_model(
                connection, user_id=user_id, document_id=committed_document_id
            )
            return KnowledgeCommitResult(
                document=document_model,
                version=_version_from_row(version_row),
                created=False,
                deduplicated=True,
            )
        session = _require_open_upload(
            connection,
            user_id=user_id,
            upload_id=upload_id,
        )
        parts = connection.execute(
            """
            SELECT * FROM knowledge_upload_parts
            WHERE upload_id = ? ORDER BY sequence ASC
            """,
            (upload_id,),
        ).fetchall()
        sequences = [int(row["sequence"]) for row in parts]
        if len(parts) != expected_parts or sequences != list(range(expected_parts)):
            raise KnowledgeConflictError(
                "upload parts must be complete and consecutively numbered from zero"
            )
        content = "".join(row["content"] for row in parts)
        if not content or not content.strip():
            raise KnowledgeValidationError("document content must not be empty")
        encoded = content.encode("utf-8")
        if len(encoded) > store.max_document_bytes:
            raise KnowledgeValidationError(
                f"document exceeds {store.max_document_bytes} UTF-8 bytes"
            )
        content_sha256 = hashlib.sha256(encoded).hexdigest()
        if expected_sha256 and not hmac.compare_digest(
            expected_sha256.lower(), content_sha256
        ):
            raise KnowledgeConflictError("uploaded content SHA-256 does not match")

        declared_sensitivity = _validate_sensitivity(session["sensitivity"])
        detected_sensitivity = _detected_sensitivity(
            session["title"],
            session["source_name"],
            content,
        )
        sensitivity_override_confirmed = (
            _SENSITIVITY_RANK[detected_sensitivity]
            > _SENSITIVITY_RANK[declared_sensitivity]
        )
        if sensitivity_override_confirmed and not confirm_sensitivity_override:
            raise KnowledgeSensitivityConfirmationRequired(
                declared_sensitivity=declared_sensitivity,
                detected_sensitivity=detected_sensitivity,
            )
        sensitivity = declared_sensitivity

        now = _utc_now()
        connection.execute(
            """
            UPDATE knowledge_upload_sessions
            SET status = 'committing', updated_at = ?
            WHERE id = ?
            """,
            (now, upload_id),
        )

        replace_id = session["replace_document_id"]
        created = replace_id is None
        if created:
            document_id = _new_id()
            connection.execute(
                """
                INSERT INTO knowledge_documents (
                    id, user_id, title, source_name, content_type,
                    sensitivity, detected_sensitivity,
                    sensitivity_override_confirmed, tags_json, metadata_json,
                    status, current_version_id,
                    created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, NULL)
                """,
                (
                    document_id,
                    user_id,
                    session["title"],
                    session["source_name"],
                    session["content_type"],
                    sensitivity,
                    detected_sensitivity,
                    int(sensitivity_override_confirmed),
                    session["tags_json"],
                    session["metadata_json"],
                    now,
                    now,
                ),
            )
            current_version_id = None
            next_version = 1
        else:
            document_id = str(replace_id)
            document = _get_document_row(
                connection,
                user_id=user_id,
                document_id=document_id,
                include_deleted=False,
            )
            current_version_id = document["current_version_id"]
            if current_version_id != session["expected_current_version_id"]:
                raise KnowledgeConflictError(
                    "document changed after the upload began; start a new upload"
                )
            current = None
            if current_version_id:
                current = connection.execute(
                    """
                    SELECT * FROM knowledge_versions
                    WHERE id = ? AND document_id = ? AND user_id = ?
                    """,
                    (current_version_id, document_id, user_id),
                ).fetchone()
            if current is not None and current["content_sha256"] == content_sha256:
                connection.execute(
                    """
                    UPDATE knowledge_documents
                    SET title = ?, source_name = ?, content_type = ?,
                        sensitivity = ?, detected_sensitivity = ?,
                        sensitivity_override_confirmed = ?,
                        tags_json = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        session["title"],
                        session["source_name"],
                        session["content_type"],
                        sensitivity,
                        detected_sensitivity,
                        int(sensitivity_override_confirmed),
                        session["tags_json"],
                        session["metadata_json"],
                        now,
                        document_id,
                        user_id,
                    ),
                )
                if current["index_status"] != "ready":
                    # Identical content must not stay unsearchable: rebuild
                    # the index of the existing version instead of creating
                    # a duplicate one.
                    store._index_version_in_connection(
                        connection,
                        user_id=user_id,
                        document_id=document_id,
                        version_id=current["id"],
                        make_current=True,
                    )
                    current = connection.execute(
                        "SELECT * FROM knowledge_versions WHERE id = ?",
                        (current["id"],),
                    ).fetchone()
                connection.execute(
                    """
                    UPDATE knowledge_upload_sessions
                    SET status = 'committed', updated_at = ?,
                        committed_document_ref = ?, committed_version_ref = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        _document_ref(document_id),
                        _version_ref(current["id"]),
                        upload_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM knowledge_upload_parts WHERE upload_id = ?",
                    (upload_id,),
                )
                document_model = _load_document_model(
                    connection, user_id=user_id, document_id=document_id
                )
                return KnowledgeCommitResult(
                    document=document_model,
                    version=_version_from_row(current),
                    created=False,
                    deduplicated=True,
                )
            next_version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1 AS value
                    FROM knowledge_versions WHERE document_id = ? AND user_id = ?
                    """,
                    (document_id, user_id),
                ).fetchone()["value"]
            )
            connection.execute(
                """
                UPDATE knowledge_documents
                SET title = ?, source_name = ?, content_type = ?,
                    sensitivity = ?, detected_sensitivity = ?,
                    sensitivity_override_confirmed = ?,
                    tags_json = ?, metadata_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    session["title"],
                    session["source_name"],
                    session["content_type"],
                    sensitivity,
                    detected_sensitivity,
                    int(sensitivity_override_confirmed),
                    session["tags_json"],
                    session["metadata_json"],
                    now,
                    document_id,
                    user_id,
                ),
            )

        version_id = _new_id()
        connection.execute(
            """
            INSERT INTO knowledge_versions (
                id, document_id, user_id, version_number, content,
                content_sha256, byte_size, character_count, index_status,
                index_error, created_at, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
            """,
            (
                version_id,
                document_id,
                user_id,
                next_version,
                content,
                content_sha256,
                len(encoded),
                len(content),
                now,
            ),
        )
        store._index_version_in_connection(
            connection,
            user_id=user_id,
            document_id=document_id,
            version_id=version_id,
            make_current=True,
        )
        version_row = connection.execute(
            "SELECT * FROM knowledge_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE knowledge_upload_sessions
            SET status = 'committed', updated_at = ?,
                committed_document_ref = ?, committed_version_ref = ?
            WHERE id = ?
            """,
            (
                _utc_now(),
                _document_ref(document_id),
                _version_ref(version_id),
                upload_id,
            ),
        )
        connection.execute(
            "DELETE FROM knowledge_upload_parts WHERE upload_id = ?",
            (upload_id,),
        )
        document_model = _load_document_model(
            connection, user_id=user_id, document_id=document_id
        )
    return KnowledgeCommitResult(
        document=document_model,
        version=_version_from_row(version_row),
        created=created,
        deduplicated=False,
    )


def cancel_upload(store: ConnectionProvider, user_id: str, upload_id: str) -> bool:
    user_id = _required_text(user_id, "user_id", 256)
    upload_id = _plain_id(upload_id, "upload")
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT status FROM knowledge_upload_sessions
            WHERE id = ? AND user_id = ?
            """,
            (upload_id, user_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("upload session not found")
        if row["status"] == "committed":
            raise KnowledgeConflictError("a committed upload cannot be cancelled")
        connection.execute(
            "DELETE FROM knowledge_upload_sessions WHERE id = ? AND user_id = ?",
            (upload_id, user_id),
        )
    return True
