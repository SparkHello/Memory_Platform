from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import config_payload
import model_gateway.config_store as config_store
from model_gateway.config_store import (
    ConfigConflict,
    commit_control_plane,
    configuration_revision,
    gateway_paths,
    initialize,
    load_config,
    read_secrets,
    recover_control_plane,
    source_revision,
    write_secrets,
)
from model_gateway.models import GatewayConfig, RouteConfig
from model_gateway.service import create_app


def _competing_commit(
    home: str,
    expected_revision: str,
    port: int,
    barrier: object,
    queue: object,
) -> None:
    paths = gateway_paths(home)
    config = load_config(paths.config)
    payload = config.model_dump(mode="python", exclude_none=False)
    payload["server"]["port"] = port
    candidate = GatewayConfig.model_validate(payload)
    barrier.wait()  # type: ignore[attr-defined]
    try:
        commit_control_plane(
            paths,
            expected_revision=expected_revision,
            config=candidate,
        )
    except ConfigConflict:
        queue.put("stale")  # type: ignore[attr-defined]
    else:
        queue.put("committed")  # type: ignore[attr-defined]


def test_v1_load_migrates_to_explicit_v2_without_changing_fallback(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "home")
    paths.home.mkdir(parents=True)
    payload = config_payload()
    payload["schema_version"] = 1
    payload["connections"]["official"]["timeout_seconds"] = 42
    paths.config.write_text(json.dumps(payload), encoding="utf-8")

    config = load_config(paths.config)

    assert config.schema_version == 2
    assert config.connections["official"].connect_timeout_seconds == 30
    assert config.connections["official"].read_timeout_seconds == 42
    assert config.routes["memory.chat"].fallback_scope == "any_channel"
    assert RouteConfig(targets=["only"]).fallback_scope == "none"


def test_cross_process_revision_cas_allows_exactly_one_writer(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    revision = configuration_revision(paths.config)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    workers = [
        context.Process(
            target=_competing_commit,
            args=(str(paths.home), revision, port, barrier, queue),
        )
        for port in (2041, 2042)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0

    assert sorted([queue.get(timeout=2), queue.get(timeout=2)]) == [
        "committed",
        "stale",
    ]
    assert load_config(paths.config).server.port in {2041, 2042}


@pytest.mark.parametrize("phase", ["prepared", "secret_applied", "config_applied"])
def test_crash_journal_restores_both_config_and_secrets(
    tmp_path: Path, phase: str
) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    write_secrets(paths.secrets, {"OLD": "old-value"})
    original = load_config(paths.config)
    payload = original.model_dump(mode="python", exclude_none=False)
    payload["server"]["port"] = 2099
    candidate = GatewayConfig.model_validate(payload)

    with pytest.raises(BaseException, match=phase):
        commit_control_plane(
            paths,
            expected_revision=source_revision(original, paths.config),
            config=candidate,
            secret_updates={"OLD": "new-value", "ORPHAN": "candidate-value"},
            _crash_after=phase,
        )

    assert paths.journal.exists()
    assert recover_control_plane(paths) is True
    assert load_config(paths.config).server.port == 2030
    assert read_secrets(paths.secrets) == {"OLD": "old-value"}
    assert not paths.journal.exists()
    assert not list(paths.home.glob(".*.txn-*.before"))


@pytest.mark.parametrize(
    "recovery_phase",
    ["config_rolled_back", "secrets_rolled_back"],
)
def test_second_crash_during_uncommitted_recovery_is_idempotent(
    tmp_path: Path,
    recovery_phase: str,
) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    write_secrets(paths.secrets, {"OLD": "old-value"})
    original = load_config(paths.config)
    payload = original.model_dump(mode="python", exclude_none=False)
    payload["server"]["port"] = 2099
    candidate = GatewayConfig.model_validate(payload)

    with pytest.raises(BaseException, match="config_applied"):
        commit_control_plane(
            paths,
            expected_revision=source_revision(original, paths.config),
            config=candidate,
            secret_updates={"OLD": "new-value", "ADDED": "candidate-value"},
            _crash_after="config_applied",
        )

    with pytest.raises(BaseException, match=recovery_phase):
        config_store._recover_control_plane_unlocked(
            paths,
            _crash_after=recovery_phase,
        )

    # Recovery may already have restored one or both live files, but it must
    # retain every before-image and the journal until the whole rollback is
    # durably complete.
    assert paths.journal.exists()
    assert len(list(paths.home.glob(".*.txn-*.before"))) == 2
    assert recover_control_plane(paths) is True
    assert load_config(paths.config).server.port == 2030
    assert read_secrets(paths.secrets) == {"OLD": "old-value"}
    assert not paths.journal.exists()
    assert not list(paths.home.glob(".*.txn-*.before"))


def test_crash_after_durable_commit_keeps_candidate_pair(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    write_secrets(paths.secrets, {"OLD": "old-value"})
    original = load_config(paths.config)
    payload = original.model_dump(mode="python", exclude_none=False)
    payload["server"]["port"] = 2099
    candidate = GatewayConfig.model_validate(payload)

    with pytest.raises(BaseException, match="committed"):
        commit_control_plane(
            paths,
            expected_revision=source_revision(original, paths.config),
            config=candidate,
            secret_updates={"OLD": "new-value", "ADDED": "candidate-value"},
            _crash_after="committed",
        )

    assert paths.journal.exists()
    assert recover_control_plane(paths) is True
    assert load_config(paths.config).server.port == 2099
    assert read_secrets(paths.secrets) == {
        "ADDED": "candidate-value",
        "OLD": "new-value",
    }
    assert not paths.journal.exists()
    assert not list(paths.home.glob(".*.txn-*.before"))


@pytest.mark.parametrize(
    "phase",
    ["config_backup_deleted", "secrets_backup_deleted"],
)
def test_crash_during_committed_cleanup_keeps_candidate_pair(
    tmp_path: Path,
    phase: str,
) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    write_secrets(paths.secrets, {"OLD": "old-value"})
    original = load_config(paths.config)
    payload = original.model_dump(mode="python", exclude_none=False)
    payload["server"]["port"] = 2099
    candidate = GatewayConfig.model_validate(payload)

    with pytest.raises(BaseException, match=phase):
        commit_control_plane(
            paths,
            expected_revision=source_revision(original, paths.config),
            config=candidate,
            secret_updates={"OLD": "new-value", "ADDED": "candidate-value"},
            _crash_after=phase,
        )

    assert paths.journal.exists()
    assert recover_control_plane(paths) is True
    assert load_config(paths.config).server.port == 2099
    assert read_secrets(paths.secrets) == {
        "ADDED": "candidate-value",
        "OLD": "new-value",
    }
    assert not paths.journal.exists()
    assert not list(paths.home.glob(".*.txn-*.before"))


def test_committed_cleanup_fsyncs_external_secret_store_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_secrets = tmp_path / "separate-secret-volume" / "secrets.env"
    monkeypatch.setenv("MODEL_GATEWAY_SECRETS_PATH", str(external_secrets))
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    original = load_config(paths.config)
    payload = original.model_dump(mode="python", exclude_none=False)
    payload["server"]["port"] = 2099
    candidate = GatewayConfig.model_validate(payload)
    fsynced: list[Path] = []

    monkeypatch.setattr(
        "model_gateway.config_store._fsync_directory",
        lambda path: fsynced.append(path),
    )

    commit_control_plane(
        paths,
        expected_revision=source_revision(original, paths.config),
        config=candidate,
        secret_updates={"ADDED": "candidate-value"},
    )

    assert external_secrets.parent in fsynced
    assert paths.home in fsynced
    assert not list(external_secrets.parent.glob(".*.txn-*.before"))


def test_channel_bundle_validates_key_then_commits_atomically(gateway_home) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == "Bearer wrong-key":
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "author/chat-v1"},
                    {"id": "author/embed-v1"},
                    {"id": "author/chat-next"},
                ]
            },
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        revision = client.get(
            "/admin/configuration",
            headers={"authorization": "Bearer admin-token"},
        ).json()["revision"]
        base = {
            "revision": revision,
            "connection": {
                "id": "official",
                "channel_operator": "official-vendor",
                "base_url": "https://official.example/v1",
                "secret": "wrong-key",
            },
            "deployments": [
                {
                    "id": "chat-next",
                    "upstream_model": "author/chat-next",
                    "tool_choice_with_reasoning": "any",
                    "capabilities": {"tools": True, "reasoning": True},
                    "pricing": "chat-next-price",
                },
                {
                    "id": "embed-next",
                    "upstream_model": "author/embed-v1",
                    "kind": "embedding",
                    "dimensions": 4,
                    "capabilities": {"streaming": False},
                },
            ],
            "pricing": [
                {
                    "id": "chat-next-price",
                    "value": {
                        "mode": "per_token",
                        "currency": "CNY",
                        "tiers": [{"input": "1", "output": "2"}],
                        "source_url": "https://official.example/pricing",
                    },
                }
            ],
            "routes": [
                {"id": "memory.chat"},
                {
                    "id": "knowledge.fast",
                    "operation": "replace",
                    "targets": ["$0"],
                    "fallback_scope": "none",
                },
            ],
        }
        rejected = client.post(
            "/admin/channel-bundles/apply",
            headers={"authorization": "Bearer admin-token"},
            json=base,
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["type"] == (
            "model_gateway_candidate_key_rejected"
        )
        assert configuration_revision(gateway_home.config) == revision
        assert read_secrets(gateway_home.secrets)["UPSTREAM_OFFICIAL"] == (
            "official-secret"
        )

        valid_body = json.loads(json.dumps(base))
        valid_body["connection"]["secret"] = "replacement-secret"
        preview = client.post(
            "/admin/channel-bundles/validate",
            headers={"authorization": "Bearer admin-token"},
            json=valid_body,
        )
        assert preview.status_code == 200
        assert preview.json()["applied"] is False
        assert configuration_revision(gateway_home.config) == revision
        assert "chat-next" not in load_config(gateway_home.config).deployments

        applied = client.post(
            "/admin/channel-bundles/apply",
            headers={"authorization": "Bearer admin-token"},
            json=valid_body,
        )
        assert applied.status_code == 200
        assert applied.json()["changed_routes"] == ["knowledge.fast"]

    config = load_config(gateway_home.config)
    assert config.schema_version == 2
    assert config.deployments["chat-next"].adapter_profile == "inherit"
    assert config.deployments["chat-next"].tool_choice_with_reasoning == "any"
    assert config.deployments["chat-next"].model_author == "unknown"
    assert config.deployments["embed-next"].embedding_space.startswith(
        "mgw-embedding-v1-4-"
    )
    assert config.routes["knowledge.fast"].fallback_scope == "none"
    assert config.routes["memory.chat"].targets == [
        "chat-official",
        "chat-reseller",
    ]
    assert read_secrets(gateway_home.secrets)["UPSTREAM_OFFICIAL"] == (
        "replacement-secret"
    )
    assert not gateway_home.journal.exists()


def test_ready_requires_a_routable_configured_provider(tmp_path: Path, gateway_home) -> None:
    empty_paths = gateway_paths(tmp_path / "empty")
    empty_app = create_app(
        paths=empty_paths,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(empty_app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["reason"] == (
            "no_enabled_route_with_configured_provider"
        )

    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        write_secrets(
            gateway_home.secrets,
            {
                "CLIENT_MEMORY_CONSOLE_ADMIN": "admin-token",
                "UPSTREAM_OFFICIAL": "official-secret",
                "UPSTREAM_RESELLER": "reseller-secret",
            },
        )
        assert client.get("/readyz").status_code == 503
        write_secrets(
            gateway_home.secrets,
            {
                "CLIENT_MEMORY_GATEWAY": "local-client-token",
                "CLIENT_DESKTOP": "desktop-token",
                "CLIENT_MEMORY_CONSOLE_ADMIN": "admin-token",
            },
        )
        response = client.get("/readyz")
        assert response.status_code == 503


def test_ready_fails_closed_when_disk_config_reload_is_broken(gateway_home) -> None:
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        marker = "https://user:READY-SECRET@invalid.example/v1"
        gateway_home.config.write_text(
            json.dumps({"schema_version": 2, "connections": {"bad": marker}}),
            encoding="utf-8",
        )
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {
            "status": "not_ready",
            "reason": "configuration_reload_failed",
        }
        assert "READY-SECRET" not in response.text


def test_admin_lists_and_manages_unreferenced_objects(gateway_home) -> None:
    base = load_config(gateway_home.config)
    payload = base.model_dump(mode="python", exclude_none=False)
    payload["connections"]["orphan-channel"] = {
        "channel_operator": "orphan",
        "base_url": "https://orphan.example/v1",
        "auth": {"secret_ref": "UPSTREAM_ORPHAN"},
    }
    payload["deployments"]["orphan-deployment"] = {
        "connection": "official",
        "upstream_model": "author/orphan",
        "model_author": "unknown",
    }
    payload["pricing"]["orphan-pricing"] = {
        "mode": "unknown",
        "currency": "USD",
    }
    candidate = GatewayConfig.model_validate(payload)
    commit_control_plane(
        gateway_home,
        expected_revision=source_revision(base, gateway_home.config),
        config=candidate,
        secret_updates={"UPSTREAM_ORPHAN": "orphan-secret"},
    )

    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        admin = client.get(
            "/admin/configuration",
            headers={"authorization": "Bearer admin-token"},
        ).json()
        assert "orphan-channel" in {item["id"] for item in admin["connections"]}
        assert "orphan-deployment" in {
            item["id"] for item in admin["deployments"]
        }
        assert "orphan-pricing" in {item["id"] for item in admin["pricing"]}

        filtered = client.get(
            "/admin/configuration",
            headers={"authorization": "Bearer local-client-token"},
        ).json()
        assert "orphan-channel" not in {
            item["id"] for item in filtered["connections"]
        }

        blocked = client.request(
            "DELETE",
            "/admin/connections/official",
            headers={"authorization": "Bearer admin-token"},
            json={"revision": admin["revision"]},
        )
        assert blocked.status_code == 409

        disabled = client.patch(
            "/admin/deployments/orphan-deployment",
            headers={"authorization": "Bearer admin-token"},
            json={"revision": admin["revision"], "enabled": False},
        )
        assert disabled.status_code == 200
        revision = disabled.json()["revision"]

        for collection, item_id in (
            ("deployments", "orphan-deployment"),
            ("pricing", "orphan-pricing"),
            ("connections", "orphan-channel"),
        ):
            deleted = client.request(
                "DELETE",
                f"/admin/{collection}/{item_id}",
                headers={"authorization": "Bearer admin-token"},
                json={"revision": revision},
            )
            assert deleted.status_code == 200
            revision = deleted.json()["revision"]

    config = load_config(gateway_home.config)
    assert "orphan-channel" not in config.connections
    assert "orphan-deployment" not in config.deployments
    assert "orphan-pricing" not in config.pricing
    assert "UPSTREAM_ORPHAN" not in read_secrets(gateway_home.secrets)
