from __future__ import annotations

import json
import os
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
from model_gateway.auth import (
    AuthenticationError,
    authenticate_client,
    client_token_bytes,
    validate_secret_domains,
)
from model_gateway.http_safety import require_safe_destination_sync
from model_gateway.models import (
    ConnectionConfig,
    DeploymentConfig,
    GatewayConfig,
    PricingConfig,
    RequestTransform,
    derive_embedding_space,
)

from conftest import BACKEND_CLIENT_TOKEN, config_payload


@pytest.mark.parametrize("plan_type", ["token_plan", "coding_plan", "direct_tool_only"])
def test_plan_types_may_use_backend_allowed(plan_type: str) -> None:
    """Plan labels are informational; operators own provider ToS risk."""
    payload = config_payload()
    payload["connections"]["official"]["billing_plan"] = {
        "type": plan_type,
        "name": "operator-managed",
    }
    payload["connections"]["official"]["usage_scope"] = "backend_allowed"
    config = GatewayConfig.model_validate(payload)
    assert config.connections["official"].billing_plan.type == plan_type
    assert config.connections["official"].usage_scope == "backend_allowed"


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


def test_dashscope_deepseek_v4_profile_is_explicit_and_model_scoped() -> None:
    deployment = DeploymentConfig.model_validate(
        {
            "connection": "dashscope",
            "upstream_model": "deepseek-v4-pro",
            "model_author": "deepseek",
            "adapter_profile": "dashscope_deepseek_v4",
        }
    )
    assert deployment.adapter_profile == "dashscope_deepseek_v4"

    with pytest.raises(ValidationError, match="只允许显式绑定"):
        DeploymentConfig.model_validate(
            {
                "connection": "dashscope",
                "upstream_model": "qwen-plus",
                "model_author": "qwen",
                "adapter_profile": "dashscope_deepseek_v4",
            }
        )


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


@pytest.mark.parametrize("group", ["remove", "set_if_missing", "force"])
@pytest.mark.parametrize(
    "field",
    [
        "enable_thinking",
        "function_call",
        "functions",
        "parallel_tool_calls",
        "reasoning",
        "reasoning_effort",
        "response_format",
        "thinking",
        "tool_choice",
        "tools",
    ],
)
def test_legacy_protected_transforms_load_and_report_only_field_names(
    group: str,
    field: str,
) -> None:
    marker = "legacy-sensitive-transform-value"
    value = [field] if group == "remove" else {field: marker}

    transform = RequestTransform.model_validate({group: value})

    assert transform.protected_fields() == (field,)
    assert marker not in ", ".join(transform.protected_fields())


def test_provider_specific_transform_parameters_remain_available() -> None:
    transform = RequestTransform(
        remove=["unsupported_vendor_option"],
        set_if_missing={"temperature": 0.25},
        force={"vendor_extension": {"mode": "account-specific"}},
    )

    assert transform.protected_fields() == ()


def test_per_token_pricing_requires_official_source() -> None:
    payload = config_payload()
    payload["pricing"]["official-chat-2026-08"]["source_url"] = ""
    with pytest.raises(ValidationError, match="官方 source_url"):
        GatewayConfig.model_validate(payload)


@pytest.mark.parametrize(
    "source_url",
    [
        "http://official.example/pricing",
        "https://user:SECRET@official.example/pricing",
        "https://official.example/pricing?token=SECRET",
        "https://official.example/pricing#SECRET",
        "https://official.example:99999/pricing",
        "https://official.example/pricing\n",
    ],
)
def test_pricing_source_url_rejects_credential_bearing_or_ambiguous_urls(
    source_url: str,
) -> None:
    with pytest.raises(ValidationError):
        PricingConfig(mode="unknown", source_url=source_url)


def test_v2_connection_defaults_are_bounded_but_v1_migration_is_compatible() -> None:
    connection = ConnectionConfig.model_validate(
        {
            "channel_operator": "vendor",
            "base_url": "https://vendor.example/v1",
            "auth": {"secret_ref": "UPSTREAM"},
        }
    )
    assert (
        connection.connect_timeout_seconds,
        connection.read_timeout_seconds,
        connection.write_timeout_seconds,
        connection.pool_timeout_seconds,
    ) == (10.0, 120.0, 60.0, 10.0)
    assert connection.response_limit_bytes == 16 * 1024 * 1024

    legacy = config_payload()
    legacy["schema_version"] = 1
    migrated = GatewayConfig.model_validate(legacy)
    assert migrated.connections["official"].read_timeout_seconds == 300.0
    assert migrated.connections["official"].response_limit_bytes == 64 * 1024 * 1024
    assert migrated.clients["memory-gateway"].allow_legacy_weak_secret is True
    assert migrated.deployments["chat-official"].tool_choice_with_reasoning == (
        "auto_only"
    )


def test_derived_embedding_space_is_stable_and_never_crosses_identity() -> None:
    first = ConnectionConfig.model_validate(
        {
            "channel_operator": "vendor-a",
            "base_url": "https://API.Example:443/v1/",
            "auth": {"secret_ref": "UPSTREAM_A"},
        }
    )
    same_origin = ConnectionConfig.model_validate(
        {
            "channel_operator": "vendor-a",
            "base_url": "https://api.example/another-path",
            "auth": {"secret_ref": "UPSTREAM_B"},
        }
    )
    other_channel = ConnectionConfig.model_validate(
        {
            "channel_operator": "vendor-b",
            "base_url": "https://api.example/v1",
            "auth": {"secret_ref": "UPSTREAM_C"},
        }
    )
    baseline = derive_embedding_space(first, "embed-v4", 1024)
    assert baseline == derive_embedding_space(same_origin, "embed-v4", 1024)
    assert baseline != derive_embedding_space(other_channel, "embed-v4", 1024)
    assert baseline != derive_embedding_space(first, "embed-v4.1", 1024)
    assert baseline != derive_embedding_space(first, "embed-v4", 1536)
    assert baseline.isascii() and " " not in baseline


def test_config_is_atomic_private_and_backed_up(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    config = GatewayConfig.model_validate(config_payload())
    write_config(paths.config, config)
    write_config(paths.config, config)

    assert load_config(paths.config) == config
    assert json.loads(paths.config.read_text(encoding="utf-8"))["schema_version"] == 2
    if os.name == "posix":
        assert stat.S_IMODE(paths.config.stat().st_mode) == 0o600
    assert paths.config.with_suffix(".json.bak").exists()


def test_hot_reload_keeps_last_known_good_config(tmp_path: Path) -> None:
    paths = gateway_paths(tmp_path / "home")
    initialize(paths)
    write_config(paths.config, GatewayConfig.model_validate(config_payload()))
    manager = ConfigManager(paths)
    first, _ = manager.snapshot()

    paths.config.write_text("{broken", encoding="utf-8")
    # Bump mtime explicitly: coarse filesystem mtime granularity can stamp the
    # broken rewrite identically to the original write, skipping hot reload.
    stat_result = paths.config.stat()
    os.utime(
        paths.config,
        ns=(stat_result.st_atime_ns + 1_000_000, stat_result.st_mtime_ns + 1_000_000),
    )
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
    "header",
    [
        "Authorization",
        "Cookie",
        "Proxy-Authorization",
        "X-Api-Key",
        "Api-Key",
        "Transfer-Encoding",
        "X-Model-Gateway-Require-Deployment",
    ],
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


@pytest.mark.parametrize(
    "base_url",
    [
        "https://vendor.example/v1?token=x",
        "https://vendor.example/v1#fragment",
        "https://vendor.example:99999/v1",
        "https://vendor.example/v1\n",
        "https:\\vendor.example\\v1",
        "https://vendor.example/a/../b",
        "https://vendor.example/%2e%2e/private",
        "https://vendor.example/a%2fb",
        "https://vendor.example/a%5Cb",
        "https://vendor.example/%00private",
        "https://vendor.example/%252e%252e/private",
        "https://vendor.example/%zz/private",
    ],
)
def test_connection_url_rejects_ambiguous_or_unsafe_components(base_url: str) -> None:
    with pytest.raises(ValidationError):
        ConnectionConfig.model_validate(
            {
                "channel_operator": "vendor",
                "base_url": base_url,
                "auth": {"secret_ref": "UPSTREAM"},
            }
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "/a/../models",
        "/%2e%2e/models",
        "/a%2fmodels",
        "/a%5cmodels",
        "/%0d%0aheader",
        "/%252fmodels",
    ],
)
def test_connection_endpoints_reject_normalization_ambiguity(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        ConnectionConfig.model_validate(
            {
                "channel_operator": "vendor",
                "base_url": "https://vendor.example/compatible-mode/v1",
                "models_endpoint": endpoint,
                "auth": {"secret_ref": "UPSTREAM"},
            }
        )


def test_legitimate_nested_compatible_mode_path_remains_valid() -> None:
    connection = ConnectionConfig.model_validate(
        {
            "channel_operator": "vendor",
            "base_url": "https://vendor.example/compatible-mode/v1",
            "models_endpoint": "/models",
            "auth": {"secret_ref": "UPSTREAM"},
        }
    )
    assert connection.base_url.endswith("/compatible-mode/v1")


def test_private_upstream_requires_explicit_cidr() -> None:
    payload = {
        "channel_operator": "lan-provider",
        "base_url": "http://192.168.50.20:8000/v1",
        "auth": {"secret_ref": "UPSTREAM"},
    }
    with pytest.raises(ValidationError, match="allowed_private_networks"):
        ConnectionConfig.model_validate(payload)

    payload["allowed_private_networks"] = ["192.168.50.0/24"]
    connection = ConnectionConfig.model_validate(payload)
    assert connection.base_url == "http://192.168.50.20:8000/v1"

    ipv6 = ConnectionConfig.model_validate(
        {
            "channel_operator": "lan-v6",
            "allowed_private_networks": ["fd00:1234::/48"],
            "base_url": "http://[fd00:1234::20]:8000/v1",
            "auth": {"secret_ref": "UPSTREAM_V6"},
        }
    )
    assert ipv6.base_url.startswith("http://[fd00:1234::20]")


def test_https_hostname_cannot_resolve_to_implicit_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def private_dns(host: str, port: int, **kwargs):
        return [(2, 1, 6, "", ("192.168.50.20", port))]

    monkeypatch.setattr("socket.getaddrinfo", private_dns)
    with pytest.raises(ValueError, match="未显式允许"):
        require_safe_destination_sync("https://provider.example/v1/models")

    require_safe_destination_sync(
        "https://provider.example/v1/models",
        allowed_private_networks=("192.168.50.0/24",),
    )


def test_rfc2544_mapping_requires_explicit_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def benchmark_dns(host: str, port: int, **kwargs):
        return [(2, 1, 6, "", ("198.18.0.77", port))]

    monkeypatch.setattr("socket.getaddrinfo", benchmark_dns)
    with pytest.raises(ValueError, match="未显式允许"):
        require_safe_destination_sync("https://provider.example/v1/models")

    # Exact /32 still works for fixed sandbox mappings.
    require_safe_destination_sync(
        "https://provider.example/v1/models",
        allowed_private_networks=("198.18.0.77/32",),
    )

    # Clash/Surge TUN fake-ip ranges may change; users may opt into the whole
    # RFC 2544 supernet or a broader subnet explicitly.
    require_safe_destination_sync(
        "https://provider.example/v1/models",
        allowed_private_networks=("198.18.0.0/15",),
    )
    require_safe_destination_sync(
        "https://provider.example/v1/models",
        allowed_private_networks=("198.18.0.0/24",),
    )

    connection = ConnectionConfig.model_validate(
        {
            "channel_operator": "sandbox-provider",
            "base_url": "https://provider.example/v1",
            "allowed_private_networks": ["198.18.0.0/15"],
            "auth": {"secret_ref": "UPSTREAM_SANDBOX"},
        }
    )
    assert list(connection.allowed_private_networks) == ["198.18.0.0/15"]

    # Rejection message should include resolved address and fake-ip guidance.
    with pytest.raises(ValueError, match="198\\.18\\.0\\.77"):
        require_safe_destination_sync("https://provider.example/v1/models")


def test_client_and_connection_secret_refs_are_disjoint() -> None:
    payload = config_payload()
    payload["connections"]["official"]["auth"]["secret_ref"] = (
        "CLIENT_MEMORY_GATEWAY"
    )
    with pytest.raises(ValidationError, match="权限域混淆"):
        GatewayConfig.model_validate(payload)


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
            f"Bearer {BACKEND_CLIENT_TOKEN}",
            config=config,
            secrets={
                "CLIENT_MEMORY_GATEWAY": BACKEND_CLIENT_TOKEN,
                "CLIENT_DESKTOP": BACKEND_CLIENT_TOKEN,
            },
        )


def test_provider_and_client_secret_values_fail_closed() -> None:
    config = GatewayConfig.model_validate(config_payload())
    with pytest.raises(AuthenticationError, match="上游连接密钥配置冲突"):
        authenticate_client(
            f"Bearer {BACKEND_CLIENT_TOKEN}",
            config=config,
            secrets={
                "CLIENT_MEMORY_GATEWAY": BACKEND_CLIENT_TOKEN,
                "UPSTREAM_OFFICIAL": BACKEND_CLIENT_TOKEN,
            },
        )


@pytest.mark.parametrize(
    "token",
    [
        "short-token",
        "a" * 32 + "!",
        "a" * 64,
        "contains space but is definitely long enough",
    ],
)
def test_schema_v2_client_tokens_require_strong_url_safe_format(token: str) -> None:
    with pytest.raises(ValueError, match="客户端密钥"):
        client_token_bytes(token)


def test_schema_v1_weak_client_token_is_an_explicit_migration_only() -> None:
    payload = config_payload()
    payload["schema_version"] = 1
    migrated = GatewayConfig.model_validate(payload)
    weak = "old-local-key"

    validate_secret_domains(
        config=migrated,
        secrets={"CLIENT_MEMORY_GATEWAY": weak},
    )
    authenticated = authenticate_client(
        f"Bearer {weak}",
        config=migrated,
        secrets={"CLIENT_MEMORY_GATEWAY": weak},
    )
    assert authenticated.id == "memory-gateway"

    v2_payload = migrated.model_dump(mode="python")
    v2_payload["clients"]["memory-gateway"]["allow_legacy_weak_secret"] = False
    strict = GatewayConfig.model_validate(v2_payload)
    with pytest.raises(ValueError, match="至少 32 字节"):
        validate_secret_domains(
            config=strict,
            secrets={"CLIENT_MEMORY_GATEWAY": weak},
        )


@pytest.mark.parametrize("token", ["contains space", "非ASCII", "tab\ttoken"])
def test_client_token_must_be_printable_ascii(token: str) -> None:
    config = GatewayConfig.model_validate(config_payload())
    with pytest.raises(AuthenticationError, match="可打印 ASCII"):
        authenticate_client(
            f"Bearer {token}",
            config=config,
            secrets={"CLIENT_MEMORY_GATEWAY": token},
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


def test_fake_ip_env_accepts_rfc2544_range_without_per_connection_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def benchmark_dns(host: str, port: int, **kwargs):
        return [(2, 1, 6, "", ("198.19.3.4", port))]

    monkeypatch.setattr("socket.getaddrinfo", benchmark_dns)
    monkeypatch.delenv("MODEL_GATEWAY_ALLOW_FAKE_IP", raising=False)
    with pytest.raises(ValueError, match="未显式允许"):
        require_safe_destination_sync("https://provider.example/v1/models")

    monkeypatch.setenv("MODEL_GATEWAY_ALLOW_FAKE_IP", "1")
    require_safe_destination_sync("https://provider.example/v1/models")

    # IPv6 fake-ip (Clash Meta / mihomo fake-ip-range6 fc00::/18) is relaxed too.
    def benchmark_dns6(host: str, port: int, **kwargs):
        return [(10, 1, 6, "", ("fc00::8", port, 0, 0))]

    monkeypatch.setattr("socket.getaddrinfo", benchmark_dns6)
    require_safe_destination_sync("https://provider.example/v1/models")
    monkeypatch.delenv("MODEL_GATEWAY_ALLOW_FAKE_IP", raising=False)
    with pytest.raises(ValueError, match="fc00::/18"):
        require_safe_destination_sync("https://provider.example/v1/models")
    monkeypatch.setenv("MODEL_GATEWAY_ALLOW_FAKE_IP", "1")

    # Only the fake-ip ranges are relaxed; real LAN / ULA addresses stay blocked.
    def ula_dns(host: str, port: int, **kwargs):
        return [(10, 1, 6, "", ("fd12::1", port, 0, 0))]

    monkeypatch.setattr("socket.getaddrinfo", ula_dns)
    with pytest.raises(ValueError, match="未显式允许"):
        require_safe_destination_sync("https://provider.example/v1/models")

    def lan_dns(host: str, port: int, **kwargs):
        return [(2, 1, 6, "", ("192.168.1.9", port))]

    monkeypatch.setattr("socket.getaddrinfo", lan_dns)
    with pytest.raises(ValueError, match="未显式允许"):
        require_safe_destination_sync("https://provider.example/v1/models")
