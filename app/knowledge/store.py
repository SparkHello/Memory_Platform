from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
import base64
import hashlib
import hmac
import json
import re
import sqlite3
from typing import Any, Final
from uuid import uuid4

from app.knowledge.chunking import chunk_knowledge_text
from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeCommitResult,
    KnowledgeDocument,
    KnowledgeSearchHit,
    KnowledgeSensitivity,
    KnowledgeUploadPart,
    KnowledgeUploadSession,
    KnowledgeVersion,
)


_DOCUMENT_PREFIX: Final = "knowledge://document/"
_VERSION_PREFIX: Final = "knowledge://version/"
_CHUNK_PREFIX: Final = "knowledge://chunk/"
_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTENT_TYPES: Final = {"text/plain", "text/markdown"}
_SENSITIVITIES: Final = {"normal", "private", "sensitive"}
_SENSITIVITY_RANK: Final = {"normal": 0, "private": 1, "sensitive": 2}
_UPLOAD_PART_MAX_CHARS: Final = 1_048_576
_UPLOAD_TTL_HOURS: Final = 24
_READ_MAX_CHARS: Final = 20_000
_SEARCH_MAX_RESULTS: Final = 20
_SEARCH_EXCERPT_CHARS: Final = 800

# This deliberately small, deterministic floor protects the most common
# credential and personal-data forms without involving a remote model.  It is
# enforced again at the storage boundary, including metadata-only updates and
# historical-version restores.
_SENSITIVE_PATTERNS: Final = (
    re.compile(
        r"密码|口令|验证码|密钥|私钥|助记词|身份证|护照号|社保号|驾驶证号|"
        r"健康隐私|病历|确诊|诊断|疾病|患有|过敏|用药|药物|处方|病史|症状|治疗|"
        r"手术|血糖|血压|心率|糖尿病|癌症|抑郁症|焦虑症|银行卡|信用卡|银行账户|"
        r"银行账号|支付账号|账户余额|家庭住址|家庭地址|详细地址|门牌号|收货地址",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:password|passcode|pin\s*(?:code)?|otp|api[-_ ]?key|"
        r"access[-_ ]?token|secret[-_ ]?key|private[-_ ]?key|seed phrase|"
        r"passport (?:number|no\.?|id)|social security|ssn|medical|"
        r"diagnos(?:is|ed)|disease|allerg(?:y|ic)|medication|prescription|"
        r"credit card|debit card|bank account|account balance|home address|"
        r"street address)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret)\b\s*[:=]"),
    re.compile(r"(?i)\b(?:sk|pk|token)[-_][A-Za-z0-9_-]{4,}\b"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\b\d{15,19}\b"),
    re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:省|市|区|县).{0,20}(?:路|街|道|巷|弄).{0,10}\d+\s*号"),
    re.compile(
        r"\b\d{1,6}\s+[A-Za-z][A-Za-z .'-]{1,40}\s+(?:Street|St|Road|Rd|Avenue|Ave)\b",
        re.IGNORECASE,
    ),
)
_PRIVATE_PATTERNS: Final = (
    re.compile(
        r"手机号|电话号码|电子邮箱|邮箱地址|工资|收入|债务|负债|"
        r"\b(?:phone number|e-?mail address|salary|income|debt)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
)


class KnowledgeError(Exception):
    """Base exception for the isolated knowledge subsystem."""


class KnowledgeValidationError(KnowledgeError, ValueError):
    """The caller supplied malformed or unsafe input."""


class KnowledgeNotFoundError(KnowledgeError, LookupError):
    """A record is missing, belongs to another user, or is not readable."""


class KnowledgeConflictError(KnowledgeError):
    """The requested mutation conflicts with persistent state."""


class _ClosingSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class KnowledgeStore:
    """SQLite store for versioned long-form knowledge.

    This class owns no memory-store object and never reads or writes the memory
    database.  Every public record operation is scoped by ``user_id``.
    """

    def __init__(
        self,
        database_path: str,
        max_document_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        if not database_path or not str(database_path).strip():
            raise KnowledgeValidationError("database_path must not be blank")
        if max_document_bytes <= 0:
            raise KnowledgeValidationError("max_document_bytes must be positive")
        self.database_path = str(database_path)
        self.max_document_bytes = int(max_document_bytes)

    def init_db(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT 'text/markdown',
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'active',
                    current_version_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    CHECK (content_type IN ('text/plain', 'text/markdown')),
                    CHECK (sensitivity IN ('normal', 'private', 'sensitive')),
                    CHECK (status IN ('active', 'deleted'))
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_user_status
                    ON knowledge_documents(user_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    character_count INTEGER NOT NULL,
                    index_status TEXT NOT NULL DEFAULT 'pending',
                    index_error TEXT,
                    created_at TEXT NOT NULL,
                    indexed_at TEXT,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    UNIQUE(document_id, version_number),
                    CHECK (version_number >= 1),
                    CHECK (byte_size >= 0),
                    CHECK (character_count >= 0),
                    CHECK (index_status IN ('pending', 'indexing', 'ready', 'failed'))
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_versions_user_document
                    ON knowledge_versions(user_id, document_id, version_number DESC);
                CREATE INDEX IF NOT EXISTS idx_knowledge_versions_user_index_status
                    ON knowledge_versions(user_id, index_status, created_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    title_path_json TEXT NOT NULL DEFAULT '[]',
                    char_start INTEGER NOT NULL,
                    char_end INTEGER NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(version_id) REFERENCES knowledge_versions(id) ON DELETE CASCADE,
                    UNIQUE(version_id, ordinal),
                    CHECK (ordinal >= 0),
                    CHECK (char_start >= 0 AND char_end >= char_start),
                    CHECK (line_start >= 1 AND line_end >= line_start)
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_user_version
                    ON knowledge_chunks(user_id, version_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_user_document
                    ON knowledge_chunks(user_id, document_id, version_id);

                CREATE TABLE IF NOT EXISTS knowledge_upload_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    replace_document_id TEXT,
                    expected_current_version_id TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    committed_document_ref TEXT NOT NULL DEFAULT '',
                    committed_version_ref TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(replace_document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    CHECK (status IN ('open', 'committing', 'committed', 'failed', 'expired'))
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_upload_sessions_user_status
                    ON knowledge_upload_sessions(user_id, status, expires_at);

                CREATE TABLE IF NOT EXISTS knowledge_upload_parts (
                    upload_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    character_count INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(upload_id, sequence),
                    FOREIGN KEY(upload_id) REFERENCES knowledge_upload_sessions(id) ON DELETE CASCADE,
                    CHECK (sequence >= 0),
                    CHECK (character_count >= 0),
                    CHECK (byte_size >= 0)
                );
                """
            )
            # A contentful FTS table makes reindex and cascading purge explicit
            # and reliable.  The canonical text remains knowledge_chunks.
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    user_id UNINDEXED,
                    document_id UNINDEXED,
                    version_id UNINDEXED,
                    content,
                    title_path,
                    tokenize='trigram'
                )
                """
            )

    # ------------------------------------------------------------------
    # Upload lifecycle

    def begin_upload(
        self,
        user_id: str,
        title: str,
        *,
        content_type: str = "text/markdown",
        source_name: str = "",
        replace_document_ref: str = "",
        sensitivity: KnowledgeSensitivity = "normal",
    ) -> KnowledgeUploadSession:
        user_id = _required_text(user_id, "user_id", 256)
        title = _required_text(title, "title", 500)
        source_name = _optional_text(source_name, "source_name", 1000)
        content_type = _validate_content_type(content_type)
        sensitivity = _validate_sensitivity(sensitivity)
        now = _utc_now()
        expires_at = _utc_after(hours=_UPLOAD_TTL_HOURS)
        replace_id: str | None = None
        expected_version_id: str | None = None

        with self._connect() as connection:
            if replace_document_ref:
                replace_id = self._document_id(replace_document_ref)
                row = self._get_document_row(
                    connection,
                    user_id=user_id,
                    document_id=replace_id,
                    include_deleted=False,
                )
                expected_version_id = row["current_version_id"]
            upload_id = _new_id()
            connection.execute(
                """
                INSERT INTO knowledge_upload_sessions (
                    id, user_id, title, content_type, source_name, sensitivity,
                    replace_document_id, expected_current_version_id, status,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    upload_id,
                    user_id,
                    title,
                    content_type,
                    source_name,
                    sensitivity,
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
        return self._upload_session_from_row(row)

    def append_upload(
        self,
        user_id: str,
        upload_id: str,
        sequence: int,
        text: str,
    ) -> KnowledgeUploadPart:
        user_id = _required_text(user_id, "user_id", 256)
        upload_id = self._plain_id(upload_id, "upload")
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

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_open_upload(connection, user_id=user_id, upload_id=upload_id)
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
                return self._upload_part_from_row(existing, duplicate=True)

            total = connection.execute(
                """
                SELECT COALESCE(SUM(byte_size), 0) AS total
                FROM knowledge_upload_parts WHERE upload_id = ?
                """,
                (upload_id,),
            ).fetchone()["total"]
            if int(total) + len(encoded) > self.max_document_bytes:
                raise KnowledgeValidationError(
                    f"document exceeds {self.max_document_bytes} UTF-8 bytes"
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
        return self._upload_part_from_row(row)

    def commit_upload(
        self,
        user_id: str,
        upload_id: str,
        expected_parts: int,
        expected_sha256: str = "",
    ) -> KnowledgeCommitResult:
        user_id = _required_text(user_id, "user_id", 256)
        upload_id = self._plain_id(upload_id, "upload")
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

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._require_open_upload(
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
            if len(encoded) > self.max_document_bytes:
                raise KnowledgeValidationError(
                    f"document exceeds {self.max_document_bytes} UTF-8 bytes"
                )
            content_sha256 = hashlib.sha256(encoded).hexdigest()
            if expected_sha256 and not hmac.compare_digest(
                expected_sha256.lower(), content_sha256
            ):
                raise KnowledgeConflictError("uploaded content SHA-256 does not match")

            now = _utc_now()
            connection.execute(
                """
                UPDATE knowledge_upload_sessions
                SET status = 'committing', updated_at = ?
                WHERE id = ?
                """,
                (now, upload_id),
            )
            declared_sensitivity = _validate_sensitivity(session["sensitivity"])
            sensitivity = _sensitivity_floor(
                declared_sensitivity,
                session["title"],
                session["source_name"],
                content,
            )

            replace_id = session["replace_document_id"]
            created = replace_id is None
            if created:
                document_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO knowledge_documents (
                        id, user_id, title, source_name, content_type,
                        sensitivity, status, current_version_id,
                        created_at, updated_at, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, NULL)
                    """,
                    (
                        document_id,
                        user_id,
                        session["title"],
                        session["source_name"],
                        session["content_type"],
                        sensitivity,
                        now,
                        now,
                    ),
                )
                current_version_id = None
                next_version = 1
            else:
                document_id = str(replace_id)
                document = self._get_document_row(
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
                sensitivity = _higher_sensitivity(document["sensitivity"], sensitivity)
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
                            sensitivity = ?, updated_at = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            session["title"],
                            session["source_name"],
                            session["content_type"],
                            sensitivity,
                            now,
                            document_id,
                            user_id,
                        ),
                    )
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
                    document_model = self._load_document_model(
                        connection, user_id=user_id, document_id=document_id
                    )
                    return KnowledgeCommitResult(
                        document=document_model,
                        version=self._version_from_row(current),
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
                        sensitivity = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        session["title"],
                        session["source_name"],
                        session["content_type"],
                        sensitivity,
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
            self._index_version_in_connection(
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
            document_model = self._load_document_model(
                connection, user_id=user_id, document_id=document_id
            )
        return KnowledgeCommitResult(
            document=document_model,
            version=self._version_from_row(version_row),
            created=created,
            deduplicated=False,
        )

    def cancel_upload(self, user_id: str, upload_id: str) -> bool:
        user_id = _required_text(user_id, "user_id", 256)
        upload_id = self._plain_id(upload_id, "upload")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status FROM knowledge_upload_sessions
                WHERE id = ? AND user_id = ?
                """,
                (upload_id, user_id),
            ).fetchone()
            if row is None:
                raise KnowledgeNotFoundError("upload session not found")
            if row["status"] in {"committing", "committed"}:
                raise KnowledgeConflictError("a committed upload cannot be cancelled")
            connection.execute(
                "DELETE FROM knowledge_upload_sessions WHERE id = ? AND user_id = ?",
                (upload_id, user_id),
            )
        return True

    # ------------------------------------------------------------------
    # Document and version management

    def list_documents(
        self,
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
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                {self._document_select_sql()}
                WHERE {' AND '.join(conditions)}
                ORDER BY d.updated_at DESC, d.id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._document_from_row(row) for row in rows]

    def get_document_detail(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        include_content: bool = False,
        include_sensitive: bool = True,
    ) -> dict[str, Any]:
        user_id = _required_text(user_id, "user_id", 256)
        document_id = self._document_id(
            _one_reference(document_id, document_ref, "document")
        )
        with self._connect() as connection:
            document = self._load_document_model(
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
        versions = [self._version_from_row(row, include_content=include_content) for row in rows]
        return {"document": document, "versions": versions}

    def get_version(
        self,
        user_id: str,
        version_id: str,
        *,
        include_content: bool = False,
        include_sensitive: bool = True,
    ) -> KnowledgeVersion:
        user_id = _required_text(user_id, "user_id", 256)
        version_id = self._version_id(version_id)
        with self._connect() as connection:
            row = self._get_version_row(
                connection,
                user_id=user_id,
                version_id=version_id,
                active_document=False,
                include_sensitive=include_sensitive,
            )
        return self._version_from_row(row, include_content=include_content)

    def update_document(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        title: str | None = None,
        source_name: str | None = None,
        sensitivity: KnowledgeSensitivity | None = None,
    ) -> KnowledgeDocument:
        user_id = _required_text(user_id, "user_id", 256)
        document_id = self._document_id(
            _one_reference(document_id, document_ref, "document")
        )
        if title is None and source_name is None and sensitivity is None:
            raise KnowledgeValidationError("at least one document field must be supplied")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_document_row(
                connection,
                user_id=user_id,
                document_id=document_id,
                include_deleted=False,
            )
            new_title = row["title"] if title is None else _required_text(title, "title", 500)
            new_source = (
                row["source_name"]
                if source_name is None
                else _optional_text(source_name, "source_name", 1000)
            )
            declared = row["sensitivity"] if sensitivity is None else _validate_sensitivity(sensitivity)
            content_rows = connection.execute(
                """
                SELECT content FROM knowledge_versions
                WHERE user_id = ? AND document_id = ?
                """,
                (user_id, document_id),
            ).fetchall()
            new_sensitivity = _sensitivity_floor(
                declared,
                new_title,
                new_source,
                *(item["content"] for item in content_rows),
            )
            connection.execute(
                """
                UPDATE knowledge_documents
                SET title = ?, source_name = ?, sensitivity = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_title, new_source, new_sensitivity, _utc_now(), document_id, user_id),
            )
            model = self._load_document_model(
                connection, user_id=user_id, document_id=document_id
            )
        return model

    def soft_delete_document(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        confirm_document_ref: str = "",
    ) -> KnowledgeDocument:
        user_id = _required_text(user_id, "user_id", 256)
        document_id = self._document_id(
            _one_reference(document_id, document_ref, "document")
        )
        if confirm_document_ref and confirm_document_ref != _document_ref(document_id):
            raise KnowledgeConflictError("confirm_document_ref does not match")
        now = _utc_now()
        with self._connect() as connection:
            self._get_document_row(
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
            model = self._load_document_model(
                connection, user_id=user_id, document_id=document_id
            )
        return model

    def restore_document(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
    ) -> KnowledgeDocument:
        user_id = _required_text(user_id, "user_id", 256)
        document_id = self._document_id(
            _one_reference(document_id, document_ref, "document")
        )
        with self._connect() as connection:
            row = self._get_document_row(
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
            model = self._load_document_model(
                connection, user_id=user_id, document_id=document_id
            )
        return model

    def purge_document(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        confirm_document_ref: str = "",
        confirm_document_id: str = "",
    ) -> bool:
        user_id = _required_text(user_id, "user_id", 256)
        supplied_reference = _one_reference(document_id, document_ref, "document")
        document_id = self._document_id(supplied_reference)
        confirmation = _one_reference(
            confirm_document_id,
            confirm_document_ref,
            "document confirmation",
        )
        if self._document_id(confirmation) != document_id:
            raise KnowledgeConflictError("the complete document id or reference is required to purge")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_document_row(
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
        self,
        user_id: str,
        document_id: str = "",
        version_id: str = "",
        *,
        document_ref: str = "",
        version_ref: str = "",
    ) -> KnowledgeCommitResult:
        user_id = _required_text(user_id, "user_id", 256)
        document_id = self._document_id(
            _one_reference(document_id, document_ref, "document")
        )
        version_id = self._version_id(_one_reference(version_id, version_ref, "version"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            document = self._get_document_row(
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
            sensitivity = _sensitivity_floor(
                document["sensitivity"], document["title"], document["source_name"], content
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
                SET sensitivity = ?, updated_at = ? WHERE id = ? AND user_id = ?
                """,
                (sensitivity, now, document_id, user_id),
            )
            self._index_version_in_connection(
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
            document_model = self._load_document_model(
                connection, user_id=user_id, document_id=document_id
            )
        return KnowledgeCommitResult(
            document=document_model,
            version=self._version_from_row(version_row),
            created=False,
            deduplicated=False,
        )

    def reindex_version(
        self,
        user_id: str,
        version_id: str = "",
        *,
        document_id: str = "",
        document_ref: str = "",
        version_ref: str = "",
    ) -> KnowledgeCommitResult:
        user_id = _required_text(user_id, "user_id", 256)
        version_id = self._version_id(_one_reference(version_id, version_ref, "version"))
        supplied_document = document_id or document_ref
        expected_document_id = self._document_id(supplied_document) if supplied_document else ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_version_row(
                connection,
                user_id=user_id,
                version_id=version_id,
                active_document=False,
                include_sensitive=True,
            )
            if expected_document_id and row["document_id"] != expected_document_id:
                raise KnowledgeNotFoundError("knowledge version not found")
            document = self._get_document_row(
                connection,
                user_id=user_id,
                document_id=row["document_id"],
                include_deleted=False,
            )
            prior_status = row["index_status"]
            make_current = prior_status in {"pending", "indexing", "failed"}
            if document["current_version_id"] == version_id:
                make_current = True
            self._index_version_in_connection(
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
            document_model = self._load_document_model(
                connection, user_id=user_id, document_id=row["document_id"]
            )
        return KnowledgeCommitResult(
            document=document_model,
            version=self._version_from_row(result),
            created=False,
            deduplicated=False,
        )

    # ------------------------------------------------------------------
    # Retrieval

    def search_chunks(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        document_refs: Sequence[str] | None = None,
        include_sensitive: bool = False,
    ) -> list[KnowledgeSearchHit]:
        user_id = _required_text(user_id, "user_id", 256)
        query = _required_text(query, "query", 8000)
        limit = _bounded_int(limit, "limit", minimum=1, maximum=_SEARCH_MAX_RESULTS)
        document_ids = self._document_ids(document_refs or [])
        if len(document_ids) > 50:
            raise KnowledgeValidationError("document_refs must not contain more than 50 items")
        if document_ids and not self._all_documents_visible(
            user_id, document_ids, include_sensitive=include_sensitive
        ):
            return []

        compact_query = "".join(query.split())
        if len(compact_query) < 3:
            rows = self._search_with_instr(
                user_id=user_id,
                query=query,
                limit=limit,
                document_ids=document_ids,
                include_sensitive=include_sensitive,
            )
            signal = "substring"
        else:
            rows = self._search_with_fts(
                user_id=user_id,
                query=query,
                limit=limit,
                document_ids=document_ids,
                include_sensitive=include_sensitive,
            )
            signal = "fts"
            if not rows:
                rows = self._search_with_instr(
                    user_id=user_id,
                    query=query,
                    limit=limit,
                    document_ids=document_ids,
                    include_sensitive=include_sensitive,
                )
                signal = "substring"
        return [self._search_hit_from_row(row, query=query, signal=signal) for row in rows]

    # Agent compatibility name.
    search_index = search_chunks

    def get_chunks_by_refs(
        self,
        user_id: str,
        chunk_refs: Sequence[str],
        include_sensitive: bool = False,
    ) -> list[KnowledgeSearchHit]:
        user_id = _required_text(user_id, "user_id", 256)
        if len(chunk_refs) > 20:
            raise KnowledgeValidationError("chunk_refs must not contain more than 20 items")
        chunk_ids = [self._chunk_id(ref) for ref in chunk_refs]
        if not chunk_ids:
            return []
        unique_ids = list(dict.fromkeys(chunk_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        sensitive_sql = "" if include_sensitive else "AND d.sensitivity = 'normal'"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    c.*,
                    d.title,
                    d.source_name,
                    d.content_type,
                    d.sensitivity,
                    d.status AS document_status,
                    v.version_number,
                    0.0 AS rank
                FROM knowledge_chunks c
                JOIN knowledge_documents d
                    ON d.id = c.document_id AND d.user_id = c.user_id
                JOIN knowledge_versions v
                    ON v.id = c.version_id AND v.user_id = c.user_id
                WHERE c.user_id = ?
                  AND c.id IN ({placeholders})
                  AND d.status = 'active'
                  AND v.index_status = 'ready'
                  {sensitive_sql}
                """,
                [user_id, *unique_ids],
            ).fetchall()
        by_id = {row["id"]: row for row in rows}
        result: list[KnowledgeSearchHit] = []
        for chunk_id in chunk_ids:
            row = by_id.get(chunk_id)
            if row is not None:
                result.append(self._search_hit_from_row(row, query="", signal="reference"))
        return result

    # Agent compatibility name.
    inspect_chunks = get_chunks_by_refs

    def read_reference(
        self,
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
            chunk_id = self._chunk_id(reference)
            with self._connect() as connection:
                row = self._get_chunk_row(
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
        version_id = self._version_id(reference)
        with self._connect() as connection:
            row = self._get_version_row(
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

    # ------------------------------------------------------------------
    # Independent backup and restore

    def list_versions(
        self,
        user_id: str,
        document_id: str = "",
        *,
        document_ref: str = "",
        include_content: bool = False,
    ) -> list[KnowledgeVersion]:
        user_id = _required_text(user_id, "user_id", 256)
        document_id = self._document_id(
            _one_reference(document_id, document_ref, "document")
        )
        with self._connect() as connection:
            self._get_document_row(
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
            self._version_from_row(row, include_content=include_content) for row in rows
        ]

    def export_user(self, user_id: str) -> dict[str, Any]:
        """Export canonical knowledge data, never derived chunks or FTS rows."""
        user_id = _required_text(user_id, "user_id", 256)
        documents: list[dict[str, Any]] = []
        with self._connect() as connection:
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
            "schema_version": 1,
            "exported_at": _utc_now(),
            "documents": documents,
        }

    def restore_export(self, user_id: str, export_data: dict[str, Any]) -> dict[str, Any]:
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
        if len(documents_value) > 100_000:
            raise KnowledgeValidationError("knowledge export contains too many documents")
        prepared = [self._validate_import_document(value) for value in documents_value]

        restored_documents: list[KnowledgeDocument] = []
        restored_versions = 0
        failed_versions = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in prepared:
                document_id = _new_id()
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO knowledge_documents (
                        id, user_id, title, source_name, content_type,
                        sensitivity, status, current_version_id,
                        created_at, updated_at, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, NULL)
                    """,
                    (
                        document_id,
                        user_id,
                        item["title"],
                        item["source_name"],
                        item["content_type"],
                        item["sensitivity"],
                        item["created_at"] or now,
                        now,
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
                    self._index_version_in_connection(
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
                    self._load_document_model(
                        connection, user_id=user_id, document_id=document_id
                    )
                )
        return {
            "restored_documents": len(restored_documents),
            "restored_versions": restored_versions,
            "failed_versions": failed_versions,
            "document_refs": [item.ref for item in restored_documents],
            "chunks_rebuilt": True,
            "fts_rebuilt": True,
        }

    def _validate_import_document(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise KnowledgeValidationError("each exported knowledge document must be an object")
        title = _required_text(value.get("title"), "title", 500)
        source_name = _optional_text(value.get("source_name", ""), "source_name", 1000)
        content_type = _validate_content_type(value.get("content_type", "text/markdown"))
        declared = _validate_sensitivity(value.get("sensitivity", "normal"))
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
        total_bytes = 0
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
            if len(encoded) > self.max_document_bytes:
                raise KnowledgeValidationError(
                    f"document exceeds {self.max_document_bytes} UTF-8 bytes"
                )
            total_bytes += len(encoded)
            versions.append(
                {
                    "version_number": number,
                    "content": content,
                    "created_at": _safe_exported_time(raw_version.get("created_at")),
                }
            )
        if total_bytes > self.max_document_bytes * len(versions):
            raise KnowledgeValidationError("exported version data is too large")
        versions.sort(key=lambda item: item["version_number"])
        current_number = value.get("current_version_number")
        if current_number is None:
            current_number = versions[-1]["version_number"]
        if isinstance(current_number, bool) or not isinstance(current_number, int):
            raise KnowledgeValidationError("current_version_number must be an integer")
        if current_number not in seen_numbers:
            raise KnowledgeValidationError("current_version_number is not present in versions")
        sensitivity = _sensitivity_floor(
            declared,
            title,
            source_name,
            *(item["content"] for item in versions),
        )
        return {
            "title": title,
            "source_name": source_name,
            "content_type": content_type,
            "sensitivity": sensitivity,
            "status": status,
            "current_version_number": current_number,
            "created_at": _safe_exported_time(value.get("created_at")),
            "deleted_at": _safe_exported_time(value.get("deleted_at")),
            "versions": versions,
        }

    # ------------------------------------------------------------------
    # Status and counts

    def counts(self, user_id: str) -> dict[str, int]:
        user_id = _required_text(user_id, "user_id", 256)
        with self._connect() as connection:
            document_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM knowledge_documents
                WHERE user_id = ? GROUP BY status
                """,
                (user_id,),
            ).fetchall()
            version_rows = connection.execute(
                """
                SELECT index_status, COUNT(*) AS count FROM knowledge_versions
                WHERE user_id = ? GROUP BY index_status
                """,
                (user_id,),
            ).fetchall()
            chunk_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM knowledge_chunks WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["count"]
            )
            open_uploads = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM knowledge_upload_sessions
                    WHERE user_id = ? AND status = 'open' AND expires_at > ?
                    """,
                    (user_id, _utc_now()),
                ).fetchone()["count"]
            )
        result = {
            "documents": 0,
            "active_documents": 0,
            "deleted_documents": 0,
            "versions": 0,
            "chunks": chunk_count,
            "index_pending": 0,
            "index_indexing": 0,
            "index_ready": 0,
            "index_failed": 0,
            "open_uploads": open_uploads,
        }
        for row in document_rows:
            count = int(row["count"])
            result["documents"] += count
            result[f"{row['status']}_documents"] = count
        for row in version_rows:
            count = int(row["count"])
            result["versions"] += count
            result[f"index_{row['index_status']}"] = count
        return result

    get_counts = counts

    def status(self, user_id: str) -> dict[str, Any]:
        try:
            counts = self.counts(user_id)
        except sqlite3.Error as exc:
            return {
                "available": False,
                "fts5": False,
                "tokenizer": "trigram",
                "error": str(exc),
                "counts": {},
            }
        return {
            "available": True,
            "fts5": True,
            "tokenizer": "trigram",
            "error": "",
            "counts": counts,
        }

    get_status = status

    # ------------------------------------------------------------------
    # Internal SQL and mapping helpers

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            factory=_ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _require_open_upload(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        upload_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM knowledge_upload_sessions
            WHERE id = ? AND user_id = ?
            """,
            (upload_id, user_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("upload session not found")
        if row["status"] != "open":
            raise KnowledgeConflictError("upload session is not open")
        if _parse_utc(row["expires_at"]) <= datetime.now(UTC):
            connection.execute(
                """
                UPDATE knowledge_upload_sessions
                SET status = 'expired', updated_at = ? WHERE id = ? AND user_id = ?
                """,
                (_utc_now(), upload_id, user_id),
            )
            raise KnowledgeConflictError("upload session has expired")
        return row

    def _index_version_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        document_id: str,
        version_id: str,
        make_current: bool,
    ) -> None:
        row = connection.execute(
            """
            SELECT * FROM knowledge_versions
            WHERE id = ? AND document_id = ? AND user_id = ?
            """,
            (version_id, document_id, user_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("knowledge version not found")
        connection.execute(
            """
            UPDATE knowledge_versions
            SET index_status = 'indexing', index_error = NULL, indexed_at = NULL
            WHERE id = ? AND user_id = ?
            """,
            (version_id, user_id),
        )
        connection.execute(
            "DELETE FROM knowledge_chunks_fts WHERE user_id = ? AND version_id = ?",
            (user_id, version_id),
        )
        connection.execute(
            "DELETE FROM knowledge_chunks WHERE user_id = ? AND version_id = ?",
            (user_id, version_id),
        )
        try:
            drafts = chunk_knowledge_text(row["content"])
            if not drafts:
                raise ValueError("document content produced no indexable chunks")
            now = _utc_now()
            for draft in drafts:
                chunk_id = f"{version_id}_{draft.ordinal}"
                title_path_json = json.dumps(
                    list(draft.title_path), ensure_ascii=False, separators=(",", ":")
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        id, document_id, version_id, user_id, ordinal,
                        title_path_json, char_start, char_end, line_start,
                        line_end, content, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        document_id,
                        version_id,
                        user_id,
                        draft.ordinal,
                        title_path_json,
                        draft.char_start,
                        draft.char_end,
                        draft.line_start,
                        draft.line_end,
                        draft.content,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_chunks_fts (
                        chunk_id, user_id, document_id, version_id, content, title_path
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        user_id,
                        document_id,
                        version_id,
                        draft.content,
                        " / ".join(draft.title_path),
                    ),
                )
            indexed_at = _utc_now()
            connection.execute(
                """
                UPDATE knowledge_versions
                SET index_status = 'ready', index_error = NULL, indexed_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (indexed_at, version_id, user_id),
            )
            if make_current:
                connection.execute(
                    """
                    UPDATE knowledge_documents
                    SET current_version_id = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (version_id, indexed_at, document_id, user_id),
                )
        except Exception as exc:
            connection.execute(
                "DELETE FROM knowledge_chunks_fts WHERE user_id = ? AND version_id = ?",
                (user_id, version_id),
            )
            connection.execute(
                "DELETE FROM knowledge_chunks WHERE user_id = ? AND version_id = ?",
                (user_id, version_id),
            )
            connection.execute(
                """
                UPDATE knowledge_versions
                SET index_status = 'failed', index_error = ?, indexed_at = NULL
                WHERE id = ? AND user_id = ?
                """,
                (_safe_error(exc), version_id, user_id),
            )

    def _search_with_fts(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
        document_ids: list[str],
        include_sensitive: bool,
    ) -> list[sqlite3.Row]:
        fts_query = _fts_query(query)
        conditions = [
            "knowledge_chunks_fts MATCH ?",
            "c.user_id = ?",
            "d.status = 'active'",
            "d.current_version_id = c.version_id",
            "v.index_status = 'ready'",
        ]
        params: list[Any] = [fts_query, user_id]
        if not include_sensitive:
            conditions.append("d.sensitivity = 'normal'")
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            conditions.append(f"c.document_id IN ({placeholders})")
            params.extend(document_ids)
        params.append(limit)
        try:
            with self._connect() as connection:
                return connection.execute(
                    f"""
                    SELECT
                        c.*,
                        d.title,
                        d.source_name,
                        d.content_type,
                        d.sensitivity,
                        v.version_number,
                        bm25(knowledge_chunks_fts) AS rank
                    FROM knowledge_chunks_fts
                    JOIN knowledge_chunks c
                        ON c.id = knowledge_chunks_fts.chunk_id
                    JOIN knowledge_documents d
                        ON d.id = c.document_id AND d.user_id = c.user_id
                    JOIN knowledge_versions v
                        ON v.id = c.version_id AND v.user_id = c.user_id
                    WHERE {' AND '.join(conditions)}
                    ORDER BY rank ASC, c.ordinal ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        except sqlite3.OperationalError:
            return []

    def _search_with_instr(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
        document_ids: list[str],
        include_sensitive: bool,
    ) -> list[sqlite3.Row]:
        conditions = [
            "c.user_id = ?",
            "d.status = 'active'",
            "d.current_version_id = c.version_id",
            "v.index_status = 'ready'",
            "(instr(lower(c.content), lower(?)) > 0 OR "
            "instr(lower(c.title_path_json), lower(?)) > 0 OR "
            "instr(lower(d.title), lower(?)) > 0)",
        ]
        params: list[Any] = [user_id, query, query, query]
        if not include_sensitive:
            conditions.append("d.sensitivity = 'normal'")
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            conditions.append(f"c.document_id IN ({placeholders})")
            params.extend(document_ids)
        params.append(limit)
        with self._connect() as connection:
            return connection.execute(
                f"""
                SELECT
                    c.*,
                    d.title,
                    d.source_name,
                    d.content_type,
                    d.sensitivity,
                    v.version_number,
                    CASE
                        WHEN instr(lower(c.content), lower(?)) > 0 THEN 0.0
                        WHEN instr(lower(c.title_path_json), lower(?)) > 0 THEN 0.5
                        ELSE 1.0
                    END AS rank
                FROM knowledge_chunks c
                JOIN knowledge_documents d
                    ON d.id = c.document_id AND d.user_id = c.user_id
                JOIN knowledge_versions v
                    ON v.id = c.version_id AND v.user_id = c.user_id
                WHERE {' AND '.join(conditions)}
                ORDER BY rank ASC, c.ordinal ASC
                LIMIT ?
                """,
                [query, query, *params],
            ).fetchall()

    def _all_documents_visible(
        self,
        user_id: str,
        document_ids: list[str],
        *,
        include_sensitive: bool,
    ) -> bool:
        unique_ids = list(dict.fromkeys(document_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        sensitivity_sql = "" if include_sensitive else "AND sensitivity = 'normal'"
        with self._connect() as connection:
            count = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS count FROM knowledge_documents
                    WHERE user_id = ? AND status = 'active'
                      AND id IN ({placeholders}) {sensitivity_sql}
                    """,
                    [user_id, *unique_ids],
                ).fetchone()["count"]
            )
        return count == len(unique_ids)

    def _get_document_row(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        document_id: str,
        include_deleted: bool,
    ) -> sqlite3.Row:
        status_sql = "" if include_deleted else "AND status = 'active'"
        row = connection.execute(
            f"""
            SELECT * FROM knowledge_documents
            WHERE id = ? AND user_id = ? {status_sql}
            """,
            (document_id, user_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("knowledge document not found")
        return row

    def _get_version_row(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        version_id: str,
        active_document: bool,
        include_sensitive: bool,
    ) -> sqlite3.Row:
        status_sql = "AND d.status = 'active'" if active_document else ""
        sensitivity_sql = "" if include_sensitive else "AND d.sensitivity = 'normal'"
        row = connection.execute(
            f"""
            SELECT v.*
            FROM knowledge_versions v
            JOIN knowledge_documents d
                ON d.id = v.document_id AND d.user_id = v.user_id
            WHERE v.id = ? AND v.user_id = ? {status_sql} {sensitivity_sql}
            """,
            (version_id, user_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("knowledge version not found")
        return row

    def _get_chunk_row(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        chunk_id: str,
        include_sensitive: bool,
    ) -> sqlite3.Row:
        sensitivity_sql = "" if include_sensitive else "AND d.sensitivity = 'normal'"
        row = connection.execute(
            f"""
            SELECT c.*, d.title, d.source_name, d.content_type, d.sensitivity
            FROM knowledge_chunks c
            JOIN knowledge_documents d
                ON d.id = c.document_id AND d.user_id = c.user_id
            JOIN knowledge_versions v
                ON v.id = c.version_id AND v.user_id = c.user_id
            WHERE c.id = ? AND c.user_id = ?
              AND d.status = 'active' AND v.index_status = 'ready'
              {sensitivity_sql}
            """,
            (chunk_id, user_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("knowledge reference not found")
        return row

    def _load_document_model(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        document_id: str,
    ) -> KnowledgeDocument:
        row = connection.execute(
            f"""
            {self._document_select_sql()}
            WHERE d.id = ? AND d.user_id = ?
            """,
            (document_id, user_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("knowledge document not found")
        return self._document_from_row(row)

    @staticmethod
    def _document_select_sql() -> str:
        return """
            SELECT
                d.*,
                cv.version_number AS current_version_number,
                cv.index_status AS current_index_status,
                COALESCE(
                    cv.byte_size,
                    (SELECT lv.byte_size FROM knowledge_versions lv
                     WHERE lv.document_id = d.id AND lv.user_id = d.user_id
                     ORDER BY lv.version_number DESC LIMIT 1),
                    0
                ) AS current_byte_size,
                COALESCE(
                    cv.character_count,
                    (SELECT lv.character_count FROM knowledge_versions lv
                     WHERE lv.document_id = d.id AND lv.user_id = d.user_id
                     ORDER BY lv.version_number DESC LIMIT 1),
                    0
                ) AS current_character_count,
                COALESCE(
                    cv.index_status,
                    (SELECT lv.index_status FROM knowledge_versions lv
                     WHERE lv.document_id = d.id AND lv.user_id = d.user_id
                     ORDER BY lv.version_number DESC LIMIT 1)
                ) AS display_index_status
            FROM knowledge_documents d
            LEFT JOIN knowledge_versions cv
                ON cv.id = d.current_version_id AND cv.user_id = d.user_id
        """

    def _document_from_row(self, row: sqlite3.Row) -> KnowledgeDocument:
        version_id = row["current_version_id"]
        return KnowledgeDocument(
            id=row["id"],
            ref=_document_ref(row["id"]),
            user_id=row["user_id"],
            title=row["title"],
            source_name=row["source_name"],
            content_type=row["content_type"],
            sensitivity=row["sensitivity"],
            status=row["status"],
            current_version_id=version_id,
            current_version_ref=_version_ref(version_id) if version_id else "",
            current_version_number=row["current_version_number"],
            index_status=row["display_index_status"],
            byte_size=int(row["current_byte_size"] or 0),
            character_count=int(row["current_character_count"] or 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    def _version_from_row(
        self,
        row: sqlite3.Row,
        *,
        include_content: bool = False,
    ) -> KnowledgeVersion:
        return KnowledgeVersion(
            id=row["id"],
            ref=_version_ref(row["id"]),
            document_id=row["document_id"],
            document_ref=_document_ref(row["document_id"]),
            user_id=row["user_id"],
            version_number=int(row["version_number"]),
            content_sha256=row["content_sha256"],
            byte_size=int(row["byte_size"]),
            character_count=int(row["character_count"]),
            index_status=row["index_status"],
            index_error=row["index_error"],
            created_at=row["created_at"],
            indexed_at=row["indexed_at"],
            content=row["content"] if include_content else None,
        )

    def _chunk_from_row(self, row: sqlite3.Row) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=row["id"],
            ref=_chunk_ref(row["id"]),
            document_id=row["document_id"],
            document_ref=_document_ref(row["document_id"]),
            version_id=row["version_id"],
            version_ref=_version_ref(row["version_id"]),
            user_id=row["user_id"],
            ordinal=int(row["ordinal"]),
            title_path=_json_string_list(row["title_path_json"]),
            char_start=int(row["char_start"]),
            char_end=int(row["char_end"]),
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
            content=row["content"],
            created_at=row["created_at"],
        )

    def _search_hit_from_row(
        self,
        row: sqlite3.Row,
        *,
        query: str,
        signal: str,
    ) -> KnowledgeSearchHit:
        content = row["content"]
        if query:
            excerpt, local_start, local_end = _excerpt(content, query, _SEARCH_EXCERPT_CHARS)
        else:
            excerpt, local_start, local_end = content, 0, len(content)
        absolute_start = int(row["char_start"]) + local_start
        absolute_end = int(row["char_start"]) + local_end
        line_start = int(row["line_start"]) + content.count("\n", 0, local_start)
        line_end = line_start + max(0, excerpt.count("\n") - (1 if excerpt.endswith("\n") else 0))
        rank = float(row["rank"] or 0.0)
        signals = [signal]
        if signal == "fts":
            signals.append("trigram")
        if query and query.casefold() in content.casefold():
            signals.append("exact_phrase")
        title_path = _json_string_list(row["title_path_json"])
        if query and query.casefold() in " / ".join(title_path).casefold():
            signals.append("heading")
        if signal == "reference":
            score = 1.0
        elif signal == "fts":
            # FTS5 bm25 is ordered ascending and normally returns negative
            # values; negate it so a stronger match also has a larger score.
            score = max(0.0, -rank)
        else:
            score = 1.0 / (1.0 + max(0.0, rank))
        return KnowledgeSearchHit(
            document_ref=_document_ref(row["document_id"]),
            version_ref=_version_ref(row["version_id"]),
            chunk_ref=_chunk_ref(row["id"]),
            title=row["title"],
            source_name=row["source_name"],
            content_type=row["content_type"],
            sensitivity=row["sensitivity"],
            title_path=title_path,
            ordinal=int(row["ordinal"]),
            char_start=absolute_start,
            char_end=absolute_end,
            line_start=line_start,
            line_end=max(line_start, line_end),
            excerpt=excerpt,
            score=score,
            match_signals=signals,
        )

    def _upload_session_from_row(self, row: sqlite3.Row) -> KnowledgeUploadSession:
        replace_id = row["replace_document_id"]
        expected_id = row["expected_current_version_id"]
        return KnowledgeUploadSession(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            content_type=row["content_type"],
            source_name=row["source_name"],
            sensitivity=row["sensitivity"],
            replace_document_id=replace_id,
            replace_document_ref=_document_ref(replace_id) if replace_id else "",
            expected_current_version_id=expected_id,
            expected_current_version_ref=_version_ref(expected_id) if expected_id else "",
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            committed_document_ref=row["committed_document_ref"],
            committed_version_ref=row["committed_version_ref"],
        )

    @staticmethod
    def _upload_part_from_row(
        row: sqlite3.Row,
        *,
        duplicate: bool = False,
    ) -> KnowledgeUploadPart:
        return KnowledgeUploadPart(
            upload_id=row["upload_id"],
            sequence=int(row["sequence"]),
            character_count=int(row["character_count"]),
            byte_size=int(row["byte_size"]),
            content_sha256=row["content_sha256"],
            created_at=row["created_at"],
            duplicate=duplicate,
        )

    @staticmethod
    def _plain_id(value: str, label: str) -> str:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise KnowledgeValidationError(f"invalid {label} id")
        return value

    def _document_id(self, value: str) -> str:
        return self._reference_id(value, _DOCUMENT_PREFIX, "document")

    def _version_id(self, value: str) -> str:
        return self._reference_id(value, _VERSION_PREFIX, "version")

    def _chunk_id(self, value: str) -> str:
        return self._reference_id(value, _CHUNK_PREFIX, "chunk")

    def _reference_id(self, value: str, prefix: str, label: str) -> str:
        if not isinstance(value, str):
            raise KnowledgeValidationError(f"invalid {label} reference")
        raw = value[len(prefix) :] if value.startswith(prefix) else value
        if not _ID_RE.fullmatch(raw):
            raise KnowledgeValidationError(f"invalid {label} reference")
        if value.startswith("knowledge://") and not value.startswith(prefix):
            raise KnowledgeValidationError(f"invalid {label} reference")
        return raw

    def _document_ids(self, values: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            document_id = self._document_id(value)
            if document_id not in seen:
                seen.add(document_id)
                result.append(document_id)
        return result


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


def _detect_sensitivity(text: str) -> KnowledgeSensitivity:
    if any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS):
        return "sensitive"
    if any(pattern.search(text) for pattern in _PRIVATE_PATTERNS):
        return "private"
    return "normal"


def detect_knowledge_text_sensitivity(text: str) -> KnowledgeSensitivity:
    """Public local detector used by storage and knowledge-agent egress gates."""

    if not isinstance(text, str):
        raise KnowledgeValidationError("text must be a string")
    return _detect_sensitivity(text)


def _sensitivity_floor(
    declared: KnowledgeSensitivity,
    *texts: str | None,
) -> KnowledgeSensitivity:
    detected = _detect_sensitivity("\n".join(value for value in texts if value))
    return _higher_sensitivity(declared, detected)


def _higher_sensitivity(left: str, right: str) -> KnowledgeSensitivity:
    value = max((left, right), key=_SENSITIVITY_RANK.__getitem__)
    return value  # type: ignore[return-value]


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace("\x00", "").strip()
    return (text or exc.__class__.__name__)[:500]


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


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _last_touched_line(text: str, start: int, end: int) -> int:
    if end <= start:
        return _line_at(text, start)
    return text.count("\n", 0, end - 1) + 1
