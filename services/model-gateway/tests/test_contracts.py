from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

import pytest
from pydantic import ValidationError

import model_gateway.models as legacy_models
import model_gateway_contracts as contracts
from model_gateway.http_safety import normalize_base_url as legacy_normalize_base_url
from model_gateway_contracts.urls import (
    normalize_base_url,
    normalize_endpoint,
    normalize_private_networks,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_ROOT = ROOT / "packages" / "model-gateway-contracts"
SERVICE_ROOTS = (
    ROOT / "services" / "memory-gateway",
    ROOT / "services" / "model-gateway",
)


def test_legacy_model_path_reexports_the_exact_contract_types() -> None:
    model_names = {
        name
        for name in contracts.__all__
        if name.endswith("Config") or name in {"BillingPlan", "PricingTier"}
    }
    assert model_names
    for name in model_names:
        assert getattr(legacy_models, name) is getattr(contracts, name)


def test_legacy_config_fixture_still_migrates_to_schema_v2() -> None:
    payload = json.loads(
        (ROOT / "services" / "model-gateway" / "examples" / "config.example.json")
        .read_text(encoding="utf-8")
    )

    config = contracts.GatewayConfig.model_validate(payload)
    dumped = config.model_dump(mode="json")

    assert payload["schema_version"] == 1
    assert dumped["schema_version"] == contracts.GATEWAY_CONFIG_SCHEMA_VERSION == 2
    assert dumped["clients"]["memory-gateway"]["allow_legacy_weak_secret"] is True
    assert dumped["routes"]["memory.chat"]["fallback_scope"] == "any_channel"


def test_future_config_schema_is_rejected() -> None:
    with pytest.raises(ValidationError):
        contracts.GatewayConfig.model_validate({"schema_version": 3})


def test_memory_gateway_defaults_are_eight_exact_routes() -> None:
    assert contracts.DEFAULT_MEMORY_GATEWAY_ROUTES == (
        "memory.chat",
        "memory.extract",
        "memory.compact",
        "memory.core",
        "memory.review",
        "knowledge.fast",
        "knowledge.pro",
        "memory.embedding",
    )
    assert all("*" not in route for route in contracts.DEFAULT_MEMORY_GATEWAY_ROUTES)


def test_user_console_uses_the_shared_route_order() -> None:
    from model_gateway.user_console import PURPOSES

    assert tuple(route_id for route_id, _, _ in PURPOSES) == (
        contracts.DEFAULT_MEMORY_GATEWAY_ROUTES
    )


def test_wire_constants_are_stable_strings() -> None:
    assert contracts.MODEL_GATEWAY_ROUTE_HEADER == "X-Model-Gateway-Route"
    assert (
        contracts.MODEL_GATEWAY_REASONING_ORIGIN_DEPLOYMENT_HEADER
        == "X-Model-Gateway-Reasoning-Origin-Deployment"
    )
    assert (
        contracts.GatewayErrorCode.CONFIGURATION_INVALID
        == "model_gateway_configuration_invalid"
    )
    assert contracts.GatewayErrorCode.AFFINITY_UNAVAILABLE == (
        "model_gateway_affinity_unavailable"
    )


def test_old_http_safety_path_reexports_contract_normalizer() -> None:
    assert legacy_normalize_base_url is normalize_base_url
    assert normalize_base_url("https://api.example/v1/") == "https://api.example/v1"


@pytest.mark.parametrize(
    "value",
    (
        " 10.0.0.0/8",
        "10.0.0.0/8\n",
    ),
)
def test_private_network_contract_rejects_each_whitespace_form(value: str) -> None:
    with pytest.raises(ValueError, match="空白或控制字符"):
        normalize_private_networks([value])


@pytest.mark.parametrize(
    "value",
    (
        "ftp://api.example/v1",
        "http:///v1",
        "https://user@api.example/v1",
        "https://:password@api.example/v1",
        "https://api.example:0/v1",
        "https://valid.-invalid.example/v1",
    ),
)
def test_base_url_contract_rejects_independent_invalid_components(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(value)


@pytest.mark.parametrize(
    "value",
    (
        "http://localhost:2030/v1",
        "http://127.0.0.1:2030/v1",
    ),
)
def test_base_url_contract_accepts_each_loopback_form(value: str) -> None:
    assert normalize_base_url(value) == value


def test_private_literal_requires_the_matching_allowlisted_network() -> None:
    with pytest.raises(ValueError, match="必须显式列入"):
        normalize_base_url(
            "http://10.0.0.1/v1",
            allowed_private_networks=["192.168.0.0/16"],
        )


def test_endpoint_contract_rejects_non_string_without_leaking_type_errors() -> None:
    with pytest.raises(ValueError, match="外围空白"):
        normalize_endpoint(42)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    (
        "/models?limit=1",
        "/models#fragment",
        "/./models",
    ),
)
def test_endpoint_contract_rejects_each_structural_suffix(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_endpoint(value)


def test_contract_package_has_no_service_or_http_runtime_imports() -> None:
    forbidden_roots = {
        "app",
        "argparse",
        "fastapi",
        "httpx",
        "model_gateway",
        "pydantic_settings",
        "uvicorn",
    }
    for path in (CONTRACTS_ROOT / "model_gateway_contracts").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".", 1)[0])
        assert imported.isdisjoint(forbidden_roots), (path.name, imported)


def test_model_declares_the_exact_contract_dependency() -> None:
    project = tomllib.loads(
        (ROOT / "services" / "model-gateway" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert "model-gateway-contracts==0.5.1" in project["project"]["dependencies"]


def test_contract_package_imports_without_either_service_on_path() -> None:
    code = """
import sys
sys.path.insert(0, sys.argv[1])
import model_gateway_contracts
assert model_gateway_contracts.GatewayConfig().schema_version == 2
assert not any(name == 'model_gateway' or name.startswith('model_gateway.') for name in sys.modules)
assert not any(name == 'app' or name.startswith('app.') for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code, str(CONTRACTS_ROOT)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_documented_editable_installs_resolve_local_packages_offline() -> None:
    expected_command = '-e ../../packages/model-gateway-contracts -e ".[dev]"'
    documentation = (
        ROOT / "services" / "memory-gateway" / "AGENTS.md",
        ROOT / "services" / "memory-gateway" / "README.md",
        ROOT / "services" / "memory-gateway" / "docs" / "usage_guide.md",
        ROOT / "services" / "model-gateway" / "AGENTS.md",
        ROOT / "services" / "model-gateway" / "README.md",
    )
    for path in documentation:
        assert expected_command in path.read_text(encoding="utf-8"), path

    # pip adds deep hash/cache/staging paths. Keep the disposable build tree at
    # the repository root so this also works on Windows without long-path
    # support; TemporaryDirectory removes it even when an assertion fails.
    with tempfile.TemporaryDirectory(prefix=".pytest-b6-pkg-", dir=ROOT) as value:
        _exercise_documented_editable_installs(Path(value))


def _exercise_documented_editable_installs(isolated_root: Path) -> None:
    source_root = isolated_root / "s"
    local_contracts_root = source_root / "packages" / "model-gateway-contracts"
    shutil.copytree(
        CONTRACTS_ROOT,
        local_contracts_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"),
    )
    local_service_roots: list[Path] = []
    for service_root in SERVICE_ROOTS:
        local_service_root = source_root / "services" / service_root.name
        local_service_root.mkdir(parents=True)
        shutil.copy2(service_root / "pyproject.toml", local_service_root)
        shutil.copy2(service_root / "README.md", local_service_root)
        source_package = (
            "app" if service_root.name == "memory-gateway" else "model_gateway"
        )
        shutil.copytree(
            service_root / source_package,
            local_service_root / source_package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        local_service_roots.append(local_service_root)

    process_temp = isolated_root / "t"
    process_temp.mkdir()
    environment = {
        **os.environ,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_NO_INDEX": "1",
        "TEMP": str(process_temp),
        "TMP": str(process_temp),
    }
    wheelhouse = isolated_root / "w"
    wheelhouse.mkdir()
    wheel = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            str(local_contracts_root),
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert wheel.returncode == 0, wheel.stderr
    assert len(tuple(wheelhouse.glob("model_gateway_contracts-*.whl"))) == 1

    for service_root in local_service_roots:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--ignore-installed",
                "--no-deps",
                "--no-build-isolation",
                "-e",
                "../../packages/model-gateway-contracts",
                "-e",
                ".[dev]",
            ],
            capture_output=True,
            check=False,
            cwd=service_root,
            env=environment,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        dry_run = completed.stdout.lower()
        assert "model-gateway-contracts-0.5.1" in dry_run
        distribution_name = (
            "memory-gateway"
            if service_root.name == "memory-gateway"
            else "local-model-gateway"
        )
        assert f"{distribution_name}-0.5.1" in dry_run
