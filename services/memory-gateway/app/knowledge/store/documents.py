"""Document and version management operations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.knowledge.models import (
    KnowledgeCommitResult,
    KnowledgeDocument,
    KnowledgeSensitivity,
    KnowledgeVersion,
)
from app.knowledge.store.errors import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgeValidationError,
)
from app.knowledge.store.helpers import (
    _ConnectableStore,
    _document_from_row,
    _document_id,
    _document_ids,
    _document_select_sql,
    _get_document_row,
    _get_version_row,
    _load_document_model,
    _version_from_row,
    _version_id,
)
from app.knowledge.store.utils import (
    _bounded_int,
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
    _utc_now,
    _validate_metadata,
    _validate_sensitivity,
    _validate_tags,
)
from app.sensitivity import SENSITIVITY_RANK as _SENSITIVITY_RANK


def list_documents(
    store: _ConnectableStore,
    user_id: str,
    query: str = "",
    status: str = "active",
    limit: int = 50,
    include_sensitive: bool = False,
) -> list[KnowledgeDocument]:
    user_id = _required_text(user_id, "user_id", 256)
    query = _optional_text(query, "query", 2000)
    if status not in {"active", "deleted", "all"}:
        raise KnowledgeValidationError("status must be active, deleted, or all")
    limit = _bounded_int(limit, "limit", minimum=1, maximum=1000)
    conditions = ["d.user_id = ?"]
    params: list[Any] = [user_id]
    if status != "all":
        conditions.append("d.status = ?")
        params.append(status)
    if query:
        conditions.append(
            "(instr(lower(d.title), lower(?)) > 0 OR "
            "instr(lower(d.source_name), lower(?)) > 0)"
        )
        params.extend([query, query])
    if not include_sensitive:
        conditions.append("d.sensitivity = 'normal'")
    params.append(limit)
    with store._connect() as connection:
        rows = connection.execute(
            f"""
            {_document_select_sql()}
            WHERE {' AND '.join(conditions)}
            ORDER BY d.updated_at DESC, d.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_document_from_row(row) for row in rows]


def resolve_document_refs(
    store: _ConnectableStore,
    user_id: str,
    *,
    document_refs: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    metadata_filter: dict[str, Any] | None = None,
    include_sensitive: bool = False,
    limit: int = 50,
) -> list[str]:
    """Resolve an authorized document scope using exact local metadata filters."""
    user_id = _required_text(user_id, "user_id", 256)
    supplied_ids = _document_ids(document_refs or [])
    wanted_tags = _validate_tags(tags or [])
    wanted_metadata = _validate_metadata(metadata_filter or {})
    limit = _bounded_int(limit, "limit", minimum=1, maximum=1000)
    conditions = ["user_id = ?", "status = 'active'"]
    params: list[Any] = [user_id]
    if not include_sensitive:
        conditions.append("sensitivity = 'normal'")
    if supplied_ids:
        placeholders = ",".join("?" for _ in supplied_ids)
        conditions.append(f"id IN ({placeholders})")
        params.extend(supplied_ids)
    params.append(limit)
    with store._connect() as connection:
        rows = connection.execute(
            f"""
            SELECT id, tags_json, metadata_json
            FROM knowledge_documents
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC, id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    result: list[str] = []
    wanted_tag_set = set(wanted_tags)
    for row in rows:
        row_tags = set(_json_string_list(row["tags_json"]))
        row_metadata = _json_metadata(row["metadata_json"])
        if wanted_tag_set and not wanted_tag_set.issubset(row_tags):
            continue
        if any(row_metadata.get(key) != value for key, value in wanted_metadata.items()):
            continue
        result.append(_document_ref(row["id"]))
    return result


def get_document_detail(
    store: _ConnectableStore,
    user_id: str,
    document_id: str = "",
    *,
    document_ref: str = "",
    include_content: bool = False,
    include_sensitive: bool = True,
) -> dict[str, Any]:
    user_id = _required_text(user_id, "user_id", 256)
    document_id = _document_id(
        _one_reference(document_id, document_ref, "document")
    )
    with store._connect() as connection:
        document = _load_document_model(
            connection, user_id=user_id, document_id=document_id
        )
        if not include_sensitive and document.sensitivity != "normal":
            raise KnowledgeNotFoundError("knowledge document not found")
        rows = connection.execute(
            """
            SELECT * FROM knowledge_versions
            WHERE user_id = ? AND document_id = ?
            ORDER BY version_number DESC
            """,
            (user_id, document_id),
        ).fetchall()
    versions = [_version_from_row(row, include_content=include_content) for row in rows]
    return {"document": document, "versions": versions}


def get_version(
    store: _ConnectableStore,
    user_id: str,
    version_id: str,
    *,
    include_content: bool = False,
    include_sensitive: bool = True,
) -> KnowledgeVersion:
    user_id = _required_text(user_id, "user_id", 256)
    version_id = _version_id(version_id)
    with store._connect() as connection:
        row = _get_version_row(
            connection,
            user_id=user_id,
            version_id=version_id,
            active_document=False,
            include_sensitive=include_sensitive,
        )
    return _version_from_row(row, include_content=include_content)


def update_document(
    store: _ConnectableStore,
    user_id: str,
    document_id: str = "",
    *,
    document_ref: str = "",
    title: str | None = None,
    source_name: str | None = None,
    sensitivity: KnowledgeSensitivity | None = None,
    tags: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> KnowledgeDocument:
    user_id = _required_text(user_id, "user_id", 256)
    document_id = _document_id(
        _one_reference(document_id, document_ref, "document")
    )
    if (
        title is None
        and source_name is None
        and sensitivity is None
        and tags is None
        and metadata is None
    ):
        raise KnowledgeValidationError("at least one document field must be supplied")
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _get_document_row(
            connection,
            user_id=user_id,
            document_id=document_id,
            include_deleted=False,
        )
        new_title = row["title"] if title is None else _required_text(title, "title", 300)
        new_source = (
            row["source_name"]
            if source_name is None
            else _optional_text(source_name, "source_name", 1000)
        )
        declared = row["sensitivity"] if sensitivity is None else _validate_sensitivity(sensitivity)
        new_tags = (
            _json_string_list(row["tags_json"])
            if tags is None
            else _validate_tags(tags)
        )
        new_metadata = (
            _json_metadata(row["metadata_json"])
            if metadata is None
            else _validate_metadata(metadata)
        )
        content_rows = connection.execute(
            """
            SELECT content FROM knowledge_versions
            WHERE user_id = ? AND document_id = ?
            """,
            (user_id, document_id),
        ).fetchall()
        detected_sensitivity = _detected_sensitivity(
            new_title,
            new_source,
            *(item["content"] for item in content_rows),
        )
        preserve_confirmed_override = bool(
            row["sensitivity_override_confirmed"]
        ) and (sensitivity is None or declared == row["sensitivity"])
        if preserve_confirmed_override:
            new_sensitivity = _validate_sensitivity(row["sensitivity"])
            sensitivity_override_confirmed = (
                _SENSITIVITY_RANK[detected_sensitivity]
                > _SENSITIVITY_RANK[new_sensitivity]
            )
        else:
            new_sensitivity = _higher_sensitivity(
                declared, detected_sensitivity
            )
            sensitivity_override_confirmed = False
        connection.execute(
            """
            UPDATE knowledge_documents
            SET title = ?, source_name = ?, sensitivity = ?,
                detected_sensitivity = ?,
                sensitivity_override_confirmed = ?,
                tags_json = ?, metadata_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                new_title,
                new_source,
                new_sensitivity,
                detected_sensitivity,
                int(sensitivity_override_confirmed),
                _json_dump(new_tags),
                _json_dump(new_metadata),
                _utc_now(),
                document_id,
                user_id,
            ),
        )
        model = _load_document_model(
            connection, user_id=user_id, document_id=document_id
        )
    return model


def soft_delete_document(
    store: _ConnectableStore,
    user_id: str,
    document_id: str = "",
    *,
    document_ref: str = "",
    confirm_document_ref: str = "",
) -> KnowledgeDocument:
    user_id = _required_text(user_id, "user_id", 256)
    document_id = _document_id(
        _one_reference(document_id, document_ref, "document")
    )
    if confirm_document_ref and confirm_document_ref != _document_ref(document_id):
        raise KnowledgeConflictError("confirm_document_ref does not match")
    now = _utc_now()
    with store._connect() as connection:
        _get_document_row(
            connection,
            user_id=user_id,
            document_id=document_id,
            include_deleted=False,
        )
        connection.execute(
            """
            UPDATE knowledge_documents
            SET status = 'deleted', deleted_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, now, document_id, user_id),
        )
        model = _load_document_model(
            connection, user_id=user_id, document_id=document_id
        )
    return model


def restore_document(
    store: _ConnectableStore,
    user_id: str,
    document_id: str = "",
    *,
    document_ref: str = "",
) -> KnowledgeDocument:
    user_id = _required_text(user_id, "user_id", 256)
    document_id = _document_id(
        _one_reference(document_id, document_ref, "document")
    )
    with store._connect() as connection:
        row = _get_document_row(
            connection,
            user_id=user_id,
            document_id=document_id,
            include_deleted=True,
        )
        if row["status"] != "deleted":
            raise KnowledgeConflictError("knowledge document is not deleted")
        connection.execute(
            """
            UPDATE knowledge_documents
            SET status = 'active', deleted_at = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (_utc_now(), document_id, user_id),
        )
        model = _load_document_model(
            connection, user_id=user_id, document_id=document_id
        )
    return model


def purge_document(
    store: _ConnectableStore,
    user_id: str,
    document_id: str = "",
    *,
    document_ref: str = "",
    confirm_document_ref: str = "",
    confirm_document_id: str = "",
) -> bool:
    user_id = _required_text(user_id, "user_id", 256)
    supplied_reference = _one_reference(document_id, document_ref, "document")
    document_id = _document_id(supplied_reference)
    confirmation = _one_reference(
        confirm_document_id,
        confirm_document_ref,
        "document confirmation",
    )
    if _document_id(confirmation) != document_id:
        raise KnowledgeConflictError("the complete document id or reference is required to purge")
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _get_document_row(
            connection,
            user_id=user_id,
            document_id=document_id,
            include_deleted=True,
        )
        if row["status"] != "deleted":
            raise KnowledgeConflictError("only a deleted knowledge document can be purged")
        connection.execute(
            "DELETE FROM knowledge_chunks_fts WHERE user_id = ? AND document_id = ?",
            (user_id, document_id),
        )
        connection.execute(
            "DELETE FROM knowledge_documents WHERE id = ? AND user_id = ?",
            (document_id, user_id),
        )
    return True


def restore_version(
    store: _ConnectableStore,
    user_id: str,
    document_id: str = "",
    version_id: str = "",
    *,
    document_ref: str = "",
    version_ref: str = "",
) -> KnowledgeCommitResult:
    user_id = _required_text(user_id, "user_id", 256)
    document_id = _document_id(
        _one_reference(document_id, document_ref, "document")
    )
    version_id = _version_id(_one_reference(version_id, version_ref, "version"))
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        document = _get_document_row(
            connection,
            user_id=user_id,
            document_id=document_id,
            include_deleted=False,
        )
        source = connection.execute(
            """
            SELECT * FROM knowledge_versions
            WHERE id = ? AND document_id = ? AND user_id = ?
            """,
            (version_id, document_id, user_id),
        ).fetchone()
        if source is None:
            raise KnowledgeNotFoundError("knowledge version not found")
        if source["index_status"] != "ready":
            raise KnowledgeConflictError("only a ready version can be restored")
        next_version = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1 AS value
                FROM knowledge_versions WHERE document_id = ? AND user_id = ?
                """,
                (document_id, user_id),
            ).fetchone()["value"]
        )
        new_version_id = _new_id()
        now = _utc_now()
        content = source["content"]
        detected_sensitivity = _detected_sensitivity(
            document["title"], document["source_name"], content
        )
        sensitivity = _validate_sensitivity(document["sensitivity"])
        sensitivity_override_confirmed = bool(
            document["sensitivity_override_confirmed"]
        ) and (
            _SENSITIVITY_RANK[detected_sensitivity]
            > _SENSITIVITY_RANK[sensitivity]
        )
        if not sensitivity_override_confirmed:
            sensitivity = _higher_sensitivity(
                sensitivity, detected_sensitivity
            )
        connection.execute(
            """
            INSERT INTO knowledge_versions (
                id, document_id, user_id, version_number, content,
                content_sha256, byte_size, character_count, index_status,
                index_error, created_at, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
            """,
            (
                new_version_id,
                document_id,
                user_id,
                next_version,
                content,
                source["content_sha256"],
                source["byte_size"],
                source["character_count"],
                now,
            ),
        )
        connection.execute(
            """
            UPDATE knowledge_documents
            SET sensitivity = ?, detected_sensitivity = ?,
                sensitivity_override_confirmed = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                sensitivity,
                detected_sensitivity,
                int(sensitivity_override_confirmed),
                now,
                document_id,
                user_id,
            ),
        )
        store._index_version_in_connection(
            connection,
            user_id=user_id,
            document_id=document_id,
            version_id=new_version_id,
            make_current=True,
        )
        version_row = connection.execute(
            "SELECT * FROM knowledge_versions WHERE id = ?",
            (new_version_id,),
        ).fetchone()
        document_model = _load_document_model(
            connection, user_id=user_id, document_id=document_id
        )
    return KnowledgeCommitResult(
        document=document_model,
        version=_version_from_row(version_row),
        created=False,
        deduplicated=False,
    )


def reindex_version(
    store: _ConnectableStore,
    user_id: str,
    version_id: str = "",
    *,
    document_id: str = "",
    document_ref: str = "",
    version_ref: str = "",
) -> KnowledgeCommitResult:
    user_id = _required_text(user_id, "user_id", 256)
    version_id = _version_id(_one_reference(version_id, version_ref, "version"))
    supplied_document = document_id or document_ref
    expected_document_id = _document_id(supplied_document) if supplied_document else ""
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _get_version_row(
            connection,
            user_id=user_id,
            version_id=version_id,
            active_document=False,
            include_sensitive=True,
        )
        if expected_document_id and row["document_id"] != expected_document_id:
            raise KnowledgeNotFoundError("knowledge version not found")
        document = _get_document_row(
            connection,
            user_id=user_id,
            document_id=row["document_id"],
            include_deleted=False,
        )
        make_current = document["current_version_id"] == version_id
        if not make_current:
            ready = connection.execute(
                """
                SELECT 1 FROM knowledge_versions
                WHERE document_id = ? AND user_id = ? AND index_status = 'ready'
                LIMIT 1
                """,
                (row["document_id"], user_id),
            ).fetchone()
            # Without any ready version the document would otherwise stay
            # unsearchable; only then does a reindexed version take over.
            make_current = ready is None
        store._index_version_in_connection(
            connection,
            user_id=user_id,
            document_id=row["document_id"],
            version_id=version_id,
            make_current=make_current,
        )
        result = connection.execute(
            "SELECT * FROM knowledge_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        document_model = _load_document_model(
            connection, user_id=user_id, document_id=row["document_id"]
        )
    return KnowledgeCommitResult(
        document=document_model,
        version=_version_from_row(result),
        created=False,
        deduplicated=False,
    )
