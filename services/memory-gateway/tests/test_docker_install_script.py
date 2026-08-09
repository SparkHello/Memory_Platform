from __future__ import annotations

import os
from pathlib import Path
import subprocess


PLATFORM_ROOT = Path(__file__).resolve().parents[3]


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_docker_script(*, preexisting: bool) -> str:
    existing = "fake-memory-platform-container" if preexisting else ""
    return f"""#!/bin/sh
if [ "$1" = "info" ]; then
  exit 0
fi
if [ "$1" = "cp" ]; then
  printf 'safe backup' > "$3"
  exit 0
fi
case "$*" in
  "compose version") exit 0 ;;
  "ps -a --filter label=com.docker.compose.service=memory-platform --format "*)
    printf '%s\\n' "${{DOCKER_EXISTING_DIR:-}}"
    exit 0
    ;;
  *" ps -aq memory-platform"*) printf '{existing}\\n'; exit 0 ;;
  *" ps -q memory-platform"*) printf '{existing}\\n'; exit 0 ;;
  *" port memory-platform 2026") printf '0.0.0.0:%s\\n' "$DOCKER_CURRENT_PORT"; exit 0 ;;
  "volume ls "*) exit 0 ;;
  *" exec -T memory-platform memgw stack backup "*) exit 0 ;;
  *" up -d"*) printf '%s|%s\\n' "$MEMORY_PORT" "$MEMORY_HOST" > "$DOCKER_CAPTURE"; exit 0 ;;
  *) exit 0 ;;
esac
"""


def test_repeated_docker_install_preserves_existing_compose_environment(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / ".env").write_text(
        "MEMORY_PORT=3026\n"
        "MEMORY_HOST=0.0.0.0\n"
        "CUSTOM_SETTING=keep-me\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "compose-up-environment.txt"
    _executable(
        fake_bin / "curl",
        """#!/bin/sh
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then
    printf 'services: {}\n' > "$argument"
    exit 0
  fi
  previous=$argument
done
exit 0
""",
    )
    _executable(
        fake_bin / "lsof",
        "#!/bin/sh\nexit 0\n",
    )
    _executable(fake_bin / "docker", _fake_docker_script(preexisting=True))

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "MEMORY_PLATFORM_DIR": str(install_dir),
        "DOCKER_CURRENT_PORT": "3026",
        "DOCKER_CAPTURE": str(capture),
        "MEMORY_NO_OPEN": "1",
    }
    environment.pop("MEMORY_PORT", None)
    environment.pop("MEMORY_HOST", None)

    result = subprocess.run(
        ["sh", str(PLATFORM_ROOT / "deploy" / "install.sh")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8").strip() == "3026|0.0.0.0"
    values = (install_dir / ".env").read_text(encoding="utf-8").splitlines()
    assert values == [
        "MEMORY_PORT=3026",
        "MEMORY_HOST=0.0.0.0",
        "CUSTOM_SETTING=keep-me",
    ]
    backups = list((install_dir / "backups").glob("pre-upgrade-*.zip"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "safe backup"
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_explicit_default_values_replace_stale_compose_environment(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / ".env").write_text(
        "MEMORY_PORT=3026\nMEMORY_HOST=0.0.0.0\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "compose-up-environment.txt"
    _executable(
        fake_bin / "curl",
        """#!/bin/sh
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then
    printf 'services: {}\n' > "$argument"
    exit 0
  fi
  previous=$argument
done
exit 0
""",
    )
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    _executable(fake_bin / "docker", _fake_docker_script(preexisting=True))
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "MEMORY_PLATFORM_DIR": str(install_dir),
        "MEMORY_PORT": "2026",
        "MEMORY_HOST": "127.0.0.1",
        "DOCKER_CAPTURE": str(capture),
        "DOCKER_CURRENT_PORT": "2026",
        "MEMORY_NO_OPEN": "1",
    }

    result = subprocess.run(
        ["sh", str(PLATFORM_ROOT / "deploy" / "install.sh")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8").strip() == "2026|127.0.0.1"
    assert (install_dir / ".env").read_text(encoding="utf-8").splitlines() == [
        "MEMORY_PORT=2026",
        "MEMORY_HOST=127.0.0.1",
    ]


def test_installer_finds_existing_install_when_run_from_another_directory(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "existing install"
    install_dir.mkdir()
    (install_dir / ".env").write_text("MEMORY_PORT=3026\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "compose-up-environment.txt"
    _executable(
        fake_bin / "curl",
        """#!/bin/sh
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then
    printf 'services: {}\n' > "$argument"
    exit 0
  fi
  previous=$argument
done
exit 0
""",
    )
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "docker", _fake_docker_script(preexisting=True))

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_EXISTING_DIR": str(install_dir),
        "DOCKER_CURRENT_PORT": "3026",
        "DOCKER_CAPTURE": str(capture),
        "MEMORY_NO_OPEN": "1",
    }
    environment.pop("MEMORY_PLATFORM_DIR", None)
    environment.pop("MEMORY_PORT", None)
    environment.pop("MEMORY_HOST", None)

    result = subprocess.run(
        ["sh", str(PLATFORM_ROOT / "deploy" / "install.sh")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"已找到现有安装：{install_dir}" in result.stdout
    assert (install_dir / "docker-compose.user.yml").is_file()
    assert not (tmp_path / "memory-platform").exists()
    assert capture.read_text(encoding="utf-8").strip() == "3026|127.0.0.1"
