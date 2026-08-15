"""Independent knowledge export and restore."""

from __future__ import annotations

import hashlib
from typing import Any

from app.knowledge.models import KnowledgeDocument, KnowledgeVersion
from app.knowledge.store.errors import KnowledgeValidationError
from app.knowledge.store.helpers import (
    _ConnectableStore,
    _document_id,
    _get_document_row,
    _load_document_model,
    _version_from_row,
)
from app.knowledge.store.utils import (
    _detected_sensitivity,
    _document_ref,
    _higher_sensitivity,
    _json_dump,
    _json_metadata,
    _json_string_list,
    _new_id,
    _one_reference,
    _optional_text,
    _required_text,
    _safe_exported_time,
    _utc_now,
    _validate_content_type,
    _validate_metadata,
    _validate_sensitivity,
    _validate_tags,
    _version_ref,
)
from app.sensitivity import SENSITIVITY_RANK as _SENSITIVITY_RANK


def list_versions(
    store: _ConnectableStore,
    user_id: str,
    document_id: str = "",
    *,
    document_ref: str = "",
    include_content: bool = False,
) -> list[KnowledgeVersion]:
    user_id = _required_text(user_id, "user_id", 256)
    document_id = _document_id(
        _one_reference(document_id, document_ref, "document")
    )
    with store._connect() as connection:
        _get_document_row(
            connection,
            user_id=user_id,
            document_id=document_id,
            include_deleted=True,
        )
        rows = connection.execute(
            """
            SELECT * FROM knowledge_versions
            WHERE user_id = ? AND document_id = ?
            ORDER BY version_number ASC
            """,
            (user_id, document_id),
        ).fetchall()
    return [
        _version_from_row(row, include_content=include_content) for row in rows
    ]


def export_user(store: _ConnectableStore, user_id: str) -> dict[str, Any]:
    """Export canonical knowledge data, never derived chunks or FTS rows."""
    user_id = _required_text(user_id, "user_id", 256)
    documents: list[dict[str, Any]] = []
    with store._connect() as connection:
        document_rows = connection.execute(
            """
            SELECT * FROM knowledge_documents
            WHERE user_id = ? ORDER BY created_at ASC, id ASC
            """,
            (user_id,),
        ).fetchall()
        for row in document_rows:
            version_rows = connection.execute(
                """
                SELECT * FROM knowledge_versions
                WHERE user_id = ? AND document_id = ?
                ORDER BY version_number ASC
                """,
                (user_id, row["id"]),
            ).fetchall()
            current_number = None
            if row["current_version_id"]:
                current = next(
                    (
                        version
                        for version in version_rows
                        if version["id"] == row["current_version_id"]
                    ),
                    None,
                )
                current_number = int(current["version_number"]) if current else None
            documents.append(
                {
                    "source_document_ref": _document_ref(row["id"]),
                    "title": row["title"],
                    "source_name": row["source_name"],
                    "content_type": row["content_type"],
                    "sensitivity": row["sensitivity"],
                    "detected_sensitivity": row["detected_sensitivity"],
                    "sensitivity_override_confirmed": bool(
                        row["sensitivity_override_confirmed"]
                    ),
                    "tags": _json_string_list(row["tags_json"]),
                    "metadata": _json_metadata(row["metadata_json"]),
                    "status": row["status"],
                    "current_version_number": current_number,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "deleted_at": row["deleted_at"],
                    "versions": [
                        {
                            "source_version_ref": _version_ref(version["id"]),
                            "version_number": int(version["version_number"]),
                            "content": version["content"],
                            "content_sha256": version["content_sha256"],
                            "byte_size": int(version["byte_size"]),
                            "character_count": int(version["character_count"]),
                            "index_status": version["index_status"],
                            "index_error": version["index_error"],
                            "created_at": version["created_at"],
                            "indexed_at": version["indexed_at"],
                        }
                        for version in version_rows
                    ],
                }
            )
    return {
        "format": "memory-gateway-knowledge",
        "schema_version": 3,
        "exported_at": _utc_now(),
        "documents": documents,
    }


def restore_export(
    store: _ConnectableStore, user_id: str, export_data: dict[str, Any]
) -> dict[str, Any]:
    """Restore an export under ``user_id`` and rebuild every derived index."""
    user_id = _required_text(user_id, "user_id", 256)
    if not isinstance(export_data, dict):
        raise KnowledgeValidationError("knowledge export must be an object")
    payload = export_data
    if isinstance(payload.get("knowledge"), dict):
        payload = payload["knowledge"]
    documents_value = payload.get("documents")
    if not isinstance(documents_value, list):
        raise KnowledgeValidationError("knowledge export documents must be a list")
    if len(documents_value) > 10_000:
        raise KnowledgeValidationError("knowledge export contains too many documents")
    prepared = [_validate_import_document(store, value) for value in documents_value]
    total_bytes = sum(
        len(version["content"].encode("utf-8"))
        for item in prepared
        for version in item["versions"]
    )
    # Resolve through the package namespace so tests can monkeypatch the limit
    # on app.knowledge.store.
    from app.knowledge import store as store_package

    if total_bytes > store_package._MAX_RESTORE_TOTAL_BYTES:
        raise KnowledgeValidationError("knowledge export data is too large")

    restored_documents: list[KnowledgeDocument] = []
    restored_versions = 0
    failed_versions = 0
    skipped_documents = 0
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for item in prepared:
            source_ref = item["source_document_ref"]
            if source_ref:
                existing = connection.execute(
                    """
                    SELECT id FROM knowledge_documents
                    WHERE user_id = ? AND source_document_ref = ?
                      AND status != 'deleted'
                    """,
                    (user_id, source_ref),
                ).fetchone()
                if existing is not None:
                    skipped_documents += 1
                    continue
            document_id = _new_id()
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO knowledge_documents (
                    id, user_id, title, source_name, content_type,
                    sensitivity, detected_sensitivity,
                    sensitivity_override_confirmed, tags_json, metadata_json,
                    status, current_version_id,
                    created_at, updated_at, deleted_at, source_document_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, NULL, ?)
                """,
                (
                    document_id,
                    user_id,
                    item["title"],
                    item["source_name"],
                    item["content_type"],
                    item["sensitivity"],
                    item["detected_sensitivity"],
                    int(item["sensitivity_override_confirmed"]),
                    _json_dump(item["tags"]),
                    _json_dump(item["metadata"]),
                    item["created_at"] or now,
                    now,
                    source_ref,
                ),
            )
            version_ids: dict[int, str] = {}
            for version in item["versions"]:
                version_id = _new_id()
                version_ids[version["version_number"]] = version_id
                content = version["content"]
                encoded = content.encode("utf-8")
                connection.execute(
                    """
                    INSERT INTO knowledge_versions (
                        id, document_id, user_id, version_number, content,
                        content_sha256, byte_size, character_count,
                        index_status, index_error, created_at, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
                    """,
                    (
                        version_id,
                        document_id,
                        user_id,
                        version["version_number"],
                        content,
                        hashlib.sha256(encoded).hexdigest(),
                        len(encoded),
                        len(content),
                        version["created_at"] or now,
                    ),
                )
                store._index_version_in_connection(
                    connection,
                    user_id=user_id,
                    document_id=document_id,
                    version_id=version_id,
                    make_current=False,
                )
                restored_versions += 1
                index_status = connection.execute(
                    "SELECT index_status FROM knowledge_versions WHERE id = ?",
                    (version_id,),
                ).fetchone()["index_status"]
                if index_status == "failed":
                    failed_versions += 1

            current_id = version_ids.get(item["current_version_number"])
            if current_id:
                current_status = connection.execute(
                    "SELECT index_status FROM knowledge_versions WHERE id = ?",
                    (current_id,),
                ).fetchone()["index_status"]
                if current_status != "ready":
                    current_id = None
            deleted = item["status"] == "deleted"
            connection.execute(
                """
                UPDATE knowledge_documents
                SET current_version_id = ?, status = ?, deleted_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    current_id,
                    "deleted" if deleted else "active",
                    item["deleted_at"] or now if deleted else None,
                    now,
                    document_id,
                    user_id,
                ),
            )
            restored_documents.append(
                _load_document_model(
                    connection, user_id=user_id, document_id=document_id
                )
            )
    return {
        "restored_documents": len(restored_documents),
        "restored_versions": restored_versions,
        "failed_versions": failed_versions,
        "skipped_documents": skipped_documents,
        "document_refs": [item.ref for item in restored_documents],
        "chunks_rebuilt": True,
        "fts_rebuilt": True,
    }


def _validate_import_document(
    store: _ConnectableStore, value: Any
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KnowledgeValidationError("each exported knowledge document must be an object")
    title = _required_text(value.get("title"), "title", 500)
    source_name = _optional_text(value.get("source_name", ""), "source_name", 1000)
    source_document_ref = value.get("source_document_ref", "")
    if not isinstance(source_document_ref, str) or len(source_document_ref) > 300:
        raise KnowledgeValidationError("exported source_document_ref is invalid")
    content_type = _validate_content_type(value.get("content_type", "text/markdown"))
    declared = _validate_sensitivity(value.get("sensitivity", "normal"))
    tags = _validate_tags(value.get("tags", []))
    metadata = _validate_metadata(value.get("metadata", {}))
    status = value.get("status", "active")
    if status not in {"active", "deleted"}:
        raise KnowledgeValidationError("exported document status is invalid")
    versions_value = value.get("versions")
    if not isinstance(versions_value, list) or not versions_value:
        raise KnowledgeValidationError("exported document versions must be a non-empty list")
    if len(versions_value) > 100_000:
        raise KnowledgeValidationError("exported document contains too many versions")
    versions: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    for raw_version in versions_value:
        if not isinstance(raw_version, dict):
            raise KnowledgeValidationError("each exported knowledge version must be an object")
        number = raw_version.get("version_number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise KnowledgeValidationError("exported version_number must be positive")
        if number in seen_numbers:
            raise KnowledgeValidationError("exported version numbers must be unique")
        seen_numbers.add(number)
        content = raw_version.get("content")
        if not isinstance(content, str) or not content:
            raise KnowledgeValidationError("exported version content must not be empty")
        encoded = content.encode("utf-8")
        if len(encoded) > store.max_document_bytes:
            raise KnowledgeValidationError(
                f"document exceeds {store.max_document_bytes} UTF-8 bytes"
            )
        versions.append(
            {
                "version_number": number,
                "content": content,
                "created_at": _safe_exported_time(raw_version.get("created_at")),
            }
        )
    versions.sort(key=lambda item: item["version_number"])
    current_number = value.get("current_version_number")
    if current_number is None:
        current_number = versions[-1]["version_number"]
    if isinstance(current_number, bool) or not isinstance(current_number, int):
        raise KnowledgeValidationError("current_version_number must be an integer")
    if current_number not in seen_numbers:
        raise KnowledgeValidationError("current_version_number is not present in versions")
    detected_sensitivity = _detected_sensitivity(
        title,
        source_name,
        *(item["content"] for item in versions),
    )
    raw_override = value.get("sensitivity_override_confirmed", False)
    if not isinstance(raw_override, bool):
        raise KnowledgeValidationError(
            "sensitivity_override_confirmed must be a boolean"
        )
    sensitivity_override_confirmed = raw_override and (
        _SENSITIVITY_RANK[detected_sensitivity]
        > _SENSITIVITY_RANK[declared]
    )
    sensitivity = (
        declared
        if sensitivity_override_confirmed
        else _higher_sensitivity(declared, detected_sensitivity)
    )
    return {
        "title": title,
        "source_name": source_name,
        "source_document_ref": source_document_ref,
        "content_type": content_type,
        "sensitivity": sensitivity,
        "detected_sensitivity": detected_sensitivity,
        "sensitivity_override_confirmed": sensitivity_override_confirmed,
        "tags": tags,
        "metadata": metadata,
        "status": status,
        "current_version_number": current_number,
        "created_at": _safe_exported_time(value.get("created_at")),
        "deleted_at": _safe_exported_time(value.get("deleted_at")),
        "versions": versions,
    }
