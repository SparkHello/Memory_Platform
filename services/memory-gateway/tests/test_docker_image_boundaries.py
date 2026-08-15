from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _stage(name: str) -> str:
    match = re.search(
        rf"^FROM [^\n]+ AS {re.escape(name)}\n(?P<body>.*?)(?=^FROM |\Z)",
        DOCKERFILE,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing Docker stage: {name}"
    return match.group("body")


def test_runtime_dependencies_resolve_into_three_fresh_venvs() -> None:
    wheelhouse = _stage("python-wheelhouse")
    memory = _stage("memory-python-build")
    model = _stage("model-python-build")
    initializer = _stage("init-python-build")

    assert "pip download" in wheelhouse
    assert "--require-hashes --only-binary=:all:" in wheelhouse
    assert "./model-gateway-contracts ./memory-gateway ./model-gateway" in wheelhouse
    assert DOCKERFILE.count("--no-index --find-links=/wheelhouse") == 4

    assert "python -m venv /opt/venv" in memory
    assert "/wheelhouse/memory_gateway-*.whl" in memory
    assert "/wheelhouse/model_gateway_contracts-*.whl" in memory
    assert "/wheelhouse/local_model_gateway-*.whl" not in memory

    assert "python -m venv /opt/venv" in model
    assert "/wheelhouse/local_model_gateway-*.whl" in model
    assert "/wheelhouse/model_gateway_contracts-*.whl" in model
    assert "/wheelhouse/memory_gateway-*.whl" not in model

    assert "python -m venv /opt/venv" in initializer
    assert "/wheelhouse/memory_gateway-*.whl" in initializer
    assert "/wheelhouse/local_model_gateway-*.whl" in initializer
    assert "/wheelhouse/model_gateway_contracts-*.whl" in initializer
    assert DOCKERFILE.count("/opt/venv/bin/pip check") == 3
    assert DOCKERFILE.count("touch /opt/venv/.pip-check-ok") == 3


def test_long_lived_images_copy_only_their_service_environment() -> None:
    memory = _stage("memory-runtime")
    model = _stage("model-runtime")
    initializer = _stage("stack-init")

    assert "COPY --from=memory-python-build /opt/venv /opt/venv" in memory
    assert "/wheelhouse" not in memory
    assert "services/memory-gateway/app" in memory
    assert "model-python-build" not in memory
    assert "services/model-gateway" not in memory

    assert "COPY --from=model-python-build /opt/venv /opt/venv" in model
    assert "/wheelhouse" not in model
    assert "memory-python-build" not in model
    assert "services/memory-gateway" not in model

    assert "COPY --from=init-python-build /opt/venv /opt/venv" in initializer
    assert "/wheelhouse" not in initializer
    assert "services/memory-gateway/app" in initializer
    for maintenance_tool in (
        "migrate_legacy.py",
        "backup_legacy.py",
        "restore_split.py",
        "validate_compose.py",
        "plan_install.py",
        "verify_backup.py",
    ):
        assert maintenance_tool in initializer
    init_source = (ROOT / "deploy" / "init_stack.py").read_text(encoding="utf-8")
    assert 'MODELGW = Path("/opt/venv/bin/modelgw")' in init_source
