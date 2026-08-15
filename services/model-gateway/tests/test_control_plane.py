from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import (
    ADMIN_CLIENT_TOKEN,
    BACKEND_CLIENT_TOKEN,
    DESKTOP_CLIENT_TOKEN,
    config_payload,
)
import model_gateway.config_store as config_store
from model_gateway.config_store import (
    ConfigConflict,
    ControlPlaneValidationError,
    commit_control_plane,
    configuration_revision,
    gateway_paths,
    initialize,
    load_config,
    read_secrets,
    recover_control_plane,
    source_revision,
    write_config,
    write_secrets,
)
from model_gateway.models import GatewayConfig, RouteConfig, derive_embedding_space
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


@pytest.mark.parametrize("group", ["remove", "set_if_missing", "force"])
def test_control_plane_rejects_new_protected_request_transform_atomically(
    tmp_path: Path,
    group: str,
) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    original = GatewayConfig.model_validate(config_payload())
    write_config(paths.config, original)
    before = paths.config.read_bytes()
    payload = original.model_dump(mode="python", exclude_none=False)
    value = ["tool_choice"] if group == "remove" else {"tool_choice": "auto"}
    payload["deployments"]["chat-official"]["request_transform"][group] = value
    candidate = GatewayConfig.model_validate(payload)

    with pytest.raises(ControlPlaneValidationError) as caught:
        commit_control_plane(
            paths,
            expected_revision=source_revision(original, paths.config),
            config=candidate,
        )

    assert caught.value.reason == "request_transform_invalid"
    assert paths.config.read_bytes() == before
    assert not paths.journal.exists()


def test_control_plane_preserves_but_cannot_modify_legacy_protected_transform(
    tmp_path: Path,
) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    payload = config_payload()
    payload["deployments"]["chat-official"]["request_transform"] = {
        "force": {"tool_choice": "auto"}
    }
    legacy = GatewayConfig.model_validate(payload)
    write_config(paths.config, legacy)

    unchanged_payload = legacy.model_dump(mode="python", exclude_none=False)
    unchanged_payload["server"]["port"] = 2099
    unchanged = GatewayConfig.model_validate(unchanged_payload)
    committed = commit_control_plane(
        paths,
        expected_revision=source_revision(legacy, paths.config),
        config=unchanged,
    )
    assert committed.config.server.port == 2099

    changed_payload = committed.config.model_dump(mode="python", exclude_none=False)
    changed_payload["deployments"]["chat-official"]["request_transform"][
        "force"
    ]["tool_choice"] = "required"
    changed = GatewayConfig.model_validate(changed_payload)
    with pytest.raises(ControlPlaneValidationError) as caught:
        commit_control_plane(
            paths,
            expected_revision=committed.revision,
            config=changed,
        )
    assert caught.value.reason == "request_transform_invalid"

    repaired_payload = committed.config.model_dump(mode="python", exclude_none=False)
    repaired_payload["deployments"]["chat-official"]["request_transform"] = {}
    repaired = GatewayConfig.model_validate(repaired_payload)
    repaired_commit = commit_control_plane(
        paths,
        expected_revision=committed.revision,
        config=repaired,
    )
    assert (
        repaired_commit.config.deployments[
            "chat-official"
        ].request_transform.protected_fields()
        == ()
    )


def test_control_plane_rejects_referenced_invalid_provider_secret_before_writes(
    tmp_path: Path,
) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    original = GatewayConfig.model_validate(config_payload())
    write_config(paths.config, original)
    write_secrets(paths.secrets, {"UPSTREAM_OFFICIAL": "valid-provider-secret"})
    config_before = paths.config.read_bytes()
    secrets_before = paths.secrets.read_bytes()

    with pytest.raises(ControlPlaneValidationError) as caught:
        commit_control_plane(
            paths,
            expected_revision=source_revision(original, paths.config),
            secret_updates={"UPSTREAM_OFFICIAL": "invalid\r\nheader"},
        )

    assert caught.value.reason == "provider_secret_invalid"
    assert paths.config.read_bytes() == config_before
    assert paths.secrets.read_bytes() == secrets_before
    assert not paths.journal.exists()


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
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
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
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
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
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            json=valid_body,
        )
        assert preview.status_code == 200
        assert preview.json()["applied"] is False
        assert configuration_revision(gateway_home.config) == revision
        assert "chat-next" not in load_config(gateway_home.config).deployments

        applied = client.post(
            "/admin/channel-bundles/apply",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            json=valid_body,
        )
        assert applied.status_code == 200
        assert applied.json()["changed_routes"] == ["knowledge.fast"]
        assert applied.json()["embedding_connection_id"] == applied.json()["connection_id"]

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


def _new_channel_bundle(
    *,
    revision: str = "a" * 64,
    embedding_base_url: str = "",
    include_embedding: bool = True,
) -> dict:
    deployments: list[dict] = [
        {
            "upstream_model": "author/chat-next",
            "kind": "chat",
            "capabilities": {"tools": True, "reasoning": True},
        }
    ]
    routes: list[dict] = [
        {
            "id": "memory.chat",
            "operation": "replace",
            "targets": ["$0"],
            "fallback_scope": "none",
        }
    ]
    if include_embedding:
        deployments.append(
            {
                "upstream_model": "author/embed-v1",
                "kind": "embedding",
                "dimensions": 4,
                "capabilities": {"streaming": False},
            }
        )
        routes.append(
            {
                "id": "memory.embedding",
                "operation": "replace",
                "kind": "embedding",
                "targets": ["$1"],
                "fallback_scope": "none",
            }
        )
    body: dict = {
        "revision": revision,
        "connection": {
            "channel_operator": "dashscope",
            "adapter": "dashscope_openai",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "secret": "sk-test-upstream-key",
        },
        "deployments": deployments,
        "routes": routes,
    }
    if embedding_base_url:
        body["embedding_base_url"] = embedding_base_url
    return body


def test_bundle_candidate_keeps_embedding_on_chat_when_url_matches() -> None:
    from model_gateway.admin import BundleApplyRequest, bundle_candidate

    config = GatewayConfig.model_validate(config_payload())
    same = "https://dashscope.aliyuncs.com/compatible-mode/v1/"
    candidate, chat_id, secret_ref, deployment_ids, _, embed_id = bundle_candidate(
        config,
        BundleApplyRequest.model_validate(
            _new_channel_bundle(embedding_base_url=same)
        ),
    )
    assert chat_id == embed_id == "dashscope-account"
    assert deployment_ids[0] in candidate.deployments
    assert candidate.deployments[deployment_ids[0]].connection == chat_id
    assert candidate.deployments[deployment_ids[1]].connection == chat_id
    assert candidate.connections[chat_id].auth.secret_ref == secret_ref


def test_bundle_candidate_splits_embedding_onto_sibling_connection() -> None:
    from model_gateway.admin import BundleApplyRequest, bundle_candidate

    config = GatewayConfig.model_validate(config_payload())
    embedding_url = "https://dashscope-embedding.example/v1"
    candidate, chat_id, secret_ref, deployment_ids, _, embed_id = bundle_candidate(
        config,
        BundleApplyRequest.model_validate(
            _new_channel_bundle(embedding_base_url=embedding_url)
        ),
    )
    assert chat_id == "dashscope-account"
    assert embed_id == "dashscope-embedding-account"
    chat = candidate.connections[chat_id]
    embed = candidate.connections[embed_id]
    assert chat.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert embed.base_url == embedding_url
    assert chat.auth.secret_ref == embed.auth.secret_ref == secret_ref
    assert candidate.deployments[deployment_ids[0]].connection == chat_id
    assert candidate.deployments[deployment_ids[1]].connection == embed_id
    space = candidate.deployments[deployment_ids[1]].embedding_space
    assert space.startswith("mgw-embedding-v1-4-")
    chat_space = derive_embedding_space(chat, "author/embed-v1", 4)
    embed_space = derive_embedding_space(embed, "author/embed-v1", 4)
    assert space == embed_space
    assert space != chat_space


def test_bundle_candidate_rejects_embedding_url_without_embedding_model() -> None:
    from model_gateway.admin import BundleApplyRequest, bundle_candidate

    config = GatewayConfig.model_validate(config_payload())
    request = BundleApplyRequest.model_validate(
        _new_channel_bundle(
            embedding_base_url="https://embed.example/v1",
            include_embedding=False,
        )
    )
    with pytest.raises(ValueError, match="仅在同时配置向量模型时有效"):
        bundle_candidate(config, request)


def test_bundle_candidate_rejects_invalid_embedding_url() -> None:
    from model_gateway.admin import BundleApplyRequest, bundle_candidate

    config = GatewayConfig.model_validate(config_payload())
    request = BundleApplyRequest.model_validate(
        _new_channel_bundle(embedding_base_url="http://public.example/v1")
    )
    with pytest.raises(ValueError, match="embedding_base_url 无效"):
        bundle_candidate(config, request)


def test_channel_bundle_apply_splits_embedding_and_discovers_chat_only(
    gateway_home,
) -> None:
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        return httpx.Response(
            200,
            json={"data": [{"id": "author/chat-next"}, {"id": "author/embed-v1"}]},
        )

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    embedding_url = "https://embed.example/v1"
    with TestClient(app) as client:
        revision = client.get(
            "/admin/configuration",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
        ).json()["revision"]
        applied = client.post(
            "/admin/channel-bundles/apply",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            json=_new_channel_bundle(
                revision=revision,
                embedding_base_url=embedding_url,
            ),
        )
        assert applied.status_code == 200, applied.text
        payload = applied.json()
        assert payload["applied"] is True
        assert payload["connection_id"] == "dashscope-account"
        assert payload["embedding_connection_id"] == "dashscope-embedding-account"

    config = load_config(gateway_home.config)
    assert config.connections["dashscope-account"].base_url.endswith(
        "compatible-mode/v1"
    )
    assert config.connections["dashscope-embedding-account"].base_url == embedding_url
    assert (
        config.connections["dashscope-account"].auth.secret_ref
        == config.connections["dashscope-embedding-account"].auth.secret_ref
    )
    chat_dep = config.deployments[payload["deployment_ids"][0]]
    embed_dep = config.deployments[payload["deployment_ids"][1]]
    assert chat_dep.connection == "dashscope-account"
    assert embed_dep.connection == "dashscope-embedding-account"
    assert seen_hosts
    assert "embed.example" not in seen_hosts
    assert "dashscope.aliyuncs.com" in seen_hosts
    secrets = read_secrets(gateway_home.secrets)
    assert secrets[config.connections["dashscope-account"].auth.secret_ref] == (
        "sk-test-upstream-key"
    )


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
                "CLIENT_MEMORY_CONSOLE_ADMIN": ADMIN_CLIENT_TOKEN,
                "UPSTREAM_OFFICIAL": "official-secret",
                "UPSTREAM_RESELLER": "reseller-secret",
            },
        )
        assert client.get("/readyz").status_code == 503
        write_secrets(
            gateway_home.secrets,
            {
                "CLIENT_MEMORY_GATEWAY": BACKEND_CLIENT_TOKEN,
                "CLIENT_DESKTOP": DESKTOP_CLIENT_TOKEN,
                "CLIENT_MEMORY_CONSOLE_ADMIN": ADMIN_CLIENT_TOKEN,
            },
        )
        response = client.get("/readyz")
        assert response.status_code == 503


def test_ready_ignores_targets_with_legacy_protected_transform(gateway_home) -> None:
    config = load_config(gateway_home.config)
    for deployment in config.deployments.values():
        deployment.request_transform.force["tool_choice"] = "auto"
    write_config(gateway_home.config, config)
    app = create_app(
        paths=gateway_home,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["reason"] == (
        "no_enabled_route_with_configured_provider"
    )


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
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
        ).json()
        assert "orphan-channel" in {item["id"] for item in admin["connections"]}
        assert "orphan-deployment" in {
            item["id"] for item in admin["deployments"]
        }
        assert "orphan-pricing" in {item["id"] for item in admin["pricing"]}

        filtered = client.get(
            "/admin/configuration",
            headers={"authorization": f"Bearer {BACKEND_CLIENT_TOKEN}"},
        ).json()
        assert "orphan-channel" not in {
            item["id"] for item in filtered["connections"]
        }

        blocked = client.request(
            "DELETE",
            "/admin/connections/official",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            json={"revision": admin["revision"]},
        )
        assert blocked.status_code == 409

        disabled = client.patch(
            "/admin/deployments/orphan-deployment",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
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
                headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
                json={"revision": revision},
            )
            assert deleted.status_code == 200
            revision = deleted.json()["revision"]

    config = load_config(gateway_home.config)
    assert "orphan-channel" not in config.connections
    assert "orphan-deployment" not in config.deployments
    assert "orphan-pricing" not in config.pricing
    assert "UPSTREAM_ORPHAN" not in read_secrets(gateway_home.secrets)
