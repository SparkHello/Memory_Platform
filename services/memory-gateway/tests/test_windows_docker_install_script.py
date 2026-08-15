from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INSTALLER = ROOT / "deploy" / "install.ps1"


def _installer() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_windows_installer_has_utf8_bom_for_powershell_51() -> None:
    assert INSTALLER.read_bytes().startswith(b"\xef\xbb\xbf")


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
    assert "Test-CandidateComposeSyntax" in text
    assert "Test-RenderedCandidateTopology" in text
    assert "config --format json" in text


def test_windows_installer_never_accepts_or_recovers_secret_values_from_logs() -> None:
    text = _installer()

    assert "不接受环境变量中的密钥" in text
    assert '@("GATEWAY_API_KEY", "MEMORY_CONSOLE_ADMIN_KEY")' in text
    assert "Test-ComposeEnvKey" in text
    assert "Update-ComposeEnvironment" in text
    assert "function Resolve-CredentialFile" in text
    assert 'foreach ($extension in @("txt", "key"))' in text
    assert 'Resolve-CredentialFile $credentialDirectory "gateway"' in text
    assert 'Resolve-CredentialFile $credentialDirectory "admin"' in text
    assert "Protect-PrivatePath $credential" in text
    assert "Find-GeneratedKey" not in text
    assert "logs --no-log-prefix" not in text
    assert "generatedGatewayKey" not in text
    assert "generatedAdminKey" not in text
    assert "Get-Content" not in text


def test_windows_private_acl_rewrite_is_idempotent_without_security_privilege() -> None:
    text = _installer()

    assert "$item.SetAccessControl($acl)" in text
    assert '"System.IO.FileSystemAclExtensions" -as [type]' in text
    assert ".PSObject.BaseObject" in text
    assert "Set-Acl -LiteralPath $Path" not in text


def test_windows_installer_suppresses_native_stderr_without_powershell_51_abort() -> None:
    text = _installer()

    assert "function Invoke-NativeCapture" in text
    assert "function Invoke-NativeSilently" in text
    assert '$ErrorActionPreference = "Continue"' in text
    # The helper owns the sole redirected native invocation. Call sites must
    # not redirect Docker stderr while the script-wide preference is Stop.
    assert text.count("2>$null") == 1
    assert "*> $null" not in text


def test_windows_upgrade_backs_up_before_candidate_replacement_and_cleans_temp() -> None:
    text = _installer()

    snapshot = text.index('Write-Step "保存旧 Compose 快照"')
    download = text.index('Write-Step "下载 $release Compose 并校验"')
    plan = text.index('Write-Step "生成 typed 安装计划"', download)
    quiesced = text.index('Write-Step "旧服务已停写，创建并复验最终一致性备份"')
    replace = text.index(
        "Replace-ComposeAtomically $script:CandidateCompose $script:ComposePath",
        download,
    )
    assert download < plan < snapshot < quiesced < replace
    assert '"stack", "backup", "--model-gateway-home", "/model-data"' in text
    assert "docker cp" in text
    # 每次升级恰好一份停写一致性备份，并调用权威便携包校验器复验。
    assert "Test-BackupArchive" in text
    assert "/usr/local/libexec/memory-platform/verify_backup.py" in text
    assert "archive.testzip()" not in text
    assert "PRAGMA quick_check" not in text
    assert "$verifyImage = $script:InitImage" in text
    assert "import os,sys; os.unlink(sys.argv[1])" in text
    assert "pre-upgrade-$stamp.compose.yml" in text
    assert "MEMORY_BACKUP_RETENTION" in text
    assert "Remove-StaleHostBackups" in text
    assert "Select-Object -Skip $Retention" in text
    create_backup = text.index("if (-not (New-QuiescedBackup))")
    prune = text.index(
        "Remove-StaleHostBackups $backupDirectory $backupRetention"
    )
    assert create_backup < prune


def test_windows_legacy_layout_is_referred_to_standalone_cutover_tool() -> None:
    text = _installer()

    # 旧单卷一次性迁移已拆分为 deploy/legacy_cutover.py；install.ps1 检测到
    # legacy 布局时 fail-closed 并给出迁移工具命令，不再内嵌迁移。
    assert "migrate_legacy.py" not in text
    assert "backup_legacy.py" not in text
    assert "legacy_cutover.py" in text
    assert "检测到旧单卷（legacy）布局" in text
    assert "Invoke-Rollback" in text
    assert "/usr/local/libexec/memory-platform/restore_split.py" in text
    assert "Restore-ComposeEnvironmentSnapshot" in text
    assert "Get-ServiceImageId" in text
    assert '$script:RollbackInitImage = Get-ServiceImageId $script:ComposePath "stack-init"' in text
    assert '"--entrypoint", "python", $restoreImage' in text
    assert "up -d --pull never" in text
    assert "WSL/手工迁移" in text
    assert "拒绝覆盖" in text


def test_windows_acceptance_keeps_model_private_and_checks_health_regression() -> None:
    text = _installer()

    # 意外发布宿主端口的检查改为查 Docker 端口映射，而非宿主 curl。
    assert "docker port $candidateId" in text
    assert "invalid IP:0" in text
    assert "$candidatePublished" not in text
    assert "意外发布宿主端口" in text
    assert "Model Gateway 2030 仅位于 Docker 内部网络" in text
    assert 'Wait-HttpEndpoint "http://${hostProbe}:$port/health" 180' in text
    assert 'Wait-HttpEndpoint "http://${hostProbe}:$port/readyz" 90' in text
    assert "Get-HostProbeAddress" in text
    assert "Get-ExistingServiceReadiness" in text
    assert '$oldMemoryReadiness = "absent"' in text
    assert '$oldModelReadiness = "absent"' in text
    assert '$oldMemoryReadiness -eq "unknown"' in text
    assert '$oldModelReadiness -eq "unknown"' in text
    assert "$installPlan.AcceptMemoryReadiness" in text
    assert "$installPlan.AcceptModelReadiness" in text
    assert "$installPlan.AcceptHostReadiness" in text
    assert "readiness 退化" in text
    assert "2030 仅位于 Docker 内部网络" in text
    assert "ports: !reset []" in text
    assert "Test-RenderedCandidateTopology" in text
    assert "Wait-CandidateContainerHttp" in text
    assert '"model-gateway"; Url = "http://127.0.0.1:2030/health"' in text
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


def test_ci_runs_windows_installer_contract_on_both_powershell_engines() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    journal = (
        ROOT
        / "services"
        / "memory-gateway"
        / "tests"
        / "windows_installer_journal.ps1"
    ).read_text(encoding="utf-8")

    assert "windows-installer:" in workflow
    assert "runs-on: windows-latest" in workflow
    assert "shell: powershell" in workflow
    assert "shell: pwsh" in workflow
    assert workflow.count("test_installer_parity.py") == 2
    assert workflow.count("test_windows_docker_install_script.py") == 2
    assert workflow.count("windows_installer_journal.ps1") == 2
    # Construct non-ASCII names from code points: this remains real Chinese
    # when Windows PowerShell 5.1 reads the UTF-8-without-BOM harness as ANSI.
    assert "[char]0x4E2D" in journal and "[char]0x6587" in journal
    assert "fixture path with spaces" in journal
    assert "[char]0x5907" in journal and "[char]0x4EFD" in journal
    assert "retention corpus" in journal


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
    assert "Test-ReleaseComposeSignature" in text
    # 验签默认跳过（镜像仍按不可变 digest 固定），MEMORY_VERIFY_SIGNATURES=1 显式开启。
    assert "MEMORY_VERIFY_SIGNATURES" in text
    assert "if ($verifySignatures)" in text
    assert "已按默认跳过 Sigstore 签名验证" in text
    # 安装时由候选 init 镜像中的同一 validator 校验 public/internal 渲染结果。
    assert "validate_compose.py" in text


def test_windows_candidate_validator_is_offline_shared_and_pre_cutover() -> None:
    text = _installer()

    validation = text.index('Write-Step "用候选 init 镜像校验 public/internal 安全拓扑"')
    journal = text.index("New-CutoverJournal", validation)
    stop = text.index(" stop", journal)
    assert validation < journal < stop
    assert text.count("Test-RenderedCandidateTopology `") == 2
    assert '"--network", "none"' in text
    assert '"--read-only", "--cap-drop", "ALL"' in text
    assert '"--security-opt", "no-new-privileges:true"' in text
    assert '"--user", "65534:65534"' in text
    assert '"--entrypoint", "python"' in text
    assert 'if (-not $PublishIngress) { $arguments += "internal" }' in text
    assert "/var/run/docker.sock" not in text
    assert "--mount" not in text[text.index("function Test-RenderedCandidateTopology"):text.index("function Restore-ImageEnvironment")]


def test_windows_typed_plan_separates_noop_repair_and_upgrade() -> None:
    text = _installer()

    assert "/usr/local/libexec/memory-platform/plan_install.py" in text
    assert "function Get-InstallPlan" in text
    assert "Version = 1" in text
    assert 'Action = [string] $fields[1]' in text
    assert 'RepairScope = [string] $fields[3]' in text
    assert "AcceptMemoryReadiness" in text
    assert "AcceptModelReadiness" in text
    assert "AcceptHostReadiness" in text
    noop = text.index('if ($installPlan.Action -eq "noop")')
    repair = text.index('if ($installPlan.Action -eq "repair")', noop)
    snapshot = text.index('Write-Step "保存旧 Compose 快照"', repair)
    journal = text.index("New-CutoverJournal", snapshot)
    assert noop < repair < snapshot < journal
    pre_upgrade = text[noop:snapshot]
    assert "New-CutoverJournal" not in pre_upgrade
    assert "New-QuiescedBackup" not in pre_upgrade
    assert " stop" not in pre_upgrade
    repair_function = text[
        text.index("function Invoke-ExistingInstallPlan"):
        text.index("function Get-FirstLanIp")
    ]
    assert "--no-deps --force-recreate model-gateway" in repair_function
    assert "--no-deps --force-recreate memory-gateway" in repair_function
    assert "New-QuiescedBackup" not in repair_function
    assert " stop" not in repair_function
    planner_function = text[
        text.index("function Get-InstallPlan"):
        text.index("function Get-ExistingInstallDirectories")
    ]
    assert '"--network", "none"' in planner_function
    assert '"--read-only", "--cap-drop", "ALL"' in planner_function
    assert "--mount" not in planner_function


def test_windows_host_probe_uses_specific_bind_and_maps_wildcard_to_loopback() -> None:
    text = _installer()

    helper = text[
        text.index("function Get-HostProbeAddress"):
        text.index("function Test-HttpEndpoint")
    ]
    assert 'if ($Address -eq "0.0.0.0") { return "127.0.0.1" }' in helper
    assert "return $Address" in helper
    assert 'http://${hostProbe}:$port/health' in text
    assert 'http://${publishProbeHost}:$publishPort/health' in text


def test_windows_cutover_journal_is_durable_one_way_and_retry_safe() -> None:
    text = _installer()

    assert "MoveFileExW" in text
    assert "MOVEFILE_WRITE_THROUGH" in text
    assert "[IO.File]::Replace" not in text
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
    # 允许 MEMORY_IMAGE_REGISTRY 覆盖 registry 主机，仓库路径保持固定。
    assert "sparkhello/memory-platform-init" in text
    # 旧版安装器留下的 legacy 中断 journal 不再就地恢复，fail-closed 指向
    # 独立迁移工具；相应的卷清理助手已随内嵌迁移一起删除。
    assert "legacy_targets_absent" not in text
    assert "Test-LegacyTargetVolumeExists" not in text
    assert "Remove-LegacyTransactionVolumes" not in text
    assert "升级事务 journal 来自旧版安装器的 legacy 迁移" in text
    assert "post-commit" not in text or "must never trigger" in text

    dynamic_contract = (
        ROOT / "services" / "memory-gateway" / "tests" /
        "windows_installer_journal.ps1"
    ).read_text(encoding="utf-8")
    assert "committed-acl-failure" in dynamic_contract
    assert "Remove-LegacyTransactionVolumes" not in dynamic_contract


def test_windows_installer_pins_existing_project_and_candidate_environment() -> None:
    text = _installer()

    assert "Get-ProjectsForInstallDirectory" in text
    assert "com.docker.compose.project.working_dir" in text
    assert "--format '{{json .Labels}}'" in text
    assert '{{.Label "com.docker.compose.project.working_dir"}}' not in text
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


def test_windows_installer_does_not_embed_legacy_backup() -> None:
    # 旧单卷只读备份编排迁移到 deploy/legacy_cutover.py，其只读/无网络契约由
    # tests/test_legacy_cutover.py 覆盖；安装器自身不再引用 backup_legacy.py。
    text = _installer()

    assert "backup_legacy.py" not in text
    assert "target=/legacy,readonly" not in text
