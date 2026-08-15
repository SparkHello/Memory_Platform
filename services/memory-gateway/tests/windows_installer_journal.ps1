$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../../.."))
$installer = Join-Path $repository "deploy/install.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $installer,
    [ref] $tokens,
    [ref] $errors
)
if ($errors.Count -gt 0) { throw "install.ps1 did not parse" }
$definitions = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true) | ForEach-Object { $_.Extent.Text })
$temporaryBase = Join-Path ([IO.Path]::GetTempPath()) `
    ("memory-platform-windows-journal-" + [Guid]::NewGuid().ToString("N"))
$chineseFixtureName = (([string][char]0x4E2D) + [char]0x6587 +
    " fixture path with spaces")
$temporaryRoot = Join-Path $temporaryBase $chineseFixtureName
New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
$functionsPath = Join-Path $temporaryRoot "installer-functions.ps1"
[IO.File]::WriteAllText(
    $functionsPath,
    ([string]::Join("`n`n", $definitions)),
    # Windows PowerShell 5.1 treats UTF-8 without a BOM as the active ANSI
    # code page.  The extracted functions contain Chinese diagnostics whose
    # quote characters must survive the round trip before dot-sourcing.
    (New-Object Text.UTF8Encoding($true))
)
. $functionsPath

$runningOnWindows = [Environment]::OSVersion.Platform -eq `
    [PlatformID]::Win32NT
if ($runningOnWindows) {
    $aclFileProbe = Join-Path $temporaryRoot "private ACL file"
    $aclDirectoryProbe = Join-Path $temporaryRoot "private ACL directory"
    [IO.File]::WriteAllText($aclFileProbe, "synthetic")
    New-Item -ItemType Directory -Path $aclDirectoryProbe | Out-Null
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    foreach ($aclProbe in @($aclFileProbe, $aclDirectoryProbe)) {
        Protect-PrivatePath $aclProbe
        Protect-PrivatePath $aclProbe
        $protectedAcl = Get-Acl -LiteralPath $aclProbe
        if (-not $protectedAcl.AreAccessRulesProtected -or
            @($protectedAcl.Access).Count -ne 1 -or
            $protectedAcl.Access[0].IdentityReference.Value -ne $currentIdentity -or
            $protectedAcl.Access[0].FileSystemRights -ne `
                [Security.AccessControl.FileSystemRights]::FullControl) {
            throw "private ACL was not restricted to the current Windows user"
        }
    }
    $lockProbe = Join-Path $temporaryRoot "installer-lock-probe"
    Acquire-InstallerLock $lockProbe
    Release-InstallerLock
    Acquire-InstallerLock $lockProbe
    Release-InstallerLock
}

# Native MoveFileEx is Windows-only. The journal state machine itself is
# exercised cross-platform with an atomic same-filesystem move substitute;
# Windows CI separately parses and runs the production P/Invoke path.
function Move-PathWriteThrough([string] $Source, [string] $Destination) {
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    $item = Get-Item -LiteralPath $Source -Force
    if ($item.PSIsContainer) {
        [IO.Directory]::Move($Source, $Destination)
    } else {
        [IO.File]::Move($Source, $Destination)
    }
}
function Protect-PrivatePath([string] $Path) { }
function Write-Step([string] $Message) { }
function Stop-Install([string] $Message) { throw $Message }
function Write-DurableTextAtomic([string] $Path, [string] $Content) {
    [IO.File]::WriteAllText(
        $Path,
        $Content,
        (New-Object Text.UTF8Encoding($false))
    )
}

function Assert-True([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}

try {
    Assert-True ($temporaryRoot.Contains((([string][char]0x4E2D) + [char]0x6587))) `
        "fixture root did not retain the intended Chinese path component"
    $retentionCorpusName = (([string][char]0x5907) + [char]0x4EFD +
        " retention corpus")
    $retentionDirectory = Join-Path $temporaryRoot $retentionCorpusName
    New-Item -ItemType Directory -Path $retentionDirectory | Out-Null
    foreach ($index in 1..4) {
        $stem = "pre-upgrade-2026010${index}T000000Z-test"
        [IO.File]::WriteAllText(
            (Join-Path $retentionDirectory "$stem.zip"),
            "backup-$index"
        )
        [IO.File]::WriteAllText(
            (Join-Path $retentionDirectory "$stem.compose.yml"),
            "compose-$index"
        )
    }
    Remove-StaleHostBackups $retentionDirectory 2
    $retainedArchives = @(Get-ChildItem -LiteralPath $retentionDirectory `
        -File -Filter "pre-upgrade-*.zip" | Sort-Object Name)
    Assert-True ($retainedArchives.Count -eq 2) `
        "retention did not keep exactly N archives"
    Assert-True `
        ($retainedArchives[0].Name -match "20260103" -and
         $retainedArchives[1].Name -match "20260104") `
        "retention did not keep the newest archives"
    Assert-True `
        (-not (Test-Path -LiteralPath (Join-Path $retentionDirectory `
            "pre-upgrade-20260101T000000Z-test.compose.yml"))) `
        "retention left the stale compose sidecar"
    Assert-True `
        (Test-Path -LiteralPath (Join-Path $retentionDirectory `
            "pre-upgrade-20260104T000000Z-test.compose.yml")) `
        "retention removed a retained compose sidecar"

    if ($runningOnWindows) {
        $nativeSuccess = Invoke-NativeSilently {
            & cmd.exe /d /c 'echo normal-progress 1>&2 & exit /b 0'
        }
        $nativeFailure = Invoke-NativeSilently {
            & cmd.exe /d /c 'echo expected-failure 1>&2 & exit /b 7'
        }
    } else {
        $nativeSuccess = Invoke-NativeSilently {
            & sh -c 'echo normal-progress >&2; exit 0'
        }
        $nativeFailure = Invoke-NativeSilently {
            & sh -c 'echo expected-failure >&2; exit 7'
        }
    }
    Assert-True ($nativeSuccess -eq 0) `
        "native stderr converted a successful command into an installer failure"
    Assert-True ($nativeFailure -eq 7) `
        "native command exit status was not preserved"

    if (-not $runningOnWindows) {
        $nativeBin = Join-Path $temporaryRoot "native-bin"
        New-Item -ItemType Directory -Path $nativeBin | Out-Null
        $fakeDocker = Join-Path $nativeBin "docker"
        $fakeDockerScript = @'
#!/bin/sh
[ "$1" = alpha ] || exit 2
[ "$2" = "path with spaces" ] || exit 3
IFS= read -r value
[ "$value" = synthetic-input ] || exit 4
'@
        [IO.File]::WriteAllText(
            $fakeDocker,
            $fakeDockerScript,
            (New-Object Text.UTF8Encoding($false))
        )
        & chmod 700 $fakeDocker
        $inputFile = Join-Path $temporaryRoot "synthetic-input"
        [IO.File]::WriteAllText($inputFile, "synthetic-input`n")
        $savedPath = $env:PATH
        $env:PATH = "$nativeBin$([IO.Path]::PathSeparator)$savedPath"
        try {
            Assert-True `
                (Invoke-DockerWithInputFile @("alpha", "path with spaces") $inputFile) `
                "credential stdin helper did not preserve native arguments/input"
        } finally {
            $env:PATH = $savedPath
        }
    }

    foreach ($shape in @("complete", "phase-only", "empty")) {
        $case = Join-Path $temporaryRoot $shape
        New-Item -ItemType Directory -Path $case | Out-Null
        $script:InstallDirectory = $case
        $script:ComposePath = Join-Path $case "docker-compose.user.yml"
        $script:CutoverJournal = Join-Path $case ".memory-platform-cutover"
        $script:CutoverCommittedCleanup = "$($script:CutoverJournal).committed-cleanup"
        $script:Layout = "split"
        New-Item -ItemType Directory -Path $script:CutoverJournal | Out-Null
        if ($shape -ne "empty") {
            [IO.File]::WriteAllText(
                (Join-Path $script:CutoverJournal "phase.txt"),
                "committed`n"
            )
        }
        if ($shape -eq "complete") {
            $digest = "sha256:" + ("a" * 64)
            [IO.File]::WriteAllText(
                (Join-Path $script:CutoverJournal "metadata.json"),
                (@{
                    version = 1
                    project = "journal-project"
                    layout = "split"
                    backup = "pre-upgrade-test.zip"
                    old_init_image = $digest
                    old_model_image = $digest
                    old_memory_image = $digest
                    legacy_targets_absent = $false
                } | ConvertTo-Json -Compress)
            )
            [IO.File]::WriteAllText(
                (Join-Path $script:CutoverJournal "old-compose.yml"),
                "synthetic"
            )
            [IO.File]::WriteAllText(
                (Join-Path $script:CutoverJournal "old.env"),
                "synthetic"
            )
        }
        $environmentPath = Join-Path $case ".env"
        [IO.File]::WriteAllText($environmentPath, "ACCEPTED_NEW_STATE=1`n")
        Restore-InterruptedCutover $environmentPath
        Assert-True (-not (Test-Path -LiteralPath $script:CutoverJournal)) `
            "committed journal shape $shape was not cleaned"
        Assert-True `
            ([IO.File]::ReadAllText($environmentPath) -eq "ACCEPTED_NEW_STATE=1`n") `
            "committed journal shape $shape rolled back accepted state"
    }

    $digest = "sha256:" + ("b" * 64)
    $committedV2 = Join-Path $temporaryRoot "committed-v2-publish"
    New-Item -ItemType Directory -Path $committedV2 | Out-Null
    $script:InstallDirectory = $committedV2
    $script:ComposePath = Join-Path $committedV2 "docker-compose.user.yml"
    $script:CutoverJournal = Join-Path $committedV2 ".memory-platform-cutover"
    $script:CutoverCommittedCleanup = "$($script:CutoverJournal).committed-cleanup"
    New-Item -ItemType Directory -Path $script:CutoverJournal | Out-Null
    [IO.File]::WriteAllText($script:ComposePath, "accepted-compose`n")
    $environmentPath = Join-Path $committedV2 ".env"
    [IO.File]::WriteAllText(
        $environmentPath,
        "COMPOSE_PROJECT_NAME=journal-project`nMEMORY_HOST=127.0.0.1`nMEMORY_PORT=3026`n"
    )
    [IO.File]::WriteAllText(
        (Join-Path $script:CutoverJournal "metadata.json"),
        (@{
            version = 2
            project = "journal-project"
            layout = "split"
            backup = "pre-upgrade-v2.zip"
            old_init_image = $digest
            old_model_image = $digest
            old_memory_image = $digest
            legacy_targets_absent = $false
            old_env_exists = $true
            publish_host = "127.0.0.1"
            publish_port = 3026
        } | ConvertTo-Json -Compress)
    )
    [IO.File]::WriteAllText(
        (Join-Path $script:CutoverJournal "old-compose.yml"), "old-compose`n"
    )
    [IO.File]::WriteAllText(
        (Join-Path $script:CutoverJournal "old.env"), "OLD=1`n"
    )
    [IO.File]::WriteAllText(
        (Join-Path $script:CutoverJournal "phase.txt"), "committed`n"
    )
    $script:CommittedPublishCalled = $false
    function docker {
        $Arguments = @($args)
        $global:LASTEXITCODE = 0
        if ($Arguments -contains "up") { $script:CommittedPublishCalled = $true }
    }
    function Wait-HttpEndpoint([string] $Url, [int] $Attempts) { return $true }
    Restore-InterruptedCutover $environmentPath
    Assert-True $script:CommittedPublishCalled `
        "v2 committed recovery did not finish publishing the accepted stack"
    Assert-True (-not (Test-Path -LiteralPath $script:CutoverJournal)) `
        "v2 committed recovery did not clean the journal"
    Assert-True `
        ([IO.File]::ReadAllText($environmentPath) -match "MEMORY_PORT=3026") `
        "v2 committed recovery changed the accepted environment"

    $preparedV2 = Join-Path $temporaryRoot "prepared-v2-no-old-env"
    New-Item -ItemType Directory -Path $preparedV2 | Out-Null
    $script:InstallDirectory = $preparedV2
    $script:ComposePath = Join-Path $preparedV2 "docker-compose.user.yml"
    $script:CutoverJournal = Join-Path $preparedV2 ".memory-platform-cutover"
    $script:CutoverCommittedCleanup = "$($script:CutoverJournal).committed-cleanup"
    New-Item -ItemType Directory -Path $script:CutoverJournal | Out-Null
    [IO.File]::WriteAllText($script:ComposePath, "candidate-compose`n")
    $environmentPath = Join-Path $preparedV2 ".env"
    [IO.File]::WriteAllText($environmentPath, "CANDIDATE=1`n")
    New-Item -ItemType Directory -Path (Join-Path $preparedV2 "backups") | Out-Null
    [IO.File]::WriteAllText(
        (Join-Path $preparedV2 "backups/pre-upgrade-v2.zip"), "backup"
    )
    [IO.File]::WriteAllText(
        (Join-Path $script:CutoverJournal "metadata.json"),
        (@{
            version = 2
            project = "journal-project"
            layout = "split"
            backup = "pre-upgrade-v2.zip"
            old_init_image = $digest
            old_model_image = $digest
            old_memory_image = $digest
            legacy_targets_absent = $false
            old_env_exists = $false
            publish_host = "127.0.0.1"
            publish_port = 3026
        } | ConvertTo-Json -Compress)
    )
    [IO.File]::WriteAllText(
        (Join-Path $script:CutoverJournal "old-compose.yml"), "old-compose`n"
    )
    [IO.File]::WriteAllBytes(
        (Join-Path $script:CutoverJournal "old.env"), [byte[]]@()
    )
    [IO.File]::WriteAllText(
        (Join-Path $script:CutoverJournal "phase.txt"), "prepared`n"
    )
    function docker {
        $Arguments = @($args)
        $global:LASTEXITCODE = 0
    }
    function Replace-ComposeAtomically([string] $Source, [string] $Destination) {
        [IO.File]::Copy($Source, $Destination, $true)
        Remove-Item -LiteralPath $Source -Force
    }
    Restore-InterruptedCutover $environmentPath
    Assert-True (-not (Test-Path -LiteralPath $environmentPath)) `
        "v2 recovery recreated an .env that did not exist before cutover"
    Assert-True `
        ([IO.File]::ReadAllText($script:ComposePath) -eq "old-compose`n") `
        "v2 recovery did not restore the exact old Compose"

    $identityDirectory = Join-Path $temporaryRoot "project-identity"
    New-Item -ItemType Directory -Path $identityDirectory | Out-Null
    function docker {
        $Arguments = @($args)
        $global:LASTEXITCODE = 0
        if ($Arguments[0] -eq "ps") {
            @{
                "com.docker.compose.project.working_dir" = $identityDirectory
                "com.docker.compose.project" = "authoritative-project"
            } | ConvertTo-Json -Compress
        }
    }
    $projects = @(Get-ProjectsForInstallDirectory $identityDirectory)
    Assert-True `
        ($projects.Count -eq 1 -and $projects[0] -eq "authoritative-project") `
        "Windows project identity discovery did not deduplicate matching labels"

    $aclFailure = Join-Path $temporaryRoot "committed-acl-failure"
    New-Item -ItemType Directory -Path $aclFailure | Out-Null
    $script:CutoverJournal = Join-Path $aclFailure ".memory-platform-cutover"
    $script:CutoverCommittedCleanup = "$($script:CutoverJournal).committed-cleanup"
    $script:Layout = "split"
    New-Item -ItemType Directory -Path $script:CutoverJournal | Out-Null
    [IO.File]::WriteAllText(
        (Join-Path $script:CutoverJournal "phase.txt"),
        "data_may_change`n"
    )
    function Protect-PrivatePath([string] $Path) {
        if ($Path.EndsWith("phase.txt")) { throw "synthetic ACL failure" }
    }
    Assert-True (Complete-CutoverJournal) `
        "post-commit ACL failure incorrectly requested rollback"
    Assert-True (-not (Test-Path -LiteralPath $script:CutoverJournal)) `
        "post-commit ACL failure left an active rollback journal"
    function Protect-PrivatePath([string] $Path) { }

    Write-Output "windows-installer-journal: committed crash shapes passed"
} finally {
    Remove-Item -LiteralPath $temporaryBase -Recurse -Force `
        -ErrorAction SilentlyContinue
}
