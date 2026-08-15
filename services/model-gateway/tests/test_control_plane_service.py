from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_CLIENT_TOKEN
from model_gateway import admin
from model_gateway.config_store import (
    ConfigConflict,
    ControlPlaneValidationError,
    configuration_revision,
    load_config,
    read_secrets,
)
from model_gateway.control_plane import (
    BundleApplyRequest,
    ControlPlaneService,
)
from model_gateway.models import ClientConfig, GatewayConfig
from model_gateway.service import create_app


def test_admin_reexports_canonical_control_plane_dtos() -> None:
    assert admin.BundleApplyRequest is BundleApplyRequest


def test_invalid_combined_client_and_secret_candidate_never_writes(
    gateway_home,
) -> None:
    service = ControlPlaneService(gateway_home)
    snapshot = service.snapshot()
    config_before = gateway_home.config.read_bytes()
    secrets_before = gateway_home.secrets.read_bytes()
    revision_before = configuration_revision(gateway_home.config)
    client = ClientConfig(
        kind="backend",
        secret_ref="CLIENT_ATOMIC_NEW",
        allowed_routes=["memory.*"],
    )

    with pytest.raises(ControlPlaneValidationError) as caught:
        service.upsert_client(
            snapshot,
            client_id="atomic-new",
            client=client,
            secret_value="weak",
        )

    assert caught.value.reason == "client_secret_invalid"
    assert gateway_home.config.read_bytes() == config_before
    assert gateway_home.secrets.read_bytes() == secrets_before
    assert configuration_revision(gateway_home.config) == revision_before


def test_stale_candidate_is_not_merged_after_discovery_window(gateway_home) -> None:
    service = ControlPlaneService(gateway_home)
    snapshot = service.snapshot()
    client = ClientConfig(
        kind="backend",
        secret_ref="CLIENT_STALE_CANDIDATE",
        allowed_routes=["memory.*"],
    )
    stale = service.upsert_client(
        snapshot,
        client_id="stale-candidate",
        client=client,
        secret_value="stale_candidate_token_0123456789_ABCDEFG",
    )

    payload = snapshot.config.model_dump(mode="python", exclude_none=False)
    payload["server"]["port"] += 1
    competing = service.prepare(
        snapshot,
        config=GatewayConfig.model_validate(payload),
    )
    service.commit(competing)

    with pytest.raises(ConfigConflict):
        service.commit(stale)

    assert "stale-candidate" not in load_config(gateway_home.config).clients
    assert "CLIENT_STALE_CANDIDATE" not in read_secrets(gateway_home.secrets)


def test_bundle_discovery_stays_outside_commit_and_stale_result_is_not_merged(
    gateway_home,
) -> None:
    competing_port = load_config(gateway_home.config).server.port + 1

    def handler(request: httpx.Request) -> httpx.Response:
        competing_service = ControlPlaneService(gateway_home)
        snapshot = competing_service.snapshot()
        payload = snapshot.config.model_dump(mode="python", exclude_none=False)
        payload["server"]["port"] = competing_port
        competing_service.apply(
            snapshot,
            config=GatewayConfig.model_validate(payload),
        )
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    app = create_app(paths=gateway_home, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        revision = client.get(
            "/admin/configuration",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
        ).json()["revision"]
        response = client.post(
            "/admin/channel-bundles/apply",
            headers={"authorization": f"Bearer {ADMIN_CLIENT_TOKEN}"},
            json={
                "revision": revision,
                "connection": {
                    "channel_operator": "new-vendor",
                    "base_url": "https://new-vendor.example/v1",
                    "secret": "candidate-secret",
                },
            },
        )

    assert response.status_code == 409
    config = load_config(gateway_home.config)
    assert config.server.port == competing_port
    assert "new-vendor-account" not in config.connections
    assert "CONNECTION_NEW_VENDOR_ACCOUNT_API_KEY" not in read_secrets(
        gateway_home.secrets
    )


def test_control_plane_module_has_no_adapter_dependency() -> None:
    source = (
        Path(__file__).parents[1] / "model_gateway" / "control_plane.py"
    ).read_text(encoding="utf-8")
    assert "model_gateway.admin" not in source
    assert "fastapi" not in source.lower()
    assert "argparse" not in source
