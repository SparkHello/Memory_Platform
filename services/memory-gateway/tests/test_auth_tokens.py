from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import sqlite3
import threading

import pytest

from app.auth.tokens import (
    AuthStoreError,
    AuthTokenStore,
    LastActiveConsoleTokenError,
)


def test_scoped_token_store_hashes_secrets_and_revokes_immediately(tmp_path) -> None:
    database = tmp_path / "auth.db"
    store = AuthTokenStore(database)
    store.init_db()

    created = store.create_token(name="Alice phone", user_id="alice", role="chat")

    assert created.token.startswith(f"mgw_{created.record.token_id}_")
    secret = created.token.split("_", 2)[2]
    expected_hash = hashlib.sha256(secret.encode()).hexdigest()
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT token_hash, name, user_id, role FROM auth_tokens"
        ).fetchone()
    assert row == (expected_hash, "Alice phone", "alice", "chat")
    database_bytes = database.read_bytes()
    assert created.token.encode() not in database_bytes
    assert secret.encode() not in database_bytes
    assert os.stat(database).st_mode & 0o777 == 0o600

    authenticated = store.authenticate(created.token)
    assert authenticated is not None
    assert authenticated.user_id == "alice"
    assert authenticated.last_used_at is not None
    assert store.revoke_token(created.record.token_id) is True
    assert store.authenticate(created.token) is None
    assert store.revoke_token(created.record.token_id) is False
    assert store.list_tokens()[0].revoked_at is not None


def test_auth_store_list_exposes_neither_hash_nor_token(tmp_path) -> None:
    store = AuthTokenStore(tmp_path / "auth.db")
    store.init_db()
    created = store.create_token(name="Console", user_id="default", role="console")

    record = store.list_tokens()[0]

    assert not hasattr(record, "token")
    assert not hasattr(record, "token_hash")
    assert created.token not in repr(record)


def test_store_can_atomically_protect_each_users_last_console_token(tmp_path) -> None:
    store = AuthTokenStore(tmp_path / "auth.db")
    store.init_db()
    alice_first = store.create_token(
        name="Alice browser",
        user_id="alice",
        role="console",
    )
    alice_second = store.create_token(
        name="Alice backup",
        user_id="alice",
        role="console",
    )
    bob = store.create_token(name="Bob browser", user_id="bob", role="console")

    assert store.revoke_token(
        alice_first.record.token_id,
        user_id="alice",
        protect_last_console=True,
    )
    with pytest.raises(LastActiveConsoleTokenError, match="至少一个"):
        store.revoke_token(
            alice_second.record.token_id,
            user_id="alice",
            protect_last_console=True,
        )
    with pytest.raises(LastActiveConsoleTokenError):
        store.revoke_token(
            bob.record.token_id,
            user_id="bob",
            protect_last_console=True,
        )

    assert store.authenticate(alice_second.token) is not None
    assert store.authenticate(bob.token) is not None


def test_concurrent_console_revocations_cannot_remove_the_final_pair(tmp_path) -> None:
    store = AuthTokenStore(tmp_path / "auth.db")
    store.init_db()
    first = store.create_token(name="First", user_id="alice", role="console")
    second = store.create_token(name="Second", user_id="alice", role="console")
    barrier = threading.Barrier(2)

    def revoke(token_id: str) -> str:
        barrier.wait(timeout=5)
        try:
            return (
                "revoked"
                if store.revoke_token(
                    token_id,
                    user_id="alice",
                    protect_last_console=True,
                )
                else "unchanged"
            )
        except LastActiveConsoleTokenError:
            return "protected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                revoke,
                [first.record.token_id, second.record.token_id],
            )
        )

    assert sorted(results) == ["protected", "revoked"]
    active = [
        record
        for record in store.list_tokens(user_id="alice")
        if record.role == "console" and record.revoked_at is None
    ]
    assert len(active) == 1


def test_auth_schema_initialization_is_idempotent_and_rejects_future_version(
    tmp_path,
) -> None:
    database = tmp_path / "auth.db"
    store = AuthTokenStore(database)
    store.init_db()
    store.init_db()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(AuthStoreError, match="更高版本"):
        store.init_db()


@pytest.mark.parametrize("role", ["admin", "all", ""])
def test_auth_store_rejects_roles_outside_fixed_scope(tmp_path, role) -> None:
    store = AuthTokenStore(tmp_path / "auth.db")
    store.init_db()
    with pytest.raises(AuthStoreError, match="chat、mcp 或 console"):
        store.create_token(name="bad", user_id="default", role=role)
