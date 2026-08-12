from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Literal
import unicodedata

from app.schema_versions import AUTH_SCHEMA_VERSION


AuthRole = Literal["chat", "mcp", "console"]
AUTH_ROLES: tuple[AuthRole, ...] = ("chat", "mcp", "console")
# chat tokens may cap automatic memory writes; mcp/console keep "read-write"
# for schema uniformity but ignore the field on non-chat routes.
MemoryAccess = Literal["read", "read-write"]
MEMORY_ACCESS_VALUES: tuple[MemoryAccess, ...] = ("read", "read-write")
_SCHEMA_VERSION = AUTH_SCHEMA_VERSION
_TOKEN_RE = re.compile(r"^mgw_([a-f0-9]{16})_([A-Za-z0-9_-]{32,128})$")
_TOKEN_ID_RE = re.compile(r"^[a-f0-9]{16}$")


@dataclass(frozen=True, slots=True)
class AuthTokenRecord:
    token_id: str
    name: str
    user_id: str
    role: AuthRole
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    memory_access: MemoryAccess = "read-write"


@dataclass(frozen=True, slots=True)
class CreatedAuthToken:
    token: str
    record: AuthTokenRecord


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    identity: str
    token_id: str
    name: str
    user_id: str
    role: AuthRole | Literal["legacy"]
    legacy: bool = False
    memory_access: MemoryAccess = "read-write"


class AuthStoreError(ValueError):
    """The auth database or token request is invalid."""


class LastActiveConsoleTokenError(AuthStoreError):
    """Revoking this token would leave its user without Console access."""


class AuthTokenStore:
    """Small SQLite store that persists only one-way token hashes.

    A token ID is the first 16 hexadecimal characters of SHA-256(secret), so
    list/revoke can expose a stable non-secret identifier without adding a
    second credential-like value to the database.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(Path(database_path).expanduser())

    def init_db(self) -> None:
        path = Path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise AuthStoreError(
                    "AUTH_DATABASE_PATH 的 schema 来自更高版本，当前程序拒绝降级打开"
                )
            if version == 0:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auth_tokens (
                        token_hash TEXT PRIMARY KEY
                            CHECK(length(token_hash) = 64),
                        name TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL
                            CHECK(role IN ('chat', 'mcp', 'console')),
                        memory_access TEXT NOT NULL DEFAULT 'read-write'
                            CHECK(memory_access IN ('read', 'read-write')),
                        created_at TEXT NOT NULL,
                        last_used_at TEXT,
                        revoked_at TEXT
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_auth_tokens_role_active "
                    "ON auth_tokens(role, revoked_at)"
                )
                # A pre-versioned database may already have the table without
                # the v2 column; CREATE TABLE IF NOT EXISTS skips it silently.
                self._ensure_memory_access_column(connection)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                connection.commit()
            elif version == 1:
                self._ensure_memory_access_column(connection)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                connection.commit()
            self._validate_schema(connection)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def create_token(
        self,
        *,
        name: str,
        user_id: str,
        role: AuthRole,
        memory_access: MemoryAccess = "read-write",
    ) -> CreatedAuthToken:
        normalized_name = _validate_label(name, field="name", max_length=100)
        normalized_user_id = _validate_label(
            user_id,
            field="user_id",
            max_length=128,
        )
        if role not in AUTH_ROLES:
            raise AuthStoreError("role 必须是 chat、mcp 或 console")
        if memory_access not in MEMORY_ACCESS_VALUES:
            raise AuthStoreError("memory_access 必须是 read 或 read-write")
        # Only chat tokens constrain automatic memory write. Reject an
        # explicit "read" on other roles instead of silently widening it.
        if role != "chat" and memory_access != "read-write":
            raise AuthStoreError(
                "memory_access=read 仅支持 chat token；mcp/console token 不接受该参数"
            )

        created_at = _utc_now()
        for _ in range(5):
            secret = secrets.token_urlsafe(32)
            token_hash = _hash_secret(secret)
            token_id = token_hash[:16]
            token = f"mgw_{token_id}_{secret}"
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO auth_tokens(
                            token_hash, name, user_id, role, memory_access,
                            created_at, last_used_at, revoked_at
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                        """,
                        (
                            token_hash,
                            normalized_name,
                            normalized_user_id,
                            role,
                            memory_access,
                            created_at,
                        ),
                    )
                    connection.commit()
                return CreatedAuthToken(
                    token=token,
                    record=AuthTokenRecord(
                        token_id=token_id,
                        name=normalized_name,
                        user_id=normalized_user_id,
                        role=role,
                        created_at=created_at,
                        last_used_at=None,
                        revoked_at=None,
                        memory_access=memory_access,
                    ),
                )
            except sqlite3.IntegrityError:
                continue
        raise AuthStoreError("无法生成唯一访问令牌，请重试")

    def authenticate(self, token: str) -> AuthTokenRecord | None:
        parsed = _TOKEN_RE.fullmatch(token)
        if parsed is None:
            return None
        token_id, secret = parsed.groups()
        token_hash = _hash_secret(secret)
        if not hmac.compare_digest(token_id, token_hash[:16]):
            return None

        now = _utc_now()
        cutoff = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(
            timespec="seconds"
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT token_hash, name, user_id, role, memory_access,
                       created_at, last_used_at, revoked_at
                FROM auth_tokens
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            if not hmac.compare_digest(str(row["token_hash"]), token_hash):
                return None
            cursor = connection.execute(
                """
                UPDATE auth_tokens
                SET last_used_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                  AND (last_used_at IS NULL OR last_used_at < ?)
                """,
                (now, token_hash, cutoff),
            )
            if cursor.rowcount:
                connection.commit()
                row = dict(row)
                row["last_used_at"] = now
        return _record_from_row(row)

    def list_tokens(self, *, user_id: str | None = None) -> list[AuthTokenRecord]:
        normalized_user_id = (
            _validate_label(user_id, field="user_id", max_length=128)
            if user_id is not None
            else None
        )
        query = """
            SELECT token_hash, name, user_id, role, memory_access,
                   created_at, last_used_at, revoked_at
            FROM auth_tokens
        """
        params: tuple[str, ...] = ()
        if normalized_user_id is not None:
            query += " WHERE user_id = ?"
            params = (normalized_user_id,)
        query += " ORDER BY created_at, token_hash"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_record_from_row(row) for row in rows]

    def revoke_token(
        self,
        token_id: str,
        *,
        user_id: str | None = None,
        protect_last_console: bool = False,
    ) -> bool:
        normalized = token_id.strip().lower()
        if _TOKEN_ID_RE.fullmatch(normalized) is None:
            raise AuthStoreError("token id 必须是 16 位小写十六进制字符")
        normalized_user_id = (
            _validate_label(user_id, field="user_id", max_length=128)
            if user_id is not None
            else None
        )
        query = (
            "SELECT token_hash, user_id, role, revoked_at FROM auth_tokens "
            "WHERE substr(token_hash, 1, 16) = ?"
        )
        params: tuple[str, ...] = (normalized,)
        if normalized_user_id is not None:
            query += " AND user_id = ?"
            params = (normalized, normalized_user_id)
        with self._connect() as connection:
            # Serialize the read/count/update decision. Two Console requests
            # cannot both observe another active credential and revoke the
            # final pair concurrently.
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(query, params).fetchall()
            if len(rows) > 1:
                raise AuthStoreError("token id 前缀发生冲突，拒绝批量撤销")
            if not rows:
                connection.rollback()
                return False
            target = rows[0]
            if (
                protect_last_console
                and target["role"] == "console"
                and target["revoked_at"] is None
            ):
                active_console_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM auth_tokens "
                        "WHERE user_id = ? AND role = 'console' "
                        "AND revoked_at IS NULL",
                        (target["user_id"],),
                    ).fetchone()[0]
                )
                if active_console_count <= 1:
                    connection.rollback()
                    raise LastActiveConsoleTokenError(
                        "必须保留至少一个可用的 Console token"
                    )
            cursor = connection.execute(
                "UPDATE auth_tokens SET revoked_at = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (_utc_now(), target["token_hash"]),
            )
            connection.commit()
            return bool(cursor.rowcount)

    def has_active_tokens(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM auth_tokens WHERE revoked_at IS NULL LIMIT 1"
            ).fetchone()
        return row is not None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _ensure_memory_access_column(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(auth_tokens)").fetchall()
        }
        if columns and "memory_access" not in columns:
            connection.execute(
                "ALTER TABLE auth_tokens ADD COLUMN memory_access "
                "TEXT NOT NULL DEFAULT 'read-write'"
            )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(auth_tokens)").fetchall()
        }
        expected = {
            "token_hash",
            "name",
            "user_id",
            "role",
            "memory_access",
            "created_at",
            "last_used_at",
            "revoked_at",
        }
        if columns != expected:
            raise AuthStoreError("AUTH_DATABASE_PATH 的 auth_tokens schema 不兼容")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != _SCHEMA_VERSION:
            raise AuthStoreError("AUTH_DATABASE_PATH 的 schema 版本与程序不一致")


def _record_from_row(row: sqlite3.Row | dict[str, object]) -> AuthTokenRecord:
    role = str(row["role"])
    if role not in AUTH_ROLES:
        raise AuthStoreError("auth token role 无效")
    raw_access = row["memory_access"] if "memory_access" in row.keys() else "read-write"
    memory_access = str(raw_access or "read-write")
    if memory_access not in MEMORY_ACCESS_VALUES:
        memory_access = "read-write"
    return AuthTokenRecord(
        token_id=str(row["token_hash"])[:16],
        name=str(row["name"]),
        user_id=str(row["user_id"]),
        role=role,  # type: ignore[arg-type]
        created_at=str(row["created_at"]),
        last_used_at=(
            str(row["last_used_at"]) if row["last_used_at"] is not None else None
        ),
        revoked_at=(str(row["revoked_at"]) if row["revoked_at"] is not None else None),
        memory_access=memory_access,  # type: ignore[arg-type]
    )


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _validate_label(value: str, *, field: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise AuthStoreError(f"{field} 长度必须为 1 到 {max_length} 个字符")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in normalized
    ):
        raise AuthStoreError(f"{field} 不能包含控制字符")
    return normalized


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
