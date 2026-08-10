import os
from pathlib import Path
import subprocess
import sys

from app.cli_config import cli_paths
from app.config import Settings, get_settings


def test_global_test_runtime_is_sandboxed(tmp_path: Path) -> None:
    settings = get_settings()

    assert Path(settings.database_path).resolve() == (tmp_path / "runtime-memory.db").resolve()
    assert Path(settings.knowledge_database_path).resolve() == (
        tmp_path / "runtime-knowledge.db"
    ).resolve()
    assert Path(settings.eval_dir).resolve() == (tmp_path / "eval").resolve()
    assert Path(os.environ["MEMGW_HOME"]).resolve() == (tmp_path / "memgw-home").resolve()
    assert os.environ["MEMGW_SETTINGS_PATH"] == ""
    assert cli_paths().settings_env.resolve() == (
        tmp_path / "memgw-home" / "settings.env"
    ).resolve()
    assert Path(os.environ["MODEL_GATEWAY_HOME"]).resolve() == (
        tmp_path / "modelgw-home"
    ).resolve()
    assert Path(os.environ["MODEL_GATEWAY_SECRETS_PATH"]).resolve() == (
        tmp_path / "model-secrets" / "secrets.env"
    ).resolve()
    # Model Gateway backend key is part of the default dual-gateway test sandbox;
    # every other API key must stay empty so developer secrets cannot leak in.
    assert all(
        (name == "MODEL_GATEWAY_API_KEY" and value == "pytest-central-backend-key")
        or not value
        for name, value in os.environ.items()
        if name.endswith("_API_KEY")
    )


def test_test_local_monkeypatch_undo_cannot_remove_runtime_sandbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MEMGW_HOME", str(tmp_path / "test-override"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test-override.db"))
    monkeypatch.undo()

    assert Path(os.environ["MEMGW_HOME"]).resolve() == (tmp_path / "memgw-home").resolve()
    assert Path(os.environ["DATABASE_PATH"]).resolve() == (
        tmp_path / "runtime-memory.db"
    ).resolve()


def test_dotenv_is_disabled_without_changing_environment_precedence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("GATEWAY_USER_ID=must-not-be-read\n", encoding="utf-8")
    with monkeypatch.context() as local:
        local.chdir(tmp_path)
        local.setenv("GATEWAY_USER_ID", "explicit-test-environment")
        settings = Settings()

    assert settings.gateway_user_id == "explicit-test-environment"


def test_subprocess_runtime_probe(tmp_path: Path) -> None:
    external = os.environ.get("PYTEST_EXTERNAL_MEMORY_SETTINGS_CANARY", "")
    assert Path(os.environ["MEMGW_HOME"]).resolve() == (tmp_path / "memgw-home").resolve()
    assert os.environ["MEMGW_SETTINGS_PATH"] == ""
    assert Path(os.environ["DATABASE_PATH"]).resolve() == (
        tmp_path / "runtime-memory.db"
    ).resolve()
    if external:
        assert cli_paths().settings_env.resolve() != Path(external).resolve()


def test_subprocess_inherited_settings_canary_is_untouched(tmp_path: Path) -> None:
    service_root = Path(__file__).resolve().parents[1]
    external_root = tmp_path / "external-runtime"
    external_home = external_root / "memgw-home"
    external_model = external_root / "modelgw-home"
    external_settings = external_root / "settings.env"
    external_home.mkdir(parents=True)
    external_model.mkdir()
    canary_bytes = (
        b"LEGACY_PROVIDER_URL='https://external-canary.invalid/v1'\n"
        b"GATEWAY_SIGNING_SECRET='external-canary-signing-secret-32-bytes'\n"
    )
    external_settings.write_bytes(canary_bytes)
    external_settings.chmod(0o600)
    before = external_settings.stat()
    environment = dict(os.environ)
    environment.update(
        {
            "MEMGW_HOME": str(external_home),
            "MEMGW_SETTINGS_PATH": str(external_settings),
            "MODEL_GATEWAY_HOME": str(external_model),
            "PYTEST_EXTERNAL_MEMORY_SETTINGS_CANARY": str(external_settings),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/test_test_runtime_isolation.py::test_subprocess_runtime_probe",
    ]
    result = subprocess.run(
        command,
        cwd=service_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0
    after = external_settings.stat()
    assert external_settings.read_bytes() == canary_bytes
    assert after.st_mtime_ns == before.st_mtime_ns
    assert list(external_home.iterdir()) == []
    assert list(external_model.iterdir()) == []


def test_subprocess_missing_external_settings_override_is_neutralized(
    tmp_path: Path,
) -> None:
    service_root = Path(__file__).resolve().parents[1]
    missing = tmp_path / "external-missing-settings.env"
    environment = dict(os.environ)
    environment.update(
        {
            "MEMGW_SETTINGS_PATH": str(missing),
            "PYTEST_EXTERNAL_MEMORY_SETTINGS_CANARY": str(missing),
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


def test_root_test_script_supplies_isolated_process_roots() -> None:
    script = (Path(__file__).resolve().parents[3] / "scripts" / "test.sh").read_text(
        encoding="utf-8"
    )

    assert script.count("env -i") == 2
    for name in (
        "MEMGW_HOME",
        "MEMGW_SETTINGS_PATH",
        "MODEL_GATEWAY_HOME",
        "MODEL_GATEWAY_SECRETS_PATH",
        "DATABASE_PATH",
        "AUTH_DATABASE_PATH",
        "KNOWLEDGE_DATABASE_PATH",
    ):
        assert f"{name}=" in script
