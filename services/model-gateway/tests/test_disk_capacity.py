from __future__ import annotations

import errno
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from model_gateway import config_store, storage
from model_gateway.config_store import (
    commit_control_plane,
    configuration_revision,
    gateway_paths,
    load_config,
    read_secrets,
)
from model_gateway.models import GatewayConfig, ServerConfig
from model_gateway.service import create_app

from conftest import ADMIN_CLIENT_TOKEN, BACKEND_CLIENT_TOKEN


MIB = 1024 * 1024


def _disk_usage(*, total: int, free: int) -> SimpleNamespace:
    return SimpleNamespace(total=total, used=total - free, free=free)


def test_storage_reserve_defaults_v1_migration_and_small_volume_adaptation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ServerConfig()
    assert server.disk_soft_reserve_bytes == 64 * MIB
    assert server.disk_hard_reserve_bytes == 16 * MIB
    migrated = GatewayConfig.model_validate({"schema_version": 1})
    assert migrated.server.disk_soft_reserve_bytes == 64 * MIB
    assert migrated.server.disk_hard_reserve_bytes == 16 * MIB

    target = tmp_path / "ledger.db"
    target.write_bytes(b"")
    monkeypatch.setattr(
        storage.shutil,
        "disk_usage",
        lambda _path: _disk_usage(total=64 * MIB, free=8 * MIB),
    )
    capacity = storage.disk_capacity_for_path(target, server)
    assert capacity.soft_reserve_bytes == 4 * MIB
    assert capacity.hard_reserve_bytes == 1 * MIB

    disabled = ServerConfig(
        disk_soft_reserve_bytes=0,
        disk_hard_reserve_bytes=0,
    )
    capacity = storage.disk_capacity_for_path(target, disabled)
    assert capacity.soft_reserve_bytes == 0
    assert capacity.hard_reserve_bytes == 0

    with pytest.raises(ValidationError, match="disk_soft_reserve_bytes"):
        ServerConfig(
            disk_soft_reserve_bytes=1,
            disk_hard_reserve_bytes=2,
        )


def test_fresh_app_initialization_creates_all_storage_files(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "fresh-home")
    app = create_app(
        paths=paths,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    assert paths.config.is_file()
    assert paths.secrets.is_file()
    assert paths.usage_db.is_file()
    assert storage.storage_readiness_reason(
        paths,
        load_config(paths.config).server,
        usage_probe=app.state.usage_store,
    ) == ""


def test_readyz_reports_safe_disk_low_without_paths(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    monkeypatch.setattr(
        storage.shutil,
        "disk_usage",
        lambda _path: _disk_usage(total=10 * 1024 * MIB, free=32 * MIB),
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "disk_low"}
    assert str(gateway_home.home) not in response.text
    assert "33554432" not in response.text


def test_readyz_reports_disk_unavailable_when_ledger_probe_fails(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    def unavailable() -> None:
        raise sqlite3.OperationalError("unable to open database file at SECRET-PATH")

    monkeypatch.setattr(app.state.usage_store, "probe_writable", unavailable)
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "reason": "disk_unavailable",
    }
    assert "SECRET-PATH" not in response.text


def test_usage_init_storage_failure_keeps_liveness_for_safe_readyz(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from model_gateway.usage import UsageStore

    def fail_init(_self) -> None:
        raise sqlite3.OperationalError(
            "unable to open database file SECRET-PATH"
        )

    monkeypatch.setattr(UsageStore, "init_db", fail_init)
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "reason": "disk_unavailable",
    }
    assert "SECRET-PATH" not in response.text


def test_v1_paid_request_preflight_returns_507_with_zero_provider_calls(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, json={"choices": []})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        storage.shutil,
        "disk_usage",
        lambda _path: _disk_usage(total=10 * 1024 * MIB, free=8 * MIB),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {BACKEND_CLIENT_TOKEN}"},
            json={"model": "memory.chat", "messages": []},
        )

    assert response.status_code == 507
    assert response.json()["error"]["code"] == (
        "model_gateway_insufficient_storage"
    )
    assert response.json()["error"]["attempts"] == 0
    assert response.headers["x-model-gateway-attempts"] == "0"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert provider_calls == 0
    with sqlite3.connect(gateway_home.usage_db) as connection:
        assert connection.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM attempt_events").fetchone()[0] == 0


def test_v1_ledger_writability_preflight_returns_507_with_zero_provider_calls(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, json={"choices": []})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))

    def unavailable() -> None:
        raise sqlite3.OperationalError("private ledger path")

    monkeypatch.setattr(app.state.usage_store, "probe_writable", unavailable)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {BACKEND_CLIENT_TOKEN}"},
            json={"model": "memory.chat", "messages": []},
        )

    assert response.status_code == 507
    assert response.json()["error"]["code"] == (
        "model_gateway_insufficient_storage"
    )
    assert response.json()["error"]["attempts"] == 0
    assert provider_calls == 0
    assert "private ledger path" not in response.text
    with sqlite3.connect(gateway_home.usage_db) as connection:
        assert connection.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM attempt_events").fetchone()[0] == 0


def test_capability_probe_preflight_returns_507_with_zero_provider_calls(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, json={"choices": []})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))

    def unavailable() -> None:
        raise sqlite3.OperationalError("private ledger path")

    monkeypatch.setattr(app.state.usage_store, "probe_writable", unavailable)
    with TestClient(app) as client:
        response = client.post(
            "/admin/channels/probe-capabilities",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            json={
                "revision": configuration_revision(gateway_home.config),
                "candidate_key": "candidate-provider-secret",
                "channel_operator": "official-vendor",
                "base_url": "https://official.example/v1",
                "adapter": "generic",
                "auth_type": "bearer",
                "allowed_private_networks": [],
                "upstream_model": "author/chat-v1",
                "probes": ["chat"],
            },
        )

    assert response.status_code == 507
    assert response.json()["error"]["code"] == (
        "model_gateway_insufficient_storage"
    )
    assert response.json()["error"]["attempts"] == 0
    assert provider_calls == 0
    assert "private ledger path" not in response.text


def test_capability_probe_ledger_failure_keeps_result_and_latches_not_ready(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))

    def fail_record(**_kwargs) -> str:
        raise sqlite3.OperationalError("private ledger path")

    monkeypatch.setattr(app.state.usage_store, "record", fail_record)
    with TestClient(app) as client:
        response = client.post(
            "/admin/channels/probe-capabilities",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            json={
                "revision": configuration_revision(gateway_home.config),
                "candidate_key": "candidate-provider-secret",
                "channel_operator": "official-vendor",
                "base_url": "https://official.example/v1",
                "adapter": "generic",
                "auth_type": "bearer",
                "allowed_private_networks": [],
                "upstream_model": "author/chat-v1",
                "probes": ["chat"],
            },
        )
        first_ready = client.get("/readyz")
        second_ready = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["usage_ledger_status"] == "incomplete"
    assert response.json()["warnings"]
    assert provider_calls == 1
    assert "private ledger path" not in response.text
    assert first_ready.status_code == 503
    assert first_ready.json() == {
        "status": "not_ready",
        "reason": "disk_unavailable",
    }
    assert second_ready.status_code == 200


def test_post_provider_sqlite_full_keeps_upstream_result_and_latches_not_ready(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0
    upstream_body = (
        b'{"choices": [ ], "usage": '
        b'{"prompt_tokens": 1, "completion_tokens": 1}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(
            200,
            content=upstream_body,
            headers={"content-type": "application/json"},
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))

    def full(**_kwargs) -> str:
        exc = sqlite3.OperationalError("database or disk is full SECRET-BODY")
        exc.sqlite_errorcode = sqlite3.SQLITE_FULL
        raise exc

    monkeypatch.setattr(app.state.usage_store, "record", full)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {BACKEND_CLIENT_TOKEN}"},
            json={"model": "memory.chat", "messages": []},
        )
        first_ready = client.get("/readyz")
        second_ready = client.get("/readyz")

    assert provider_calls == 1
    assert response.status_code == 200
    assert response.content == upstream_body
    assert response.headers["x-model-gateway-usage-ledger-status"] == "incomplete"
    assert "x-model-gateway-usage-event-id" not in response.headers
    assert "SECRET-BODY" not in response.text
    assert first_ready.status_code == 503
    assert first_ready.json() == {
        "status": "not_ready",
        "reason": "disk_unavailable",
    }
    assert second_ready.status_code == 200


def test_stream_runtime_ledger_failure_latches_not_ready(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_body = (
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        b'data: [DONE]\n\n'
    )

    class OneChunkStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield upstream_body

        async def aclose(self) -> None:
            return None

    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                stream=OneChunkStream(),
                headers={"content-type": "text/event-stream"},
            )
        ),
    )

    def fail_record(**_kwargs) -> str:
        raise RuntimeError("non-storage ledger callback failure")

    monkeypatch.setattr(app.state.usage_store, "record", fail_record)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {BACKEND_CLIENT_TOKEN}"},
            json={"model": "memory.chat", "messages": [], "stream": True},
        )
        first_ready = client.get("/readyz")
        second_ready = client.get("/readyz")

    assert response.status_code == 200
    assert response.content == upstream_body
    assert response.headers["x-model-gateway-usage-ledger-status"] == "deferred"
    assert first_ready.status_code == 503
    assert first_ready.json() == {
        "status": "not_ready",
        "reason": "disk_unavailable",
    }
    assert second_ready.status_code == 200


def test_mixed_control_plane_enospc_rolls_back_config_and_secret(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_revision = configuration_revision(gateway_home.config)
    original_config = gateway_home.config.read_bytes()
    original_secrets = read_secrets(gateway_home.secrets)
    candidate = load_config(gateway_home.config)
    candidate.server.port = 2099
    real_atomic_write = config_store._atomic_write

    def fail_candidate_write(path, content, mode, *, backup=True):
        if path == gateway_home.config:
            raise OSError(errno.ENOSPC, "SECRET-CONTENT must not leak")
        return real_atomic_write(path, content, mode, backup=backup)

    monkeypatch.setattr(config_store, "_atomic_write", fail_candidate_write)
    with pytest.raises(OSError):
        commit_control_plane(
            gateway_home,
            expected_revision=original_revision,
            config=candidate,
            secret_updates={"UPSTREAM_OFFICIAL": "candidate-secret"},
        )

    assert gateway_home.config.read_bytes() == original_config
    assert read_secrets(gateway_home.secrets) == original_secrets
    assert not gateway_home.journal.exists()
    assert not list(gateway_home.home.glob(".*.txn-*.before"))


def test_control_plane_journal_enospc_leaves_no_partial_transaction(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = gateway_home.config.read_bytes()
    revision = configuration_revision(gateway_home.config)
    candidate = load_config(gateway_home.config)
    candidate.server.port = 2098
    real_atomic_write = config_store._atomic_write

    def fail_journal(path, content, mode, *, backup=True):
        if path == gateway_home.journal:
            raise OSError(errno.ENOSPC, "PRIVATE-CONTENT")
        return real_atomic_write(path, content, mode, backup=backup)

    monkeypatch.setattr(config_store, "_atomic_write", fail_journal)
    with pytest.raises(OSError):
        commit_control_plane(
            gateway_home,
            expected_revision=revision,
            config=candidate,
        )

    assert gateway_home.config.read_bytes() == original
    assert not gateway_home.journal.exists()
    assert not list(gateway_home.home.glob(".*.txn-*.before"))


def test_admin_secret_enospc_is_stable_507_and_old_secret_survives(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = read_secrets(gateway_home.secrets)
    real_atomic_write = config_store._atomic_write

    def fail_secret(path, content, mode, *, backup=True):
        if path == gateway_home.secrets:
            raise OSError(errno.ENOSPC, "SECRET-VALUE")
        return real_atomic_write(path, content, mode, backup=backup)

    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"id": "author/chat-v1"}]},
            )
        ),
    )
    monkeypatch.setattr(config_store, "_atomic_write", fail_secret)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put(
            "/admin/connections/official/secret",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            json={"value": "candidate-secret"},
        )

    assert response.status_code == 507
    assert response.json()["detail"]["code"] == (
        "model_gateway_insufficient_storage"
    )
    assert "SECRET-VALUE" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "candidate-secret" not in response.text
    assert read_secrets(gateway_home.secrets) == original
    assert not gateway_home.journal.exists()
    assert not list(gateway_home.home.glob(".*.txn-*.before"))


def test_admin_config_enospc_is_stable_507_and_old_revision_survives(
    gateway_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = configuration_revision(gateway_home.config)
    original = gateway_home.config.read_bytes()
    real_atomic_write = config_store._atomic_write

    def fail_config(path, content, mode, *, backup=True):
        if path == gateway_home.config:
            raise OSError(errno.ENOSPC, "SECRET-PATH")
        return real_atomic_write(path, content, mode, backup=backup)

    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    monkeypatch.setattr(config_store, "_atomic_write", fail_config)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put(
            "/admin/routes",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            content=json.dumps(
                {
                    "revision": revision,
                    "routes": [
                        {
                            "id": "memory.chat",
                            "targets": ["chat-reseller", "chat-official"],
                            "enabled": True,
                        }
                    ],
                }
            ),
        )

    assert response.status_code == 507
    assert response.json()["detail"]["code"] == (
        "model_gateway_insufficient_storage"
    )
    assert "SECRET-PATH" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert gateway_home.config.read_bytes() == original
    assert configuration_revision(gateway_home.config) == revision
    assert not gateway_home.journal.exists()
    assert not list(gateway_home.home.glob(".*.txn-*.before"))
