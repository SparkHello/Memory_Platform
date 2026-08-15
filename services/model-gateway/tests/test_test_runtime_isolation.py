from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from model_gateway.config_store import gateway_paths, initialize, write_secrets


def test_global_test_runtime_is_sandboxed(tmp_path: Path) -> None:
    assert Path(os.environ["MODEL_GATEWAY_HOME"]).resolve() == (
        tmp_path / "modelgw-home"
    ).resolve()
    assert os.environ["MODEL_GATEWAY_SECRETS_PATH"] == ""
    assert not (tmp_path / "modelgw-home").exists()

    paths = gateway_paths(tmp_path / "explicit-home")
    assert paths.secrets.resolve() == (
        tmp_path / "explicit-home" / "secrets.env"
    ).resolve()


def test_test_local_monkeypatch_undo_cannot_remove_runtime_sandbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MODEL_GATEWAY_HOME", str(tmp_path / "test-override"))
    monkeypatch.setenv(
        "MODEL_GATEWAY_SECRETS_PATH",
        str(tmp_path / "test-override-secrets.env"),
    )
    monkeypatch.undo()

    assert Path(os.environ["MODEL_GATEWAY_HOME"]).resolve() == (
        tmp_path / "modelgw-home"
    ).resolve()
    assert os.environ["MODEL_GATEWAY_SECRETS_PATH"] == ""


def test_subprocess_runtime_probe(tmp_path: Path) -> None:
    external = os.environ.get("PYTEST_EXTERNAL_MODEL_SECRETS_CANARY", "")
    paths = gateway_paths(tmp_path / "explicit-probe-home")
    initialize(paths)
    write_secrets(paths.secrets, {"UPSTREAM_PROBE": "synthetic-probe-secret"})

    assert paths.home.resolve() == (tmp_path / "explicit-probe-home").resolve()
    assert paths.secrets.resolve() == (
        tmp_path / "explicit-probe-home" / "secrets.env"
    ).resolve()
    if external:
        assert paths.secrets.resolve() != Path(external).resolve()


def test_subprocess_inherited_secret_canary_is_untouched(tmp_path: Path) -> None:
    service_root = Path(__file__).resolve().parents[1]
    external_root = tmp_path / "external-runtime"
    external_home = external_root / "modelgw-home"
    external_secrets = external_root / "secrets.env"
    external_home.mkdir(parents=True)
    canary_bytes = (
        b"CLIENT_EXTERNAL='synthetic-external-client-canary'\n"
        b"UPSTREAM_EXTERNAL='synthetic-external-provider-canary'\n"
    )
    external_secrets.write_bytes(canary_bytes)
    external_secrets.chmod(0o600)
    before = external_secrets.stat()
    environment = dict(os.environ)
    environment.update(
        {
            "MODEL_GATEWAY_HOME": str(external_home),
            "MODEL_GATEWAY_SECRETS_PATH": str(external_secrets),
            "PYTEST_EXTERNAL_MODEL_SECRETS_CANARY": str(external_secrets),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_test_runtime_isolation.py::test_subprocess_runtime_probe",
        ],
        cwd=service_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0
    after = external_secrets.stat()
    assert external_secrets.read_bytes() == canary_bytes
    assert after.st_mtime_ns == before.st_mtime_ns
    assert list(external_home.iterdir()) == []


def test_subprocess_missing_external_secret_override_is_neutralized(
    tmp_path: Path,
) -> None:
    service_root = Path(__file__).resolve().parents[1]
    missing = tmp_path / "external-missing-secrets.env"
    environment = dict(os.environ)
    environment.update(
        {
            "MODEL_GATEWAY_SECRETS_PATH": str(missing),
            "PYTEST_EXTERNAL_MODEL_SECRETS_CANARY": str(missing),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_test_runtime_isolation.py::test_subprocess_runtime_probe",
        ],
        cwd=service_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0
    assert not missing.exists()
