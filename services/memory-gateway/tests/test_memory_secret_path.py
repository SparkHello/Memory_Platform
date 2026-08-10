from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

from app.cli_config import cli_paths, write_env_atomic
from app.config import get_settings


def test_memory_settings_can_live_on_a_separate_volume(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "memory-data" / "config"
    settings_path = tmp_path / "memory-secrets" / "settings.env"
    monkeypatch.setenv("MEMGW_SETTINGS_PATH", str(settings_path))

    paths = cli_paths(home)

    assert paths.home == home
    assert paths.project_file == home / "project.json"
    assert paths.models == home / "models.json"
    assert paths.settings_env == settings_path


def test_private_settings_file_wins_for_secrets_but_allows_nonsecret_env_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.env"
    write_env_atomic(
        settings_path,
        {
            "GATEWAY_API_KEY": "file-only-secret",
            "DATABASE_PATH": str(tmp_path / "file.db"),
        },
    )
    monkeypatch.setenv("MEMGW_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("GATEWAY_API_KEY", "must-not-override")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "environment.db"))
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.gateway_api_key == "file-only-secret"
    assert settings.database_path == str(tmp_path / "environment.db")


def test_private_settings_file_rejects_unsafe_mode_and_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.env"
    write_env_atomic(settings_path, {"GATEWAY_API_KEY": "synthetic-secret"})
    settings_path.chmod(0o640)
    monkeypatch.setenv("MEMGW_SETTINGS_PATH", str(settings_path))
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="0600"):
        get_settings()

    settings_path.chmod(0o600)
    linked = tmp_path / "linked.env"
    linked.symlink_to(settings_path)
    monkeypatch.setenv("MEMGW_SETTINGS_PATH", str(linked))
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="安全读取"):
        get_settings()


def test_memory_entrypoint_never_sources_or_exports_private_settings() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "deploy" / "memory-entrypoint.sh").read_text(encoding="utf-8")

    assert "set -a" not in source
    assert '. "$MEMGW_SETTINGS_PATH"' not in source
    assert "source " not in source
    for name in (
        "GATEWAY_API_KEY",
        "GATEWAY_SIGNING_SECRET",
        "MODEL_GATEWAY_API_KEY",
    ):
        assert f"export {name}" not in source


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux /proc required")
def test_private_settings_secret_never_appears_in_process_environment(
    tmp_path: Path,
) -> None:
    secret = "proc-synthetic-secret-7d563a20"
    settings_path = tmp_path / "settings.env"
    ready_path = tmp_path / "ready"
    write_env_atomic(settings_path, {"GATEWAY_API_KEY": secret})
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.endswith(("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD"))
    }
    environment["MEMGW_SETTINGS_PATH"] = str(settings_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import time; "
                "from app.config import get_settings; "
                "assert get_settings().gateway_api_key; "
                f"Path({str(ready_path)!r}).write_text('ready'); time.sleep(5)"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists()
        assert secret.encode() not in Path(f"/proc/{process.pid}/environ").read_bytes()
    finally:
        process.terminate()
        process.wait(timeout=5)
