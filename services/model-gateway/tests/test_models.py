from __future__ import annotations

import json
from pathlib import Path
import stat

import pytest
from pydantic import ValidationError

from model_gateway.config_store import (
    ConfigManager,
    gateway_paths,
    initialize,
    load_config,
    read_secrets,
    set_secret,
    write_config,
    write_secrets,
)
from model_gateway.auth import AuthenticationError, authenticate_client
from model_gateway.models import ConnectionConfig, GatewayConfig, RequestTransform

from conftest import config_payload


@pytest.mark.parametrize("plan_type", ["token_plan", "coding_plan", "direct_tool_only"])
def test_restricted_plan_cannot_be_used_by_backend(plan_type: str) -> None:
    payload = config_payload()
    payload["connections"]["official"]["billing_plan"] = {
        "type": plan_type,
        "name": "coding-only",
    }
    with pytest.raises(ValidationError, match="不能配置为 backend_allowed"):
        GatewayConfig.model_validate(payload)


def test_embedding_route_cannot_mix_vector_spaces() -> None:
    payload = config_payload()
    payload["deployments"]["embed-reseller"] = {
        "connection": "reseller",
        "upstream_model": "author/embed-v2",
        "model_author": "author",
        "kind": "embedding",
        "dimensions": 4,
        "embedding_space": "author.embed-v2:4",
        "capabilities": {"streaming": False},
    }
    payload["routes"]["memory.embedding"]["targets"].append("embed-reseller")
    with pytest.raises(ValidationError, match="不能混用不同向量空间"):
        GatewayConfig.model_validate(payload)


@pytest.mark.parametrize("group", ["set_if_missing", "force"])
def test_embedding_transform_cannot_contradict_declared_dimensions(group: str) -> None:
    payload = config_payload()
    payload["deployments"]["embed-official"]["request_transform"] = {
        group: {"dimensions": 3}
    }

    with pytest.raises(ValidationError, match="必须等于 deployment 声明维度"):
        GatewayConfig.model_validate(payload)


@pytest.mark.parametrize("field", ["model", "messages", "input", "stream"])
def test_transforms_cannot_touch_semantic_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="不能修改核心字段"):
        RequestTransform(force={field: "changed"})


def test_per_token_pricing_requires_official_source() -> None:
    payload = config_payload()
    payload["pricing"]["official-chat-2026-08"]["source_url"] = ""
    with pytest.raises(ValidationError, match="官方 source_url"):
        GatewayConfig.model_validate(payload)


def test_config_is_atomic_private_and_backed_up(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    config = GatewayConfig.model_validate(config_payload())
    write_config(paths.config, config)
    write_config(paths.config, config)

    assert load_config(paths.config) == config
    assert json.loads(paths.config.read_text(encoding="utf-8"))["schema_version"] == 1
    assert stat.S_IMODE(paths.config.stat().st_mode) == 0o600
    assert paths.config.with_suffix(".json.bak").exists()


def test_hot_reload_keeps_last_known_good_config(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    write_config(paths.config, GatewayConfig.model_validate(config_payload()))
    manager = ConfigManager(paths)
    first, _ = manager.snapshot()

    paths.config.write_text("{broken", encoding="utf-8")
    second, _ = manager.snapshot()

    assert second == first
    assert "配置文件不是合法 JSON" in manager.last_reload_error


def test_deleted_secret_does_not_survive_in_backup(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    set_secret(paths.secrets, "UPSTREAM_ONE", "sensitive-marker")
    set_secret(paths.secrets, "UPSTREAM_ONE", None)

    assert "sensitive-marker" not in paths.secrets.read_text(encoding="utf-8")
    assert not paths.secrets.with_suffix(".env.bak").exists()


def test_disabled_models_endpoint_survives_round_trip(tmp_path: Path) -> None:
    payload = config_payload()
    payload["connections"]["official"]["models_endpoint"] = None
    config = GatewayConfig.model_validate(payload)
    path = tmp_path / "config.json"
    write_config(path, config)

    assert load_config(path).connections["official"].models_endpoint is None


@pytest.mark.parametrize(
    "header", ["Authorization", "Cookie", "X-Model-Gateway-Require-Deployment"]
)
def test_connection_cannot_forward_sensitive_local_headers(header: str) -> None:
    with pytest.raises(ValidationError, match="禁止转发"):
        ConnectionConfig.model_validate(
            {
                "channel_operator": "vendor",
                "base_url": "https://vendor.example/v1",
                "auth": {"secret_ref": "UPSTREAM"},
                "forward_headers": [header],
            }
        )


def test_connection_url_cannot_embed_credentials() -> None:
    with pytest.raises(ValidationError, match="不能内嵌账号或密钥"):
        ConnectionConfig.model_validate(
            {
                "channel_operator": "vendor",
                "base_url": "https://user:secret@vendor.example/v1",
                "auth": {"secret_ref": "UPSTREAM"},
            }
        )


def test_secret_round_trip_never_interpolates_environment_syntax(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    opaque = r'prefix-${HOME}-${ANOTHER}-backslash\-quote"'
    write_secrets(path, {"OPAQUE_SECRET": opaque})

    assert read_secrets(path) == {"OPAQUE_SECRET": opaque}


def test_clients_cannot_share_a_secret_reference() -> None:
    payload = config_payload()
    payload["clients"]["desktop"]["secret_ref"] = "CLIENT_MEMORY_GATEWAY"

    with pytest.raises(ValidationError, match="独立的 secret_ref"):
        GatewayConfig.model_validate(payload)


def test_duplicate_client_secret_values_fail_closed() -> None:
    config = GatewayConfig.model_validate(config_payload())

    with pytest.raises(AuthenticationError, match="密钥配置冲突"):
        authenticate_client(
            "Bearer duplicate-value",
            config=config,
            secrets={
                "CLIENT_MEMORY_GATEWAY": "duplicate-value",
                "CLIENT_DESKTOP": "duplicate-value",
            },
        )


@pytest.mark.parametrize("upstream_model", ["模型-v1", "model\x00v1", "model\x7fv1"])
def test_model_identifier_must_be_safe_for_response_headers(upstream_model: str) -> None:
    with pytest.raises(ValidationError, match="可打印 ASCII"):
        GatewayConfig.model_validate(
            {
                "connections": {
                    "vendor": {
                        "channel_operator": "vendor",
                        "base_url": "https://vendor.example/v1",
                        "auth": {"secret_ref": "UPSTREAM"},
                    }
                },
                "deployments": {
                    "chat": {
                        "connection": "vendor",
                        "upstream_model": upstream_model,
                        "model_author": "author",
                    }
                },
            }
        )
