from __future__ import annotations

import importlib.util
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from app.auth.tokens import AuthTokenStore
from app.cli_config import read_env_file, write_env_atomic


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="split topology helpers run inside Linux containers",
)


ROOT = Path(__file__).resolve().parents[3]


def _load_initializer():
    script = ROOT / "deploy" / "init_stack.py"
    spec = importlib.util.spec_from_file_location("split_stack_initializer", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_restore_helper():
    script = ROOT / "deploy" / "restore_split.py"
    spec = importlib.util.spec_from_file_location("split_stack_restore", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_compose_validator():
    script = ROOT / "deploy" / "validate_compose.py"
    spec = importlib.util.spec_from_file_location("split_compose_validator", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compose() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "docker-compose.user.yml").read_text())


def _rendered_release_compose() -> tuple[dict, dict[str, str]]:
    images = {
        "init_image": "ghcr.io/sparkhello/memory-platform-init@sha256:" + "a" * 64,
        "model_image": "ghcr.io/sparkhello/memory-platform-model@sha256:" + "b" * 64,
        "memory_image": "ghcr.io/sparkhello/memory-platform-memory@sha256:" + "c" * 64,
    }
    environment = {
        **os.environ,
        "MEMORY_PLATFORM_INIT_IMAGE": images["init_image"],
        "MEMORY_PLATFORM_MODEL_IMAGE": images["model_image"],
        "MEMORY_PLATFORM_MEMORY_IMAGE": images["memory_image"],
        "MEMORY_HOST": "127.0.0.1",
        "MEMORY_PORT": "32026",
        "MEMORY_CREDENTIAL_DIR": "./credentials",
        "HOST_UID": "1000",
        "HOST_GID": "1000",
    }
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "deploy" / "docker-compose.user.yml"),
                "--profile",
                "maintenance",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT / "deploy",
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("Docker Compose is unavailable")
    if result.returncode != 0:
        pytest.skip(f"Docker Compose is unavailable: {result.stderr}")
    return json.loads(result.stdout), images


def _validate_rendered(configuration: dict, images: dict[str, str]) -> None:
    _load_compose_validator().validate_compose(
        configuration,
        **images,
        host="127.0.0.1",
        port="32026",
        credential_directory=str(ROOT / "deploy" / "credentials"),
    )


def test_long_lived_services_use_distinct_users_mounts_and_networks():
    compose = _compose()
    services = compose["services"]
    memory = services["memory-gateway"]
    model = services["model-gateway"]
    assert memory["user"] == "10001:10001"
    assert model["user"] == "10002:10002"
    assert memory["read_only"] is True and model["read_only"] is True
    assert memory["cap_drop"] == ["ALL"]
    assert model["cap_drop"] == ["ALL"]
    assert "ports" not in model
    assert memory["ports"] == [
        "${MEMORY_HOST:-127.0.0.1}:${MEMORY_PORT:-2026}:2026"
    ]
    assert compose["networks"]["backend"]["internal"] is True
    assert set(memory["networks"]) == {"backend", "ingress"}
    assert set(model["networks"]) == {"backend", "provider-egress"}
    assert set(compose["networks"]) == {"backend", "ingress", "provider-egress"}
    assert compose["networks"]["ingress"] == {}
    assert compose["networks"]["provider-egress"] == {}
    assert "provider-egress" not in memory["networks"]
    assert "/health" in " ".join(model["healthcheck"]["test"])
    assert "/readyz" not in " ".join(model["healthcheck"]["test"])
    memory_mounts = "\n".join(str(item) for item in memory["volumes"])
    model_mounts = "\n".join(str(item) for item in model["volumes"])
    assert "memory-data" in memory_mounts and "memory-secrets" in memory_mounts
    assert "model-data" not in memory_mounts and "model-secrets" not in memory_mounts
    assert "model-data" in model_mounts and "model-secrets" in model_mounts
    # The private secret store is writable only inside Model Gateway so the
    # atomic channel/key control plane can rotate candidates.  Memory Gateway
    # never mounts it, which is the actual cross-service isolation boundary.
    assert "model-secrets:/secrets:ro" not in model_mounts
    assert "memory-data" not in model_mounts and "memory-secrets" not in model_mounts


def test_signed_compose_runtime_validator_accepts_only_the_split_isolation_contract(
    tmp_path: Path,
):
    rendered, images = _rendered_release_compose()
    _validate_rendered(rendered, images)

    internal_override = tmp_path / "internal.yml"
    internal_override.write_text(
        "services:\n  memory-gateway:\n    ports: !reset []\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "MEMORY_PLATFORM_INIT_IMAGE": images["init_image"],
        "MEMORY_PLATFORM_MODEL_IMAGE": images["model_image"],
        "MEMORY_PLATFORM_MEMORY_IMAGE": images["memory_image"],
        "MEMORY_HOST": "127.0.0.1",
        "MEMORY_PORT": "32026",
        "MEMORY_CREDENTIAL_DIR": "./credentials",
        "HOST_UID": "1000",
        "HOST_GID": "1000",
    }
    internal_result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy" / "docker-compose.user.yml"),
            "-f",
            str(internal_override),
            "--profile",
            "maintenance",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT / "deploy",
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    internal = json.loads(internal_result.stdout)
    assert not internal["services"]["memory-gateway"].get("ports")
    _load_compose_validator().validate_compose(
        internal,
        **images,
        host="127.0.0.1",
        port="32026",
        credential_directory=str(ROOT / "deploy" / "credentials"),
        publish_ingress=False,
    )

    unsafe_variants = []
    published_model = copy.deepcopy(rendered)
    published_model["services"]["model-gateway"]["ports"] = [
        {"target": 2030, "published": "2030", "protocol": "tcp"}
    ]
    unsafe_variants.append(published_model)

    memory_egress = copy.deepcopy(rendered)
    memory_egress["services"]["memory-gateway"]["networks"][
        "provider-egress"
    ] = None
    unsafe_variants.append(memory_egress)

    added_frontend = copy.deepcopy(rendered)
    added_frontend["networks"]["frontend"] = {
        "name": "untrusted_frontend",
        "ipam": {},
    }
    added_frontend["services"]["memory-gateway"]["networks"]["frontend"] = None
    unsafe_variants.append(added_frontend)

    cross_secret_mount = copy.deepcopy(rendered)
    cross_secret_mount["services"]["memory-gateway"]["volumes"].append(
        {"type": "volume", "source": "model-secrets", "target": "/model-secrets"}
    )
    unsafe_variants.append(cross_secret_mount)

    secret_environment = copy.deepcopy(rendered)
    secret_environment["services"]["memory-gateway"]["environment"][
        "GATEWAY_API_KEY"
    ] = "synthetic-canary"
    unsafe_variants.append(secret_environment)

    added_capability = copy.deepcopy(rendered)
    added_capability["services"]["memory-gateway"]["cap_add"] = ["NET_ADMIN"]
    unsafe_variants.append(added_capability)

    init_sys_admin = copy.deepcopy(rendered)
    init_sys_admin["services"]["stack-init"]["cap_add"].append("SYS_ADMIN")
    unsafe_variants.append(init_sys_admin)

    redirected_secret_store = copy.deepcopy(rendered)
    redirected_secret_store["services"]["model-gateway"]["environment"][
        "MODEL_GATEWAY_SECRETS_PATH"
    ] = "/data/secrets.env"
    unsafe_variants.append(redirected_secret_store)

    automatic_maintenance = copy.deepcopy(rendered)
    automatic_maintenance["services"]["stack-maintenance"].pop("profiles")
    unsafe_variants.append(automatic_maintenance)

    exfiltrating_healthcheck = copy.deepcopy(rendered)
    exfiltrating_healthcheck["services"]["model-gateway"]["healthcheck"][
        "test"
    ] = [
        "CMD",
        "python",
        "-c",
        "open('/secrets/secrets.env').read(); marker='http://127.0.0.1:2030/health'",
    ]
    unsafe_variants.append(exfiltrating_healthcheck)

    external_secret_volume = copy.deepcopy(rendered)
    external_secret_volume["volumes"]["model-secrets"] = {
        "name": "unrelated-sensitive-volume",
        "external": True,
    }
    unsafe_variants.append(external_secret_volume)

    inherited_mounts = copy.deepcopy(rendered)
    inherited_mounts["services"]["memory-gateway"]["volumes_from"] = [
        "unrelated-sensitive-container"
    ]
    unsafe_variants.append(inherited_mounts)

    remote_logging = copy.deepcopy(rendered)
    remote_logging["services"]["model-gateway"]["logging"] = {
        "driver": "syslog",
        "options": {"syslog-address": "tcp://collector.example:514"},
    }
    unsafe_variants.append(remote_logging)

    validator = _load_compose_validator()
    for unsafe in unsafe_variants:
        with pytest.raises(ValueError):
            validator.validate_compose(
                unsafe,
                **images,
                host="127.0.0.1",
                port="32026",
                credential_directory=str(ROOT / "deploy" / "credentials"),
            )


def test_init_is_offline_and_only_one_shot_service_sees_all_private_volumes():
    services = _compose()["services"]
    initializer = services["stack-init"]
    assert initializer["network_mode"] == "none"
    assert initializer["restart"] == "no"
    mounts = "\n".join(str(item) for item in initializer["volumes"])
    for name in ("memory-data", "memory-secrets", "model-data", "model-secrets"):
        assert name in mounts
    maintenance = services["stack-maintenance"]
    maintenance_mounts = "\n".join(str(item) for item in maintenance["volumes"])
    assert "model-secrets" not in maintenance_mounts
    assert maintenance["network_mode"] == "none"

    initializer_source = (ROOT / "deploy" / "init_stack.py").read_text()
    assert '"MODEL_GATEWAY_BASE_URL": "http://model-gateway:2030/v1"' in initializer_source
    assert '"MODEL_GATEWAY_ALLOW_PRIVATE_HTTP": "true"' in initializer_source
    # Direct-provider catalog paths are no longer seeded by the installer.


def test_release_compose_and_dockerfile_never_use_main_latest_or_mutable_bases():
    compose_text = (ROOT / "deploy" / "docker-compose.user.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert ":latest" not in compose_text
    assert "/main" not in compose_text
    assert dockerfile.count("@sha256:") >= 3
    assert "-e ./services" not in dockerfile
    assert "pip install --no-deps /wheels/*.whl" in dockerfile
    assert "pip install --require-hashes" in dockerfile
    assert "gosu" not in dockerfile
    assert "deploy/validate_compose.py" in dockerfile
    assert "ingress_relay" not in dockerfile
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for secret_pattern in (
        ".env",
        "**/.env",
        "credentials",
        "**/credentials",
        "*.key",
        "**/*.key",
        "settings.env",
        "**/settings.env",
        "secrets.env",
        "**/secrets.env",
    ):
        assert secret_pattern in dockerignore
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for secret_pattern in (
        "credentials/",
        "**/credentials/",
        "settings.env",
        "**/settings.env",
        "secrets.env",
        "**/secrets.env",
    ):
        assert secret_pattern in gitignore


def test_model_entrypoint_runs_a_single_gateway_process_without_a_relay() -> None:
    entrypoint = (ROOT / "deploy" / "model-entrypoint.sh").read_text()
    assert not (ROOT / "deploy" / "ingress_relay.py").exists()
    assert "ingress_relay" not in entrypoint
    assert "exec modelgw serve" in entrypoint
    assert "trap" not in entrypoint
    assert "wait " not in entrypoint


def test_release_workflow_is_pinned_and_scans_each_split_image():
    workflow = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
    assert "uses: actions/checkout@v" not in workflow
    assert "target: ${{ matrix.target }}" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "cosign sign --yes" in workflow
    assert "severity: HIGH,CRITICAL" in workflow
    assert "exit-code: \"1\"" in workflow
    assert '^v[0-9]+\\.[0-9]+\\.[0-9]+$' in workflow
    assert "needs: [smoke, release-gate]" in workflow
    assert "Test Memory Gateway" in workflow
    assert "Test Model Gateway" in workflow
    assert "Test UI in real Chromium viewports" in workflow
    assert workflow.index("Fail release on fixable HIGH or CRITICAL") < workflow.index(
        "Attest build provenance after release gates"
    )
    assert workflow.index("Attest build provenance after release gates") < workflow.index(
        "Sign promoted immutable image digest"
    )
    assert "publish-compose-signature" in workflow
    assert "cosign sign-blob --yes" in workflow
    assert "docker-compose.user.yml.sigstore.json" in workflow
    assert "Verify split network topology" in workflow


def test_installer_does_not_accept_or_print_secret_values():
    installer = (ROOT / "deploy" / "install.sh").read_text()
    assert "MEMORY_PLATFORM_VERSION" in installer
    assert "memory-platform-init:$RELEASE" in installer
    assert "digest_ref" in installer
    assert "logs --no-log-prefix" not in installer
    assert "GATEWAY_KEY=" not in installer
    assert "ADMIN_KEY=" not in installer
    assert "credentials/gateway.txt" in installer
    assert "credentials/gateway.key" in installer  # legacy fallback
    assert "migrate_legacy.py" in installer
    assert "restore_split.py" in installer
    assert "MEMORY_BACKUP_RETENTION" in installer
    assert "create_quiesced_backup" in installer
    assert "prune_host_backups" in installer
    # 国内可达性：验签默认跳过 + registry 主机可覆盖（digest 固定不变）。
    assert "MEMORY_VERIFY_SIGNATURES" in installer
    assert "MEMORY_IMAGE_REGISTRY" in installer
    assert 'if [ "$LAYOUT" != fresh ]; then' in installer
    assert 'curl -fsS "http://127.0.0.1:$PORT/readyz"' in installer
    assert "OLD_READY" not in installer
    assert "verify-blob" in installer
    assert "--certificate-identity" in installer
    assert "docker.yml@refs/tags/$RELEASE" in installer
    # 完整拓扑校验已移入 CI（validate_compose.py 门禁），安装路径不再内联执行。
    assert "validate_compose.py" in installer
    assert "commit_cutover_journal" in installer
    assert "legacy_targets_absent" in installer
    assert "cleanup_legacy_transaction_volumes" in installer
    assert "Console token:" in installer
    assert "ports: !reset []" in installer
    assert "mark_cutover_committed" in installer
    assert installer.index("mark_cutover_committed") < installer.index(
        'up -d --no-deps --force-recreate memory-gateway'
    )


def test_fresh_init_delivers_one_scoped_console_token_and_disables_legacy(
    tmp_path: Path,
) -> None:
    module = _load_initializer()
    settings_path = tmp_path / "settings.env"
    auth_path = tmp_path / "auth.db"
    credential_path = tmp_path / "credentials" / "gateway.txt"
    credential_path.parent.mkdir(mode=0o700)
    write_env_atomic(
        settings_path,
        {
            "GATEWAY_API_KEY": "fresh-legacy-value-must-be-removed",
            "GATEWAY_LEGACY_API_KEY_ENABLED": "true",
        },
    )

    module._provision_first_console_token(
        settings_path=settings_path,
        auth_database_path=auth_path,
        credential_path=credential_path,
    )

    token = credential_path.read_text(encoding="ascii").strip()
    assert token.startswith("mgw_")
    assert credential_path.stat().st_mode & 0o777 == 0o600
    values = read_env_file(settings_path)
    assert values["GATEWAY_LEGACY_API_KEY_ENABLED"] == "false"
    assert "GATEWAY_API_KEY" not in values
    store = AuthTokenStore(auth_path)
    record = store.authenticate(token)
    assert record is not None
    assert (record.name, record.user_id, record.role) == (
        "first-console",
        "default",
        "console",
    )
    assert len([item for item in store.list_tokens() if item.revoked_at is None]) == 1
    assert token.encode("ascii") not in auth_path.read_bytes()

    # Rerunning before markers are published reuses the exact delivered token
    # instead of silently creating another active console credential.
    module._provision_first_console_token(
        settings_path=settings_path,
        auth_database_path=auth_path,
        credential_path=credential_path,
    )
    assert credential_path.read_text(encoding="ascii").strip() == token


def test_completed_init_repairs_credential_mode_and_missing_file_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_initializer()
    roots = {
        "MEMORY_DATA": tmp_path / "memory-data",
        "MEMORY_SECRETS": tmp_path / "memory-secrets",
        "MODEL_DATA": tmp_path / "model-data",
        "MODEL_SECRETS": tmp_path / "model-secrets",
        "CREDENTIALS": tmp_path / "credentials",
    }
    for name, path in roots.items():
        path.mkdir(mode=0o700)
        monkeypatch.setattr(module, name, path)
    monkeypatch.setattr(module, "MEMORY_MARKER", roots["MEMORY_DATA"] / ".stack-installed-v2")
    monkeypatch.setattr(module, "MODEL_MARKER", roots["MODEL_DATA"] / ".stack-installed-v2")
    monkeypatch.setattr(module.os, "chown", lambda *_args: None)
    module.MEMORY_MARKER.write_text("a" * 32 + "\n", encoding="ascii")
    module.MODEL_MARKER.write_text("a" * 32 + "\n", encoding="ascii")
    for name in ("gateway.txt", "admin.txt"):
        path = roots["CREDENTIALS"] / name
        path.write_text(f"synthetic-{name}\n", encoding="ascii")
        path.chmod(0o644)

    assert module.main() == 0
    assert all(
        (roots["CREDENTIALS"] / name).stat().st_mode & 0o777 == 0o600
        for name in ("gateway.txt", "admin.txt")
    )

    (roots["CREDENTIALS"] / "gateway.txt").unlink()
    with pytest.raises(RuntimeError, match="缺少 gateway 凭据文件"):
        module.main()

    legacy_gateway = roots["CREDENTIALS"] / "gateway.key"
    legacy_gateway.write_text("synthetic-legacy-gateway\n", encoding="ascii")
    legacy_gateway.chmod(0o644)
    assert module.main() == 0
    assert legacy_gateway.stat().st_mode & 0o777 == 0o600


def test_initializer_rejects_symlinked_or_hardlinked_credential(tmp_path):
    module = _load_initializer()
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("synthetic-outside-value\n", encoding="ascii")
    outside.chmod(0o640)
    symlink = credential_directory / "gateway.key"
    symlink.symlink_to(outside)

    with pytest.raises(RuntimeError, match="unsafe"):
        module._deliver_once(symlink, "synthetic-outside-value")
    assert outside.read_text(encoding="ascii") == "synthetic-outside-value\n"
    assert outside.stat().st_mode & 0o777 == 0o640

    symlink.unlink()
    hardlink = credential_directory / "gateway.key"
    hardlink.hardlink_to(outside)
    with pytest.raises(RuntimeError, match="unsafe"):
        module._deliver_once(hardlink, "synthetic-outside-value")
    assert outside.read_text(encoding="ascii") == "synthetic-outside-value\n"
    assert outside.stat().st_mode & 0o777 == 0o640


def test_split_restore_helper_tracks_complete_cli_paths(tmp_path, monkeypatch):
    module = _load_restore_helper()
    memory_data = tmp_path / "memory-data"
    memory_data.mkdir()
    memory_settings = tmp_path / "memory-secrets" / "settings.env"
    memory_settings.parent.mkdir()
    model_data = tmp_path / "model-data"
    model_data.mkdir()
    captured = {}

    monkeypatch.setattr(module, "MEMORY_DATA", memory_data)
    monkeypatch.setattr(module, "MEMORY_SETTINGS", memory_settings)
    monkeypatch.setattr(module, "MODEL_DATA", model_data)
    monkeypatch.setattr(module, "ARCHIVE", memory_data / "restore.zip")
    monkeypatch.setattr(module, "_secure_tree", lambda *_args: None)

    def fake_restore(**kwargs):
        captured.update(kwargs)
        return {"restored": ["memory/memory.db"]}

    monkeypatch.setattr(module, "restore_stack_backup", fake_restore)
    assert module.main() == 0
    paths = captured["paths"]
    assert paths.home == memory_data / "config"
    assert paths.credentials == memory_data / "config" / "credentials"
    assert paths.settings_env == memory_settings
