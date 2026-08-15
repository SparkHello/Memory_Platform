from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the POSIX installer is covered only where a native sh is available",
)


PLATFORM_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = PLATFORM_ROOT / "deploy" / "install.sh"


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run(tmp_path: Path, install_dir: Path, fake_bin: Path, **extra: str):
    fake_cosign = fake_bin / "cosign"
    if not fake_cosign.exists():
        _executable(fake_cosign, "#!/bin/sh\nexit 0\n")
    fake_sync = fake_bin / "sync"
    if not fake_sync.exists():
        _executable(fake_sync, "#!/bin/sh\nexit 0\n")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "MEMORY_PLATFORM_DIR": str(install_dir),
        "MEMORY_NO_OPEN": "1",
        **extra,
    }
    environment.pop("GATEWAY_API_KEY", None)
    environment.pop("MEMORY_CONSOLE_ADMIN_KEY", None)
    return subprocess.run(
        ["sh", str(INSTALLER)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _curl_script(*, candidate: str = "services: {}\n") -> str:
    return f"""#!/bin/sh
previous=
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then
    printf '%s' '{candidate}' > "$argument"
    exit 0
  fi
  previous=$argument
done
[ -z "${{CURL_CAPTURE:-}}" ] || printf '%s\n' "$*" >> "$CURL_CAPTURE"
test -f "$MEMORY_PLATFORM_DIR/.test-ingress-published"
"""


def _fresh_docker_script() -> str:
    return """#!/bin/sh
if [ "$1" = "info" ]; then exit 0; fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  case "$3" in
    *memory-platform-init:*) printf 'ghcr.io/sparkhello/memory-platform-init@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' ;;
    *memory-platform-model:*) printf 'ghcr.io/sparkhello/memory-platform-model@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n' ;;
    *memory-platform-memory:*) printf 'ghcr.io/sparkhello/memory-platform-memory@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n' ;;
  esac
  exit 0
fi
if [ "$1" = "run" ]; then
  case "$*" in
    *'/usr/local/libexec/memory-platform/plan_install.py'*)
      printf '1\tupgrade\tfresh_install\tnone\t0\t0\t0\n'
      exit 0
      ;;
  esac
fi
case "$*" in
  "compose version") exit 0 ;;
  "volume ls "*) exit 0 ;;
  *" config --format json"*) printf '{}\n'; exit 0 ;;
  *" config"*) exit 0 ;;
  *" pull"*) exit 0 ;;
  *" ps -q model-gateway"*) printf 'synthetic-model\n'; exit 0 ;;
  *" ps -q memory-gateway"*) printf 'synthetic-memory\n'; exit 0 ;;
  *".compose.internal."*" port memory-gateway 2026"*) exit 0 ;;
  *" port memory-gateway 2026"*)
    test -f "$MEMORY_PLATFORM_DIR/.test-ingress-published" && printf '127.0.0.1:3026\n'
    exit 0
    ;;
  "port synthetic-memory"|"port synthetic-model") exit 0 ;;
  *" exec -T"*"/readyz"*) exit 99 ;;
  *".compose.internal."*" up -d"*)
    mkdir -p "$MEMORY_PLATFORM_DIR/credentials"
    printf 'synthetic-gateway-value\n' > "$MEMORY_PLATFORM_DIR/credentials/gateway.txt"
    printf 'synthetic-admin-value\n' > "$MEMORY_PLATFORM_DIR/credentials/admin.txt"
    chmod 600 "$MEMORY_PLATFORM_DIR/credentials/"*.txt
    exit 0
    ;;
  *" up -d --no-deps --force-recreate memory-gateway"*)
    : > "$MEMORY_PLATFORM_DIR/.test-ingress-published"
    candidate_port=$(awk -F= '$1=="MEMORY_PORT" { value=$2 } END { print value }' "$MEMORY_PLATFORM_DIR/.env")
    candidate_host=$(awk -F= '$1=="MEMORY_HOST" { value=$2 } END { print value }' "$MEMORY_PLATFORM_DIR/.env")
    printf '%s|%s\n' "$candidate_port" "$candidate_host" > "$DOCKER_CAPTURE"
    exit 0
    ;;
esac
exit 0
"""


@pytest.mark.parametrize(
    ("containers", "running", "inspect_exit", "probe_exit", "expected"),
    (
        ("", "true", "0", "0", "absent"),
        ("old-memory", "false", "0", "0", "absent"),
        ("old-memory", "true", "0", "0", "ready"),
        ("old-memory", "true", "0", "3", "not_ready"),
        ("old-memory", "true", "0", "4", "unknown"),
        ("first\nsecond", "true", "0", "0", "unknown"),
        ("old-memory", "true", "1", "0", "unknown"),
    ),
)
def test_existing_readiness_baseline_distinguishes_observed_states(
    tmp_path: Path,
    containers: str,
    running: str,
    inspect_exit: str,
    probe_exit: str,
    expected: str,
) -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    start = installer.index("existing_service_readiness() {")
    end = installer.index("\n}\n\ncompose_internal()", start) + 3
    readiness_function = installer[start:end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "docker",
        """#!/bin/sh
if [ "$1" = "inspect" ]; then
  printf '%s\n' "$READINESS_RUNNING"
  exit "$READINESS_INSPECT_EXIT"
fi
if [ "$1" = "exec" ]; then exit "$READINESS_PROBE_EXIT"; fi
exit 1
""",
    )
    harness = (
        readiness_function
        + "\ncompose() { printf '%s\\n' \"$READINESS_CONTAINERS\"; }\n"
        + "LAYOUT=split\nACTIVE_COMPOSE=old.yml\n"
        + "existing_service_readiness memory-gateway "
        + "http://127.0.0.1:2026/readyz\n"
    )
    result = subprocess.run(
        ["sh", "-c", harness],
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "READINESS_CONTAINERS": containers,
            "READINESS_RUNNING": running,
            "READINESS_INSPECT_EXIT": inspect_exit,
            "READINESS_PROBE_EXIT": probe_exit,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    ("listen_host", "probe_host"),
    (("0.0.0.0", "127.0.0.1"), ("192.0.2.44", "192.0.2.44")),
)
def test_fresh_install_pins_digests_and_delivers_only_credential_paths(
    tmp_path: Path,
    listen_host: str,
    probe_host: str,
):
    install_dir = tmp_path / "install with spaces"
    install_dir.mkdir()
    (install_dir / ".env").write_text(
        "CUSTOM_SETTING=keep-me\n"
        "GATEWAY_API_KEY=old-compose-residue\n"
        "MEMORY_CONSOLE_ADMIN_KEY=old-admin-residue\n"
        " export COMPOSE_PROFILES=maintenance\n"
        "export COMPOSE_ENV_FILES=alternate.env\n"
        " export MEMORY_HOST = unsafe-old-value\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "capture.txt"
    curl_capture = tmp_path / "curl-capture.txt"
    _executable(fake_bin / "curl", _curl_script())
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    _executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "docker", _fresh_docker_script())

    result = _run(
        tmp_path,
        install_dir,
        fake_bin,
        DOCKER_CAPTURE=str(capture),
        CURL_CAPTURE=str(curl_capture),
        MEMORY_PORT="3026",
        MEMORY_HOST=listen_host,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text().strip() == f"3026|{listen_host}"
    assert f"http://{probe_host}:3026/health" in curl_capture.read_text()
    env_text = (install_dir / ".env").read_text(encoding="utf-8")
    assert "CUSTOM_SETTING=keep-me" in env_text
    assert "GATEWAY_API_KEY=" not in env_text
    assert "MEMORY_CONSOLE_ADMIN_KEY=" not in env_text
    assert "COMPOSE_PROFILES=" not in env_text
    assert "COMPOSE_ENV_FILES=" not in env_text
    assert "unsafe-old-value" not in env_text
    assert f"MEMORY_HOST={listen_host}" in env_text
    assert "MEMORY_CREDENTIAL_DIR=./credentials" in env_text
    assert "memory-platform-init@sha256:" in env_text
    assert "memory-platform-model@sha256:" in env_text
    assert "memory-platform-memory@sha256:" in env_text
    assert "synthetic-gateway-value" not in result.stdout + result.stderr
    assert "synthetic-admin-value" not in result.stdout + result.stderr
    assert str(install_dir / "credentials" / "gateway.txt") in result.stdout
    assert (install_dir / "credentials" / "gateway.txt").stat().st_mode & 0o777 == 0o600


def test_installer_rejects_secret_environment_without_echoing_value(tmp_path: Path):
    install_dir = tmp_path / "install"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "docker", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
    secret = "must-never-be-reflected-by-installer"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "MEMORY_PLATFORM_DIR": str(install_dir),
        "GATEWAY_API_KEY": secret,
    }
    result = subprocess.run(
        ["sh", str(INSTALLER)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "不接受环境变量中的密钥" in result.stderr
    assert secret not in result.stdout + result.stderr


def test_installer_wide_lock_rejects_a_second_live_process_without_mutation(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    environment = install_dir / ".env"
    live.write_text("old-compose\n", encoding="utf-8")
    environment.write_bytes(b"OLD_ENV=exact\r\n")
    lock = install_dir / ".memory-platform-install.lock"
    lock.mkdir(mode=0o700)
    (lock / "owner").write_text(f"{os.getpid()}\n", encoding="ascii")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "docker",
        "#!/bin/sh\n"
        "[ \"$1\" = info ] && exit 0\n"
        "[ \"$1 $2\" = 'compose version' ] && exit 0\n"
        "exit 0\n",
    )
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 99\n")

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "另一安装器仍在运行" in result.stderr
    assert live.read_text(encoding="utf-8") == "old-compose\n"
    assert environment.read_bytes() == b"OLD_ENV=exact\r\n"
    assert lock.is_dir()


def test_existing_project_identity_rejects_conflicting_invocation(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    environment = install_dir / ".env"
    live.write_text("services:\n  memory-gateway: {}\n", encoding="utf-8")
    environment.write_bytes(b"COMPOSE_PROJECT_NAME=oldproject\r\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "docker",
        f"""#!/bin/sh
if [ "$1" = info ]; then exit 0; fi
if [ "$1 $2" = "compose version" ]; then exit 0; fi
case "$*" in
  *"com.docker.compose.project.working_dir={install_dir}"*)
    printf 'oldproject|memory-gateway\n'
    ;;
esac
exit 0
""",
    )
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 99\n")

    result = _run(
        tmp_path,
        install_dir,
        fake_bin,
        COMPOSE_PROJECT_NAME="wrongproject",
    )

    assert result.returncode != 0
    assert "与旧容器 project 身份冲突" in result.stderr
    assert live.read_text(encoding="utf-8") == "services:\n  memory-gateway: {}\n"
    assert environment.read_bytes() == b"COMPOSE_PROJECT_NAME=oldproject\r\n"


def test_invalid_candidate_does_not_replace_live_compose(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    live.write_text("operator-owned-old-compose\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "curl", _curl_script(candidate="invalid-candidate\\n"))
    _executable(
        fake_bin / "docker",
        """#!/bin/sh
if [ "$1" = "info" ]; then exit 0; fi
case "$*" in
  "compose version") exit 0 ;;
  *" config --services") exit 0 ;;
  "volume ls "*) exit 0 ;;
  *" config") exit 1 ;;
esac
exit 0
""",
    )
    result = _run(tmp_path, install_dir, fake_bin)
    assert result.returncode != 0
    assert "候选 Compose 语法无效" in result.stderr
    assert live.read_text(encoding="utf-8") == "operator-owned-old-compose\n"
    assert not list(install_dir.glob(".docker-compose.user.yml.candidate.*"))


def test_legacy_layout_is_referred_to_standalone_cutover_tool(tmp_path: Path):
    install_dir = tmp_path / "legacy"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    live.write_text("services:\n  memory-platform: {}\n", encoding="utf-8")
    environment = install_dir / ".env"
    environment.write_bytes(b"CUSTOM_SETTING=exact\r\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = tmp_path / "events"
    _executable(
        fake_bin / "curl",
        f"#!/bin/sh\nprintf 'curl:%s\n' \"$*\" >> '{events}'\nexit 99\n",
    )
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    _executable(
        fake_bin / "docker",
        f"""#!/bin/sh
printf '%s\n' "$*" >> '{events}'
if [ "$1" = "info" ]; then exit 0; fi
case "$*" in
  "compose version") exit 0 ;;
  *" config --services") printf 'memory-platform\n'; exit 0 ;;
esac
exit 0
""",
    )

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "旧单卷" in result.stderr
    assert "legacy_cutover.py" in result.stderr
    assert live.read_text(encoding="utf-8") == "services:\n  memory-platform: {}\n"
    assert environment.read_bytes() == b"CUSTOM_SETTING=exact\r\n"
    assert not (install_dir / ".memory-platform-cutover").exists()
    event_text = events.read_text(encoding="utf-8")
    # 拒绝发生在下载候选、停写或任何卷操作之前。
    assert "curl:" not in event_text
    assert " stop" not in event_text
    assert "volume rm" not in event_text


def test_split_readiness_failure_restores_old_compose_images_and_data(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "split"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    old_compose = """services:
  stack-init: {}
  model-gateway: {}
  memory-gateway: {}
"""
    live.write_text(old_compose, encoding="utf-8")
    (install_dir / ".env").write_text(
        "MEMORY_PLATFORM_INIT_IMAGE=old-init-tag\n"
        "MEMORY_PLATFORM_MODEL_IMAGE=old-model-tag\n"
        "MEMORY_PLATFORM_MEMORY_IMAGE=old-memory-tag\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = tmp_path / "events"
    _executable(
        fake_bin / "curl",
        f"""#!/bin/sh
previous=
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then
    printf 'download\n' >> '{events}'
    printf 'services:\n  stack-init: {{}}\n  model-gateway: {{}}\n  memory-gateway: {{}}\n' > "$argument"
    exit 0
  fi
  previous=$argument
done
    exit 1
""",
    )
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    _executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    _executable(
        fake_bin / "docker",
        f"""#!/bin/sh
if [ "$1" = "info" ]; then exit 0; fi
if [ "$1" = "cp" ]; then printf 'verified-backup' > "$3"; exit 0; fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  case "$3" in
    *memory-platform-init:*) printf 'ghcr.io/sparkhello/memory-platform-init@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' ;;
    *memory-platform-model:*) printf 'ghcr.io/sparkhello/memory-platform-model@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n' ;;
    *memory-platform-memory:*) printf 'ghcr.io/sparkhello/memory-platform-memory@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n' ;;
  esac
  exit 0
fi
if [ "$1" = "inspect" ]; then
  case "$*" in
    *'{{.State.Running}}'*) printf 'true\n'; exit 0 ;;
  esac
  case "$2" in
    old-init) printf 'sha256:1111111111111111111111111111111111111111111111111111111111111111\n' ;;
    old-model) printf 'sha256:2222222222222222222222222222222222222222222222222222222222222222\n' ;;
    old-memory) printf 'sha256:3333333333333333333333333333333333333333333333333333333333333333\n' ;;
  esac
  exit 0
fi
if [ "$1" = "volume" ] && [ "$2" = "ls" ]; then
  case "$*" in
    *'volume=memory-data'*) printf 'split_memory-data\n' ;;
    *'volume=memory-secrets'*) printf 'split_memory-secrets\n' ;;
    *'volume=model-data'*) printf 'split_model-data\n' ;;
  esac
  exit 0
fi
if [ "$1" = "run" ]; then
  case "$*" in
    *'/usr/local/libexec/memory-platform/plan_install.py'*)
      printf '1\tupgrade\timage_change\tnone\t1\t1\t1\n'
      ;;
    *'/usr/local/libexec/memory-platform/restore_split.py'*)
      printf 'restore:%s\n' "$*" >> '{events}'
      ;;
    *'stack backup'*) printf 'backup\n' >> '{events}' ;;
    *) printf 'container-run\n' >> '{events}' ;;
  esac
  exit 0
fi
if [ "$1" = "exec" ]; then exit 0; fi
case "$*" in
  'compose version') exit 0 ;;
  *' config --services') printf 'stack-init\nmodel-gateway\nmemory-gateway\n'; exit 0 ;;
  *' ps -aq stack-init') printf 'old-init\n'; exit 0 ;;
  *' ps -aq model-gateway') printf 'old-model\n'; exit 0 ;;
  *' ps -aq memory-gateway') printf 'old-memory\n'; exit 0 ;;
  *' port memory-gateway 2026') exit 0 ;;
  *' config --format json') printf '{{}}\n'; exit 0 ;;
  *' config') exit 0 ;;
  *' pull') exit 0 ;;
  *' exec -T'*'/readyz'*) exit 1 ;;
  *' exec -T'*) exit 0 ;;
  *' stop') printf 'stop\n' >> '{events}'; exit 0 ;;
  *' up -d --pull never'*)
    awk '/^MEMORY_PLATFORM_(INIT|MODEL|MEMORY)_IMAGE=/' "$MEMORY_PLATFORM_DIR/.env" >> '{events}'
    printf 'process-images:%s|%s|%s\n' \
      "$MEMORY_PLATFORM_INIT_IMAGE" \
      "$MEMORY_PLATFORM_MODEL_IMAGE" \
      "$MEMORY_PLATFORM_MEMORY_IMAGE" >> '{events}'
    exit 0
    ;;
  *' up -d') exit 0 ;;
esac
exit 0
""",
    )

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "readiness 退化；旧服务和数据已恢复" in result.stderr
    assert live.read_text(encoding="utf-8") == old_compose
    environment = (install_dir / ".env").read_text(encoding="utf-8")
    assert environment == (
        "MEMORY_PLATFORM_INIT_IMAGE=old-init-tag\n"
        "MEMORY_PLATFORM_MODEL_IMAGE=old-model-tag\n"
        "MEMORY_PLATFORM_MEMORY_IMAGE=old-memory-tag\n"
    )
    event_text = events.read_text(encoding="utf-8")
    assert "restore:" in event_text
    assert "sha256:" + "1" * 64 in event_text
    assert (
        "process-images:sha256:"
        + "1" * 64
        + "|sha256:"
        + "2" * 64
        + "|sha256:"
        + "3" * 64
    ) in event_text
    assert "ghcr.io/sparkhello/memory-platform-init@sha256:" + "a" * 64 not in event_text
    # 单一停写备份：旧栈 stop 之后、数据可能变化之前创建一致性备份。
    assert event_text.index("stop") < event_text.index("backup")
    assert event_text.index("backup") < event_text.index("restore:")
    assert next((install_dir / "backups").glob("pre-upgrade-*.zip")).read_text() == (
        "verified-backup"
    )


def _split_preflight_docker_script(
    *,
    events: Path,
    config_count: Path,
    fail_second_config: bool,
    fail_stop: bool,
    has_init_container: bool = True,
    fail_validator: bool = False,
    old_readiness_exit: int = 0,
    plan_line: str = "1\\tupgrade\\timage_change\\tnone\\t1\\t1\\t1",
) -> str:
    second_config_branch = (
        "if [ \"$count\" -eq 2 ]; then exit 1; fi" if fail_second_config else ":"
    )
    stop_branch = "exit 1" if fail_stop else "exit 0"
    validator_branch = "exit 1" if fail_validator else "exit 0"
    init_container_branch = "printf 'old-init\\n'" if has_init_container else ":"
    return f"""#!/bin/sh
if [ "$1" = "info" ]; then exit 0; fi
if [ "$1" = "cp" ]; then printf 'verified-backup' > "$3"; exit 0; fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  case "$3" in
    *memory-platform-init:*) printf 'ghcr.io/sparkhello/memory-platform-init@sha256:{'a' * 64}\n' ;;
    *memory-platform-model:*) printf 'ghcr.io/sparkhello/memory-platform-model@sha256:{'b' * 64}\n' ;;
    *memory-platform-memory:*) printf 'ghcr.io/sparkhello/memory-platform-memory@sha256:{'c' * 64}\n' ;;
  esac
  exit 0
fi
if [ "$1" = "inspect" ]; then
  case "$*" in
    *'{{.State.Running}}'*) printf 'true\n'; exit 0 ;;
  esac
  case "$2" in
    old-init) printf 'sha256:{'1' * 64}\n' ;;
    old-model) printf 'sha256:{'2' * 64}\n' ;;
    old-memory) printf 'sha256:{'3' * 64}\n' ;;
  esac
  exit 0
fi
if [ "$1" = "run" ]; then
    case "$*" in
    *'/usr/local/libexec/memory-platform/plan_install.py'*)
      printf '{plan_line}\n'
      exit 0
      ;;
    *'/usr/local/libexec/memory-platform/validate_compose.py'*)
      printf 'candidate-validator\n' >> '{events}'
      {validator_branch}
      ;;
  esac
fi
if [ "$1" = "exec" ]; then exit {old_readiness_exit}; fi
case "$*" in
  'compose version') exit 0 ;;
  'volume ls '*) exit 0 ;;
  *' config --services') printf 'stack-init\nmodel-gateway\nmemory-gateway\n'; exit 0 ;;
  *' ps -aq stack-init') {init_container_branch}; exit 0 ;;
  *' ps -aq model-gateway') printf 'old-model\n'; exit 0 ;;
  *' ps -aq memory-gateway') printf 'old-memory\n'; exit 0 ;;
  *' ps -q model-gateway') printf 'old-model\n'; exit 0 ;;
  *' ps -q memory-gateway') printf 'old-memory\n'; exit 0 ;;
  *' port memory-gateway 2026') printf '127.0.0.1:2026\n'; exit 0 ;;
  *'--profile maintenance run'*'stack backup'*) printf 'backup\n' >> '{events}'; exit 0 ;;
  *' exec -T'*) exit 0 ;;
  *' config'|*' config --format json')
    count=0
    [ ! -f '{config_count}' ] || count=$(cat '{config_count}')
    count=$((count+1))
    printf '%s' "$count" > '{config_count}'
    printf 'candidate-config:%s\n' "$count" >> '{events}'
    {second_config_branch}
    printf '{{}}\n'
    exit 0
    ;;
  *' pull') printf 'candidate-pull\n' >> '{events}'; exit 0 ;;
  *' stop') printf 'old-stop\n' >> '{events}'; {stop_branch} ;;
  *' up -d --no-deps --force-recreate model-gateway')
    printf 'repair-model\n' >> '{events}'; exit 0 ;;
  *' up -d --no-deps --force-recreate memory-gateway')
    printf 'repair-memory\n' >> '{events}'; exit 0 ;;
  *' up -d --pull never'*)
    printf 'recovery-images:%s|%s|%s\n' \
      "$MEMORY_PLATFORM_INIT_IMAGE" \
      "$MEMORY_PLATFORM_MODEL_IMAGE" \
      "$MEMORY_PLATFORM_MEMORY_IMAGE" >> '{events}'
    exit 0
    ;;
esac
exit 0
"""


def _prepare_split_preflight_case(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    install_dir = tmp_path / "split"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    live.write_text(
        "services:\n  stack-init: {{}}\n  model-gateway: {{}}\n  memory-gateway: {{}}\n",
        encoding="utf-8",
    )
    (install_dir / ".env").write_text(
        "CUSTOM_SETTING=keep-me\n"
        "MEMORY_PLATFORM_INIT_IMAGE=old-init-tag\n"
        "MEMORY_PLATFORM_MODEL_IMAGE=old-model-tag\n"
        "MEMORY_PLATFORM_MEMORY_IMAGE=old-memory-tag\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = tmp_path / "events"
    config_count = tmp_path / "config-count"
    _executable(fake_bin / "curl", _curl_script(candidate="services: {{}}\n"))
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    return install_dir, fake_bin, events, config_count


def _assert_old_live_image_refs(install_dir: Path) -> None:
    env_text = (install_dir / ".env").read_text(encoding="utf-8")
    assert "MEMORY_PLATFORM_INIT_IMAGE=old-init-tag" in env_text
    assert "MEMORY_PLATFORM_MODEL_IMAGE=old-model-tag" in env_text
    assert "MEMORY_PLATFORM_MEMORY_IMAGE=old-memory-tag" in env_text
    assert "@sha256:" not in env_text


def test_shared_candidate_validation_failure_does_not_pollute_live_env(
    tmp_path: Path,
) -> None:
    install_dir, fake_bin, events, config_count = _prepare_split_preflight_case(
        tmp_path
    )
    original_compose = (install_dir / "docker-compose.user.yml").read_text()
    _executable(
        fake_bin / "docker",
        _split_preflight_docker_script(
            events=events,
            config_count=config_count,
            fail_second_config=True,
            fail_stop=False,
        ),
    )

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "候选 public Compose 无法渲染为可审计配置" in result.stderr
    assert (install_dir / "docker-compose.user.yml").read_text() == original_compose
    _assert_old_live_image_refs(install_dir)
    assert "old-stop" not in events.read_text(encoding="utf-8")
    assert not list(install_dir.glob(".env.candidate.*"))
    assert not list(install_dir.glob(".docker-compose.user.yml.candidate.*"))


def test_candidate_validator_failure_precedes_journal_stop_and_volume_mutation(
    tmp_path: Path,
) -> None:
    install_dir, fake_bin, events, config_count = _prepare_split_preflight_case(
        tmp_path
    )
    original_compose = (install_dir / "docker-compose.user.yml").read_text()
    _executable(
        fake_bin / "docker",
        _split_preflight_docker_script(
            events=events,
            config_count=config_count,
            fail_second_config=False,
            fail_stop=False,
            fail_validator=True,
        ),
    )

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "候选 public Compose 未通过安全拓扑校验" in result.stderr
    assert (install_dir / "docker-compose.user.yml").read_text() == original_compose
    _assert_old_live_image_refs(install_dir)
    event_text = events.read_text(encoding="utf-8")
    assert "candidate-validator" in event_text
    assert "old-stop" not in event_text
    assert "backup" not in event_text
    assert not (install_dir / ".memory-platform-cutover").exists()


def test_unknown_old_readiness_fails_before_journal_and_stop(tmp_path: Path) -> None:
    install_dir, fake_bin, events, config_count = _prepare_split_preflight_case(
        tmp_path
    )
    original_compose = (install_dir / "docker-compose.user.yml").read_text()
    _executable(
        fake_bin / "docker",
        _split_preflight_docker_script(
            events=events,
            config_count=config_count,
            fail_second_config=False,
            fail_stop=False,
            old_readiness_exit=4,
        ),
    )

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "无法可靠建立旧服务 readiness 基线" in result.stderr
    assert (install_dir / "docker-compose.user.yml").read_text() == original_compose
    _assert_old_live_image_refs(install_dir)
    event_text = events.read_text(encoding="utf-8")
    assert "candidate-validator" in event_text
    assert "old-stop" not in event_text
    assert "backup" not in event_text
    assert not (install_dir / ".memory-platform-cutover").exists()


@pytest.mark.parametrize(
    ("action", "readiness_exit", "plan_line", "expected_repairs"),
    (
        (
            "noop",
            0,
            "1\\tnoop\\talready_current\\tnone\\t1\\t1\\t1",
            set(),
        ),
        (
            "repair",
            3,
            "1\\trepair\\tservice_repair\\tboth\\t0\\t0\\t0",
            {"repair-model", "repair-memory"},
        ),
    ),
)
def test_noop_and_repair_never_enter_upgrade_transaction(
    tmp_path: Path,
    action: str,
    readiness_exit: int,
    plan_line: str,
    expected_repairs: set[str],
) -> None:
    install_dir, fake_bin, events, config_count = _prepare_split_preflight_case(
        tmp_path
    )
    credentials = install_dir / "credentials"
    credentials.mkdir()
    for role in ("gateway", "admin"):
        credential = credentials / f"{role}.txt"
        credential.write_text(f"synthetic-{role}\n", encoding="ascii")
        credential.chmod(0o600)
    (install_dir / ".test-ingress-published").touch()
    original_compose = (install_dir / "docker-compose.user.yml").read_text()
    _executable(
        fake_bin / "docker",
        _split_preflight_docker_script(
            events=events,
            config_count=config_count,
            fail_second_config=False,
            fail_stop=False,
            old_readiness_exit=readiness_exit,
            plan_line=plan_line,
        ),
    )
    _executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode == 0, result.stderr
    assert f"已通过 {action} 验收" in result.stdout
    assert (install_dir / "docker-compose.user.yml").read_text() == original_compose
    _assert_old_live_image_refs(install_dir)
    event_lines = set(events.read_text(encoding="utf-8").splitlines())
    assert expected_repairs <= event_lines
    if action == "noop":
        assert "repair-model" not in event_lines
        assert "repair-memory" not in event_lines
    assert "old-stop" not in event_lines
    assert "backup" not in event_lines
    assert not (install_dir / ".memory-platform-cutover").exists()
    assert not list((install_dir / "backups").glob("pre-upgrade-*"))


def test_old_stop_failure_keeps_live_env_and_recovers_with_exact_old_images(
    tmp_path: Path,
) -> None:
    install_dir, fake_bin, events, config_count = _prepare_split_preflight_case(
        tmp_path
    )
    original_compose = (install_dir / "docker-compose.user.yml").read_text()
    _executable(
        fake_bin / "docker",
        _split_preflight_docker_script(
            events=events,
            config_count=config_count,
            fail_second_config=False,
            fail_stop=True,
        ),
    )

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "无法停止旧服务；未开始迁移" in result.stderr
    assert (install_dir / "docker-compose.user.yml").read_text() == original_compose
    _assert_old_live_image_refs(install_dir)
    event_text = events.read_text(encoding="utf-8")
    assert "candidate-config:2" in event_text
    assert "old-stop" in event_text
    assert (
        "recovery-images:sha256:"
        + "1" * 64
        + "|sha256:"
        + "2" * 64
        + "|sha256:"
        + "3" * 64
    ) in event_text
    assert "recovery-images:ghcr.io/sparkhello" not in event_text
    assert not list(install_dir.glob(".env.candidate.*"))
    assert not list(install_dir.glob(".docker-compose.user.yml.candidate.*"))


def test_old_stop_failure_uses_env_repository_digest_when_init_container_was_pruned(
    tmp_path: Path,
) -> None:
    install_dir, fake_bin, events, config_count = _prepare_split_preflight_case(
        tmp_path
    )
    old_init = "ghcr.io/sparkhello/memory-platform-init@sha256:" + "4" * 64
    environment_path = install_dir / ".env"
    environment_path.write_text(
        environment_path.read_text(encoding="utf-8").replace(
            "MEMORY_PLATFORM_INIT_IMAGE=old-init-tag",
            f"MEMORY_PLATFORM_INIT_IMAGE={old_init}",
        ),
        encoding="utf-8",
    )
    _executable(
        fake_bin / "docker",
        _split_preflight_docker_script(
            events=events,
            config_count=config_count,
            fail_second_config=False,
            fail_stop=True,
            has_init_container=False,
        ),
    )

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "无法停止旧服务；未开始迁移" in result.stderr
    assert f"recovery-images:{old_init}|sha256:{'2' * 64}|sha256:{'3' * 64}" in (
        events.read_text(encoding="utf-8")
    )
    assert f"MEMORY_PLATFORM_INIT_IMAGE={old_init}" in environment_path.read_text()
    assert not (install_dir / ".memory-platform-cutover").exists()


def test_signature_failure_is_fail_closed_before_live_cutover(tmp_path: Path) -> None:
    install_dir, fake_bin, events, config_count = _prepare_split_preflight_case(
        tmp_path
    )
    original_compose = (install_dir / "docker-compose.user.yml").read_text()
    _executable(
        fake_bin / "docker",
        _split_preflight_docker_script(
            events=events,
            config_count=config_count,
            fail_second_config=False,
            fail_stop=False,
        ),
    )
    _executable(
        fake_bin / "cosign",
        "#!/bin/sh\n[ \"$1\" = verify-blob ] && exit 0\nexit 1\n",
    )

    # 验签默认跳过，需显式开启后才应 fail-closed。
    result = _run(tmp_path, install_dir, fake_bin, MEMORY_VERIFY_SIGNATURES="1")

    assert result.returncode != 0
    assert "发布镜像签名无效" in result.stderr
    assert (install_dir / "docker-compose.user.yml").read_text() == original_compose
    _assert_old_live_image_refs(install_dir)
    assert "old-stop" not in events.read_text(encoding="utf-8")
    assert not (install_dir / ".memory-platform-cutover").exists()


def _write_interrupted_cutover_journal(
    install_dir: Path,
    *,
    phase: str,
    init_image: str | None = None,
) -> tuple[str, str]:
    init_image = init_image or f"sha256:{'1' * 64}"
    old_compose = (
        "services:\n  stack-init: {}\n  model-gateway: {}\n  memory-gateway: {}\n"
    )
    old_environment = (
        "CUSTOM_SETTING=old-safe-value\n"
        f"MEMORY_PLATFORM_INIT_IMAGE=sha256:{'1' * 64}\n"
        f"MEMORY_PLATFORM_MODEL_IMAGE=sha256:{'2' * 64}\n"
        f"MEMORY_PLATFORM_MEMORY_IMAGE=sha256:{'3' * 64}\n"
    )
    journal = install_dir / ".memory-platform-cutover"
    journal.mkdir(mode=0o700)
    (journal / "old-compose.yml").write_text(old_compose, encoding="utf-8")
    (journal / "old.env").write_text(old_environment, encoding="utf-8")
    (journal / "phase").write_text(phase + "\n", encoding="ascii")
    (journal / "metadata").write_text(
        "version=1\n"
        "project=journal-project\n"
        "layout=split\n"
        "backup=pre-upgrade-journal.zip\n"
        f"old_init_image={init_image}\n"
        f"old_model_image=sha256:{'2' * 64}\n"
        f"old_memory_image=sha256:{'3' * 64}\n",
        encoding="ascii",
    )
    for item in journal.iterdir():
        item.chmod(0o600)
    backups = install_dir / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "pre-upgrade-journal.zip").write_bytes(b"synthetic-backup")
    return old_compose, old_environment


def _interrupted_cutover_docker_script(*, events: Path) -> str:
    return f"""#!/bin/sh
if [ "$1" = "info" ]; then exit 0; fi
if [ "$1" = "cp" ]; then printf 'verified-backup' > "$3"; exit 0; fi
if [ "$1" = "inspect" ]; then
  case "$2" in
    old-init) printf 'sha256:{'1' * 64}\n' ;;
    old-model) printf 'sha256:{'2' * 64}\n' ;;
    old-memory) printf 'sha256:{'3' * 64}\n' ;;
  esac
  exit 0
fi
if [ "$1" = "run" ]; then printf 'restore:%s\n' "$*" >> '{events}'; exit 0; fi
if [ "$1" = "stop" ]; then printf 'recover-stop:%s\n' "$2" >> '{events}'; exit 0; fi
if [ "$1" = "volume" ] && [ "$2" = "ls" ]; then
  case "$*" in
    *'volume=memory-data'*) printf 'journal_memory-data\n' ;;
    *'volume=memory-secrets'*) printf 'journal_memory-secrets\n' ;;
    *'volume=model-data'*) printf 'journal_model-data\n' ;;
  esac
  exit 0
fi
case "$*" in
  'compose version') exit 0 ;;
  *'ps -aq --filter label=com.docker.compose.project=journal-project'*) printf 'interrupted-container\n'; exit 0 ;;
  *' config --services') printf 'stack-init\nmodel-gateway\nmemory-gateway\n'; exit 0 ;;
  *' ps -aq stack-init') printf 'old-init\n'; exit 0 ;;
  *' ps -aq model-gateway') printf 'old-model\n'; exit 0 ;;
  *' ps -aq memory-gateway') printf 'old-memory\n'; exit 0 ;;
  *' port memory-gateway 2026') exit 0 ;;
  *' up -d --pull never'*)
    printf 'recover-up:%s|%s|%s\n' \
      "$MEMORY_PLATFORM_INIT_IMAGE" \
      "$MEMORY_PLATFORM_MODEL_IMAGE" \
      "$MEMORY_PLATFORM_MEMORY_IMAGE" >> '{events}'
    exit 0
    ;;
  *'--profile maintenance run'*'stack backup'*) printf 'backup\n' >> '{events}'; exit 0 ;;
  *' exec -T'*) exit 0 ;;
esac
exit 0
"""


@pytest.mark.parametrize(
    ("phase", "expect_restore"),
    (("prepared", False), ("data_may_change", True)),
)
def test_interrupted_cutover_is_recovered_before_new_candidate_work(
    tmp_path: Path,
    phase: str,
    expect_restore: bool,
) -> None:
    install_dir = tmp_path / "interrupted"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    live.write_text("services:\n  candidate-interrupted: {}\n", encoding="utf-8")
    (install_dir / ".env").write_text(
        "MEMORY_PLATFORM_INIT_IMAGE=candidate-init\n"
        "MEMORY_PLATFORM_MODEL_IMAGE=candidate-model\n"
        "MEMORY_PLATFORM_MEMORY_IMAGE=candidate-memory\n",
        encoding="utf-8",
    )
    old_compose, _ = _write_interrupted_cutover_journal(
        install_dir, phase=phase
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = tmp_path / "events"
    _executable(fake_bin / "docker", _interrupted_cutover_docker_script(events=events))
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 1\n")
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "下载发布版 Compose 失败" in result.stderr
    assert live.read_text(encoding="utf-8") == old_compose
    _assert_old_live_image_refs_for_journal(install_dir)
    assert not (install_dir / ".memory-platform-cutover").exists()
    event_text = events.read_text(encoding="utf-8")
    assert "recover-stop:interrupted-container" in event_text
    assert (
        "recover-up:sha256:"
        + "1" * 64
        + "|sha256:"
        + "2" * 64
        + "|sha256:"
        + "3" * 64
    ) in event_text
    assert ("restore:" in event_text) is expect_restore
    if expect_restore:
        assert "--network none" in event_text
        assert "sha256:" + "1" * 64 in event_text
    # 恢复先于任何新候选工作；新流程只在旧栈停写后才创建备份，
    # 而本例在下载候选时即失败，因此不应出现任何备份事件。
    assert "stack backup" not in event_text
    assert event_text.index("recover-stop") < event_text.index("recover-up")


def _assert_old_live_image_refs_for_journal(install_dir: Path) -> None:
    env_text = (install_dir / ".env").read_text(encoding="utf-8")
    assert f"MEMORY_PLATFORM_INIT_IMAGE=sha256:{'1' * 64}" in env_text
    assert f"MEMORY_PLATFORM_MODEL_IMAGE=sha256:{'2' * 64}" in env_text
    assert f"MEMORY_PLATFORM_MEMORY_IMAGE=sha256:{'3' * 64}" in env_text
    assert "candidate-init" not in env_text


def test_incomplete_cutover_journal_fails_closed_before_container_stop(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "interrupted"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    live.write_text("operator-live-compose\n", encoding="utf-8")
    journal = install_dir / ".memory-platform-cutover"
    journal.mkdir(mode=0o700)
    (journal / "metadata").write_text("version=1\n", encoding="ascii")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = tmp_path / "events"
    _executable(
        fake_bin / "docker",
        f"#!/bin/sh\nprintf '%s\n' \"$*\" >> '{events}'\ncase \"$*\" in 'info'|'compose version') exit 0;; esac\nexit 0\n",
    )
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 1\n")

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "journal 不完整" in result.stderr
    assert live.read_text(encoding="utf-8") == "operator-live-compose\n"
    assert "stop" not in events.read_text(encoding="utf-8")


@pytest.mark.parametrize("remaining", ("all", "phase_only", "empty"))
def test_committed_cutover_cleanup_never_rolls_back_an_accepted_stack(
    tmp_path: Path,
    remaining: str,
) -> None:
    install_dir = tmp_path / "committed"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    accepted = "services:\n  accepted-new-stack: {}\n"
    live.write_text(accepted, encoding="utf-8")
    (install_dir / ".env").write_text("ACCEPTED_NEW_STATE=1\n", encoding="utf-8")
    _write_interrupted_cutover_journal(install_dir, phase="committed")
    journal = install_dir / ".memory-platform-cutover"
    if remaining == "phase_only":
        for name in ("metadata", "old-compose.yml", "old.env"):
            (journal / name).unlink()
    elif remaining == "empty":
        for item in journal.iterdir():
            item.unlink()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = tmp_path / "events"
    _executable(
        fake_bin / "docker",
        f"#!/bin/sh\nprintf '%s\n' \"$*\" >> '{events}'\n"
        "case \"$*\" in 'info'|'compose version') exit 0;; esac\nexit 0\n",
    )
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 1\n")
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "下载发布版 Compose 失败" in result.stderr
    assert live.read_text(encoding="utf-8") == accepted
    assert "ACCEPTED_NEW_STATE=1" in (install_dir / ".env").read_text()
    assert not journal.exists()
    assert " stop" not in events.read_text(encoding="utf-8")


def test_interrupted_cutover_accepts_exact_repository_digest_without_init_container(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "repo-digest"
    install_dir.mkdir()
    live = install_dir / "docker-compose.user.yml"
    live.write_text("services:\n  candidate-interrupted: {}\n", encoding="utf-8")
    (install_dir / ".env").write_text("CANDIDATE=1\n", encoding="utf-8")
    init_reference = (
        "ghcr.io/sparkhello/memory-platform-init@sha256:" + "a" * 64
    )
    old_compose, _ = _write_interrupted_cutover_journal(
        install_dir,
        phase="prepared",
        init_image=init_reference,
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = tmp_path / "events"
    _executable(fake_bin / "docker", _interrupted_cutover_docker_script(events=events))
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 1\n")
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "下载发布版 Compose 失败" in result.stderr
    assert live.read_text(encoding="utf-8") == old_compose
    event_text = events.read_text(encoding="utf-8")
    assert f"recover-up:{init_reference}|sha256:" in event_text
    assert not (install_dir / ".memory-platform-cutover").exists()


def test_missing_credentials_after_readiness_rolls_back_old_split_stack(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "credential-rollback"
    install_dir.mkdir()
    old_compose = (
        "services:\n  stack-init: {}\n  model-gateway: {}\n  memory-gateway: {}\n"
    )
    live = install_dir / "docker-compose.user.yml"
    live.write_text(old_compose, encoding="utf-8")
    (install_dir / ".env").write_text(
        "CUSTOM_SETTING=old\n"
        "MEMORY_PLATFORM_INIT_IMAGE=old-init-tag\n"
        "MEMORY_PLATFORM_MODEL_IMAGE=old-model-tag\n"
        "MEMORY_PLATFORM_MEMORY_IMAGE=old-memory-tag\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = tmp_path / "events"
    _executable(
        fake_bin / "curl",
        f"""#!/bin/sh
previous=
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then
    printf 'services:\n  stack-init: {{}}\n  model-gateway: {{}}\n  memory-gateway: {{}}\n' > "$argument"
    exit 0
  fi
  previous=$argument
done
    exit 1
""",
    )
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    _executable(
        fake_bin / "docker",
        f"""#!/bin/sh
if [ "$1" = info ]; then exit 0; fi
if [ "$1" = cp ]; then printf 'verified-backup' > "$3"; exit 0; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  case "$3" in
    *memory-platform-init:*) printf 'ghcr.io/sparkhello/memory-platform-init@sha256:{'a' * 64}\n' ;;
    *memory-platform-model:*) printf 'ghcr.io/sparkhello/memory-platform-model@sha256:{'b' * 64}\n' ;;
    *memory-platform-memory:*) printf 'ghcr.io/sparkhello/memory-platform-memory@sha256:{'c' * 64}\n' ;;
  esac
  exit 0
fi
if [ "$1" = inspect ]; then
  case "$*" in
    *'{{.State.Running}}'*) printf 'true\n'; exit 0 ;;
  esac
  case "$2" in
    old-init) printf 'sha256:{'1' * 64}\n' ;;
    old-model) printf 'sha256:{'2' * 64}\n' ;;
    old-memory) printf 'sha256:{'3' * 64}\n' ;;
  esac
  exit 0
fi
if [ "$1" = volume ] && [ "$2" = ls ]; then
  case "$*" in
    *'volume=memory-data'*) printf 'credential_memory-data\n' ;;
    *'volume=memory-secrets'*) printf 'credential_memory-secrets\n' ;;
    *'volume=model-data'*) printf 'credential_model-data\n' ;;
  esac
  exit 0
fi
if [ "$1" = run ]; then
  case "$*" in
    *plan_install.py*)
      printf '1\tupgrade\timage_change\tnone\t1\t1\t1\n'
      exit 0
      ;;
    *restore_split.py*) printf 'restore\n' >> '{events}' ;;
    *) printf 'topology\n' >> '{events}' ;;
  esac
  exit 0
fi
case "$*" in
  'compose version') exit 0 ;;
  *' config --services') printf 'stack-init\nmodel-gateway\nmemory-gateway\n'; exit 0 ;;
  *' ps -aq stack-init') printf 'old-init\n'; exit 0 ;;
  *' ps -aq model-gateway') printf 'old-model\n'; exit 0 ;;
  *' ps -aq memory-gateway') printf 'old-memory\n'; exit 0 ;;
  *' port memory-gateway 2026') exit 0 ;;
  *'--profile maintenance run'*'stack backup'*) exit 0 ;;
  *' exec -T'*) exit 0 ;;
  *' config --format json') printf '{{}}\n'; exit 0 ;;
  *' config'|*' pull'|*' stop') exit 0 ;;
  *' up -d --pull never'*)
    printf 'old-up:%s|%s|%s\n' "$MEMORY_PLATFORM_INIT_IMAGE" "$MEMORY_PLATFORM_MODEL_IMAGE" "$MEMORY_PLATFORM_MEMORY_IMAGE" >> '{events}'
    exit 0
    ;;
  *' up -d') printf 'candidate-up\n' >> '{events}'; exit 0 ;;
esac
exit 0
""",
    )

    result = _run(tmp_path, install_dir, fake_bin)

    assert result.returncode != 0
    assert "credentials；旧服务和数据已恢复" in result.stderr
    assert live.read_text(encoding="utf-8") == old_compose
    environment = (install_dir / ".env").read_text(encoding="utf-8")
    assert environment == (
        "CUSTOM_SETTING=old\n"
        "MEMORY_PLATFORM_INIT_IMAGE=old-init-tag\n"
        "MEMORY_PLATFORM_MODEL_IMAGE=old-model-tag\n"
        "MEMORY_PLATFORM_MEMORY_IMAGE=old-memory-tag\n"
    )
    event_text = events.read_text(encoding="utf-8")
    assert "candidate-up" in event_text and "restore" in event_text
    assert f"old-up:sha256:{'1' * 64}|sha256:{'2' * 64}|sha256:{'3' * 64}" in event_text
    assert not (install_dir / ".memory-platform-cutover").exists()


def test_interrupted_legacy_journal_fails_closed_without_touching_state(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "legacy-interrupted"
    install_dir.mkdir()
    old_compose = "services:\n  memory-platform: {}\n"
    live = install_dir / "docker-compose.user.yml"
    live.write_text("services:\n  interrupted-candidate: {}\n", encoding="utf-8")
    (install_dir / ".env").write_text("CANDIDATE=1\n", encoding="utf-8")
    journal = install_dir / ".memory-platform-cutover"
    journal.mkdir(mode=0o700)
    (journal / "old-compose.yml").write_text(old_compose, encoding="utf-8")
    (journal / "old.env").write_text("OLD_ENV=1\n", encoding="utf-8")
    (journal / "phase").write_text("data_may_change\n", encoding="ascii")
    (journal / "metadata").write_text(
        "version=1\n"
        "project=journal-project\n"
        "layout=legacy\n"
        "backup=pre-upgrade-legacy.zip\n"
        "old_init_image=\n"
        "old_model_image=\n"
        "old_memory_image=\n"
        "legacy_targets_absent=1\n",
        encoding="ascii",
    )
    backups = install_dir / "backups"
    backups.mkdir()
    (backups / "pre-upgrade-legacy.zip").write_bytes(b"synthetic-backup")
    events = tmp_path / "events"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 1\n")
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    _executable(
        fake_bin / "docker",
        f"#!/bin/sh\nprintf '%s\n' \"$*\" >> '{events}'\n"
        "case \"$*\" in 'info'|'compose version') exit 0;; esac\nexit 0\n",
    )

    result = _run(tmp_path, install_dir, fake_bin)

    # 旧版安装器留下的 legacy 中断 journal 不被新安装器静默恢复或丢弃；
    # fail-closed 并指向独立迁移工具/旧版安装器。
    assert result.returncode != 0
    assert "legacy 迁移" in result.stderr
    assert "legacy_cutover.py" in result.stderr
    assert live.read_text(encoding="utf-8") == "services:\n  interrupted-candidate: {}\n"
    assert journal.exists()
    event_text = events.read_text(encoding="utf-8")
    assert "stop" not in event_text
    assert "volume rm" not in event_text
