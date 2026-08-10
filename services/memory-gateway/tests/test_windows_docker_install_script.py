from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INSTALLER = ROOT / "deploy" / "install.ps1"


def _installer() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_windows_installer_uses_a_fixed_release_and_three_digest_images() -> None:
    text = _installer()

    assert "/main/" not in text
    assert "MEMORY_PLATFORM_VERSION" in text
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in text
    assert (
        'https://raw.githubusercontent.com/SparkHello/Memory_Platform/$release'
        in text
    )
    for image in (
        "memory-platform-init:$release",
        "memory-platform-model:$release",
        "memory-platform-memory:$release",
    ):
        assert image in text
    assert text.count("Resolve-ImageDigest") >= 4
    assert "@sha256:[0-9a-f]{64}" in text
    assert "Test-CandidateCompose" in text
    assert "config --format json" in text


def test_windows_installer_never_accepts_or_recovers_secret_values_from_logs() -> None:
    text = _installer()

    assert "不接受环境变量中的密钥" in text
    assert '@("GATEWAY_API_KEY", "MEMORY_CONSOLE_ADMIN_KEY")' in text
    assert "Test-ComposeEnvKey" in text
    assert "Update-ComposeEnvironment" in text
    assert 'Join-Path $credentialDirectory "gateway.key"' in text
    assert 'Join-Path $credentialDirectory "admin.key"' in text
    assert "Protect-PrivatePath $credential" in text
    assert "Find-GeneratedKey" not in text
    assert "logs --no-log-prefix" not in text
    assert "generatedGatewayKey" not in text
    assert "generatedAdminKey" not in text
    assert "Get-Content" not in text


def test_windows_upgrade_backs_up_before_candidate_replacement_and_cleans_temp() -> None:
    text = _installer()

    backup = text.index('Write-Step "准备升级前备份"')
    download = text.index('Write-Step "下载 $release Compose 并校验"')
    replace = text.index(
        "Replace-ComposeAtomically $script:CandidateCompose $script:ComposePath",
        download,
    )
    assert backup < download < replace
    assert "stack backup --model-gateway-home /model-data" in text
    assert "docker cp" in text
    assert "Remove-VolumeBackup" in text
    assert "unlink(missing_ok=True)" in text
    assert "pre-upgrade-$stamp.compose.yml" in text
    assert "MEMORY_BACKUP_RETENTION" in text
    assert "Remove-StaleHostBackups" in text
    assert "Select-Object -Skip $Retention" in text


def test_windows_legacy_migration_is_offline_and_fail_closed_with_rollback() -> None:
    text = _installer()

    migration = text.index(
        "/usr/local/libexec/memory-platform/migrate_legacy.py"
    )
    assert '"--network", "none", "--read-only"' in text[:migration]
    assert "target=/legacy,readonly" in text[:migration]
    for destination in (
        "/memory-data",
        "/memory-secrets",
        "/model-data",
        "/model-secrets",
    ):
        assert destination in text[:migration]
    assert "Invoke-Rollback" in text
    assert "/usr/local/libexec/memory-platform/restore_split.py" in text
    assert "Restore-ComposeEnvironmentSnapshot" in text
    assert "Get-ServiceImageId" in text
    assert '$script:RollbackInitImage = Get-ServiceImageId $script:ComposePath "stack-init"' in text
    assert '"--entrypoint", "python", $restoreImage' in text
    assert "up -d --pull never" in text
    assert "WSL/手工迁移" in text
    assert "拒绝覆盖不明状态" in text


def test_windows_acceptance_keeps_model_private_and_checks_health_regression() -> None:
    text = _installer()

    assert 'Get-JsonPropertyValue $privateService "ports"' in text
    assert '$ingressPorts = @(Get-JsonPropertyValue $modelService "ports")' in text
    assert "Model Gateway 2030 仅位于 Docker 内部网络" in text
    assert 'Wait-HttpEndpoint "http://127.0.0.1:$port/health" 180' in text
    assert 'Wait-HttpEndpoint "http://127.0.0.1:$port/readyz" 90' in text
    assert '$script:Layout -ne "fresh" -and' in text
    assert "$oldReady" not in text
    assert "readiness 退化" in text
    assert "2030 仅位于 Docker 内部网络" in text
    assert "ports: !reset []" in text
    assert "Test-SignedInternalComposeRuntime" in text
    assert "Wait-CandidateContainerHttp" in text
    assert '"model-gateway"; Url = "http://127.0.0.1:2026/health"' in text
    assert "Test-CandidateCredential" in text
    internal_start = text.index('Write-Step "在无宿主发布端口的隔离模式启动候选服务"')
    commit = text.index("Mark-CutoverCommitted", internal_start)
    publish = text.index('Write-Step "发布已验收的 Memory 入口"', commit)
    assert internal_start < commit < publish


def test_windows_installer_avoids_powershell_7_only_control_operators() -> None:
    text = _installer()

    # The release installer promises Windows PowerShell 5.1 support. These
    # control operators and language features would silently narrow it to 7+.
    assert " && " not in text
    assert " || " not in text
    assert "??" not in text
    assert "ForEach-Object -Parallel" not in text


def test_windows_release_authentication_covers_compose_and_all_images() -> None:
    text = _installer()

    assert "COSIGN_VERSION" not in text or "v3.0.6" in text
    assert "cosign-windows-amd64.exe" in text
    assert "9b85a88e" in text
    assert "verify-blob" in text
    assert ".sigstore.json" in text
    assert "--certificate-identity" in text
    assert "docker.yml@refs/tags/$Release" in text
    assert "https://token.actions.githubusercontent.com" in text
    assert text.count("Test-ReleaseSignature") >= 4
    assert "Test-SignedComposeRuntime" in text
    assert "/usr/local/libexec/memory-platform/validate_compose.py" in text


def test_windows_cutover_journal_is_durable_one_way_and_legacy_retry_safe() -> None:
    text = _installer()

    assert "MoveFileExW" in text
    assert "MOVEFILE_WRITE_THROUGH" in text
    assert "Write-DurableTextAtomic" in text
    assert '"committed`n"' in text
    assert "CutoverCommittedCleanup" in text
    assert "Remove-CommittedCutoverTombstone" in text
    assert "Complete-CutoverJournal" in text
    assert "version = 2" in text
    assert '$version -notin @(1, 2)' in text
    assert "old_env_exists" in text
    assert "publish_host" in text
    assert "publish_port" in text
    assert "Test-ImmutableOldImageReference" in text
    assert "ghcr.io/sparkhello/memory-platform-init" in text
    assert "legacy_targets_absent" in text
    assert "Test-LegacyTargetVolumeExists" in text
    assert "Remove-LegacyTransactionVolumes" in text
    assert "docker volume rm" in text
    assert "post-commit" not in text or "must never trigger" in text

    dynamic_contract = (
        ROOT / "services" / "memory-gateway" / "tests" /
        "windows_installer_journal.ps1"
    ).read_text(encoding="utf-8")
    assert "committed-acl-failure" in dynamic_contract
    assert "Remove-LegacyTransactionVolumes" in dynamic_contract


def test_windows_installer_pins_existing_project_and_candidate_environment() -> None:
    text = _installer()

    assert "Get-ProjectsForInstallDirectory" in text
    assert "com.docker.compose.project.working_dir" in text
    assert "与旧容器 project 身份冲突" in text
    assert "现有 Compose 没有同 project 的容器或数据卷" in text
    for name in (
        "COMPOSE_ENV_FILES",
        "COMPOSE_DISABLE_ENV_FILE",
        "COMPOSE_PROFILES",
        "COMPOSE_FILE",
        "COMPOSE_PATH_SEPARATOR",
    ):
        assert name in text
    assert '"COMPOSE_PROJECT_NAME" = $script:ProjectName' in text


def test_windows_legacy_backup_supports_missing_auth_without_mutating_source() -> None:
    text = _installer()

    assert "/usr/local/libexec/memory-platform/backup_legacy.py" in text
    assert "target=/legacy,readonly" in text
    assert "target=/backup" in text
    assert "/scratch:rw,noexec,nosuid" in text
