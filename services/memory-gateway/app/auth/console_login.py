"""一次性 console 登录 code 的签发与交换。

``POST /auth/console-login-code``（console role）签发一次性 code，同时换发一个
新的 console role token；``POST /auth/console-login-exchange``（免 Bearer，仅
本机来源）凭 code 一次性取回该 token 明文，免除手工复制 64 位 token。

同进程的宿主（安卓 App 的嵌入式入口）还可以用 ``mint_for_token`` 为一枚**既有**
console token 签发 code：浏览器换到的始终是同一枚首次登录密钥，不会每次打开
控制台都多出一枚新 token；这类 code 过期时也绝不吊销那枚既有 token。宿主可以
随 code 一并暂存 Model Gateway 管理密钥，交换时一同交付，让本机浏览器免去
第二次手工粘贴。

安全不变量：

- AUTH DB 只保存 code 的 SHA-256 哈希（复用 token 的哈希方案）、绑定的
  token id、过期与使用状态；code 明文、token 明文与管理密钥都绝不落盘。
- 明文只暂存在进程内存（``_PENDING_TOKENS``），在交换响应交付的瞬间
  弹出并丢弃。进程重启后暂存丢失，对应 code 交换按失败处理，并吊销由该
  code 换发的 token（fail-closed，见 ``exchange``）。
- code 5 分钟过期、单次使用；无效/过期/已使用一律由路由返回同一 401 响应，
  不区分原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import secrets
import sqlite3
import threading

from app.auth.tokens import AuthStoreError, AuthTokenStore, _hash_secret
from app.sqlite_util import ClosingSQLiteConnection as _ClosingSQLiteConnection


CONSOLE_LOGIN_CODE_TTL_SECONDS = 300
_CODE_MAX_LENGTH = 256


@dataclass(frozen=True, slots=True)
class MintedConsoleLoginCode:
    code: str
    token_id: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ExchangedConsoleLogin:
    token: str
    token_id: str
    user_id: str
    # 仅宿主进程（安卓 App）签发的 code 会携带；HTTP 路径签发的 code 永远为 None。
    admin_key: str | None = None


@dataclass(frozen=True, slots=True)
class _PendingDelivery:
    token: str
    expires_at: datetime
    admin_key: str | None


# code 哈希 -> 待交付明文。明文只存在于本进程内存中。
_PENDING_LOCK = threading.Lock()
_PENDING_TOKENS: dict[str, _PendingDelivery] = {}


def _stash_pending_token(
    code_hash: str,
    token: str,
    expires_at: datetime,
    admin_key: str | None = None,
) -> None:
    now = datetime.now(UTC)
    with _PENDING_LOCK:
        for key, entry in list(_PENDING_TOKENS.items()):
            if entry.expires_at <= now:
                _PENDING_TOKENS.pop(key, None)
        _PENDING_TOKENS[code_hash] = _PendingDelivery(token, expires_at, admin_key)


def _pop_pending_token(code_hash: str) -> _PendingDelivery | None:
    with _PENDING_LOCK:
        entry = _PENDING_TOKENS.pop(code_hash, None)
    if entry is None:
        return None
    if entry.expires_at <= datetime.now(UTC):
        return None
    return entry


def _clear_pending_tokens() -> None:
    """测试挂钩：模拟进程重启后内存明文丢失。"""
    with _PENDING_LOCK:
        _PENDING_TOKENS.clear()


class ConsoleLoginCodeStore:
    """一次性 code 的状态存储；复用 AUTH DB 的连接与迁移惯例。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(Path(database_path).expanduser())

    def mint(
        self, *, user_id: str, admin_key: str | None = None
    ) -> MintedConsoleLoginCode:
        """签发一次性 code 并换发绑定的 console token；明文只随返回值存在。"""
        token_store = AuthTokenStore(self.database_path)
        created = token_store.create_token(
            name="console 一次性登录",
            user_id=user_id,
            role="console",
        )
        try:
            return self._insert_code(
                user_id=user_id,
                token_id=created.record.token_id,
                token=created.token,
                token_owned=True,
                admin_key=admin_key,
            )
        except (OSError, sqlite3.Error):
            # code 落库失败时吊销刚换发的 token，不留无人持有的 console 凭证。
            _revoke_quietly(token_store, created.record.token_id)
            raise

    def mint_for_token(
        self, token: str, *, admin_key: str | None = None
    ) -> MintedConsoleLoginCode:
        """为一枚既有、仍有效的 console token 签发一次性 code。

        供同进程宿主（安卓 App）把首次登录密钥交付给本机浏览器：交换后浏览器
        拿到的就是这枚 token 本身，不新增 token；code 过期也不吊销它。
        """
        record = AuthTokenStore(self.database_path).authenticate(token)
        if record is None or record.role != "console":
            raise AuthStoreError("登录密钥无效、已吊销或不是 console 角色")
        return self._insert_code(
            user_id=record.user_id,
            token_id=record.token_id,
            token=token,
            token_owned=False,
            admin_key=admin_key,
        )

    def _insert_code(
        self,
        *,
        user_id: str,
        token_id: str,
        token: str,
        token_owned: bool,
        admin_key: str | None,
    ) -> MintedConsoleLoginCode:
        code = f"mgc_{secrets.token_urlsafe(24)}"
        code_hash = _hash_secret(code)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=CONSOLE_LOGIN_CODE_TTL_SECONDS)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._retire_expired(connection, now)
            connection.execute(
                """
                INSERT INTO console_login_codes(
                    code_hash, user_id, console_token_id,
                    created_at, expires_at, used_at, token_owned
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    code_hash,
                    user_id,
                    token_id,
                    _iso(now),
                    _iso(expires),
                    1 if token_owned else 0,
                ),
            )
            connection.commit()
        _stash_pending_token(code_hash, token, expires, admin_key)
        return MintedConsoleLoginCode(
            code=code,
            token_id=token_id,
            expires_at=_iso(expires),
        )

    def exchange(self, code: str) -> ExchangedConsoleLogin | None:
        """交付绑定的 console token 明文，全程只有一次；任何失败都返回 None。"""
        if not code or len(code) > _CODE_MAX_LENGTH:
            return None
        code_hash = _hash_secret(code)
        now = datetime.now(UTC)
        with self._connect() as connection:
            # BEGIN IMMEDIATE 串行化并发交换：第二个请求必然看到 used_at。
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT user_id, console_token_id, expires_at, used_at, token_owned
                FROM console_login_codes
                WHERE code_hash = ?
                """,
                (code_hash,),
            ).fetchone()
            if row is None or row["used_at"] is not None:
                connection.rollback()
                return None
            token_id = str(row["console_token_id"])
            user_id = str(row["user_id"])
            token_owned = bool(row["token_owned"])
            if _parse_instant(str(row["expires_at"])) <= now:
                # 过期即作废：由 code 换发的 token 一并吊销，避免留下无人持有的凭证；
                # 只是交付既有 token 的 code 则不能动那枚 token。
                self._mark_used(connection, code_hash, now)
                if token_owned:
                    self._revoke_token(connection, token_id, now)
                connection.commit()
                return None
            if not self._mark_used(connection, code_hash, now):
                connection.rollback()
                return None
            connection.commit()
        pending = _pop_pending_token(code_hash)
        if pending is None:
            # 进程重启导致内存明文丢失：code 已作废；由它换发的 token 也必须吊销。
            if token_owned:
                _revoke_quietly(AuthTokenStore(self.database_path), token_id)
            return None
        return ExchangedConsoleLogin(
            token=pending.token,
            token_id=token_id,
            user_id=user_id,
            admin_key=pending.admin_key,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            factory=_ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @classmethod
    def _retire_expired(
        cls, connection: sqlite3.Connection, now: datetime
    ) -> None:
        rows = connection.execute(
            "SELECT code_hash, console_token_id, token_owned FROM console_login_codes "
            "WHERE used_at IS NULL AND expires_at <= ?",
            (_iso(now),),
        ).fetchall()
        for row in rows:
            cls._mark_used(connection, str(row["code_hash"]), now)
            if bool(row["token_owned"]):
                cls._revoke_token(connection, str(row["console_token_id"]), now)

    @staticmethod
    def _mark_used(
        connection: sqlite3.Connection, code_hash: str, now: datetime
    ) -> bool:
        cursor = connection.execute(
            "UPDATE console_login_codes SET used_at = ? "
            "WHERE code_hash = ? AND used_at IS NULL",
            (_iso(now), code_hash),
        )
        return bool(cursor.rowcount)

    @staticmethod
    def _revoke_token(
        connection: sqlite3.Connection, token_id: str, now: datetime
    ) -> None:
        connection.execute(
            "UPDATE auth_tokens SET revoked_at = ? "
            "WHERE substr(token_hash, 1, 16) = ? AND revoked_at IS NULL",
            (_iso(now), token_id),
        )


def _revoke_quietly(token_store: AuthTokenStore, token_id: str) -> None:
    # 尽力吊销：失败不掩盖主流程结果，已失效的 code 本身也无法再交换。
    try:
        token_store.revoke_token(token_id)
    except (AuthStoreError, OSError, sqlite3.Error):
        pass


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
