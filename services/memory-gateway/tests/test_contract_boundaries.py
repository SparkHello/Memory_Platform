from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from app.api.providers import REQUIRED_CHAT_ROUTES
from app.config import Settings
from app.stack_backup import _validate_model_gateway_config
from model_gateway_contracts import (
    DEFAULT_MEMORY_CHAT_ROUTES,
    DEFAULT_MEMORY_GATEWAY_ROUTES,
    GatewayConfig,
)


SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]
CONTRACTS_ROOT = REPOSITORY_ROOT / "packages" / "model-gateway-contracts"


def test_memory_runtime_has_no_model_gateway_service_imports() -> None:
    offenders: list[str] = []
    for path in (SERVICE_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                continue
            if "model_gateway" in roots:
                offenders.append(str(path.relative_to(SERVICE_ROOT)))
                break
    assert offenders == []


def test_memory_contract_modules_import_with_model_service_blocked() -> None:
    code = """
import importlib
import sys

class BlockModelGateway:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "model_gateway" or fullname.startswith("model_gateway."):
            raise ImportError("Model service package is unavailable in Memory runtime")
        return None

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
sys.meta_path.insert(0, BlockModelGateway())
for module in (
    "app.config",
    "app.llm.model_gateway",
    "app.usage.attribution",
    "app.stack_backup",
    "app.api.memories.export",
):
    importlib.import_module(module)
assert "model_gateway" not in sys.modules
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(SERVICE_ROOT),
            str(CONTRACTS_ROOT),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_memory_declares_the_exact_contract_dependency() -> None:
    project = tomllib.loads(
        (SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "model-gateway-contracts==0.5.1" in project["project"]["dependencies"]


def test_memory_defaults_use_the_eight_contract_routes() -> None:
    settings = Settings(_env_file=None)
    configured = (
        settings.model_gateway_chat_model,
        settings.model_gateway_memory_extract_model,
        settings.model_gateway_memory_compact_model,
        settings.model_gateway_memory_core_model,
        settings.model_gateway_memory_review_model,
        settings.model_gateway_knowledge_fast_model,
        settings.model_gateway_knowledge_pro_model,
        settings.model_gateway_embedding_model,
    )
    assert configured == DEFAULT_MEMORY_GATEWAY_ROUTES
    assert REQUIRED_CHAT_ROUTES == DEFAULT_MEMORY_CHAT_ROUTES


def test_portable_model_config_keeps_v1_compatibility_without_rewriting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    payload = {"schema_version": 1, "server": {"port": 2030}}
    original = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path.write_bytes(original)

    _validate_model_gateway_config(path)

    assert path.read_bytes() == original
    assert GatewayConfig.model_validate_json(original).schema_version == 2


def test_portable_model_config_rejects_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"schema_version":3}', encoding="utf-8")

    with pytest.raises(ValueError, match="schema"):
        _validate_model_gateway_config(path)
