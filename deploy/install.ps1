# Memory Platform release installer for Windows PowerShell 5.1+.
#
# Download and run a fixed release; never pipe a mutable branch into iex:
#   $Version = "v0.2.0"
#   irm "https://raw.githubusercontent.com/SparkHello/Memory_Platform/$Version/deploy/install.ps1" -OutFile install-memory-platform.ps1
#   $env:MEMORY_PLATFORM_VERSION = $Version
#   & .\install-memory-platform.ps1
#
# Long-lived containers never receive access credentials through Compose
# environment variables or daemon logs. Generated values are delivered only
# through private files under <install-dir>\credentials.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$script:ComposeName = "docker-compose.user.yml"
$script:ProjectName = ""
$script:InstallDirectory = ""
$script:ComposePath = ""
$script:CandidateCompose = ""
$script:Layout = "fresh"
$script:BackupPath = ""
$script:OldComposeBackup = ""
$script:InitImage = ""
$script:RollbackInitImage = ""
$script:RollbackModelImage = ""
$script:RollbackMemoryImage = ""
$script:EnvironmentSnapshot = ""
$script:EnvironmentSnapshotBytes = [byte[]]@()
$script:EnvironmentSnapshotExists = $false
$script:OriginalImageEnvironment = @{}
$script:CosignPath = ""
$script:CosignTemporary = ""
$script:ComposeBundle = ""
$script:CutoverJournal = ""
$script:CutoverCommittedCleanup = ""
$script:CandidateEnvironment = ""
$script:CandidateInternalOverride = ""
$script:CandidateEmptyEnvironment = ""
$script:InstallLockStream = $null
$script:RequestedComposeProject = ""
$script:RequestedMemoryHost = ""
$script:RequestedMemoryPort = ""
$script:PublishHost = ""
$script:PublishPort = 0

function Write-Step([string] $Message) {
    Write-Host "==> $Message"
}

function Stop-Install([string] $Message) {
    throw "安装失败：$Message"
}

function New-TemporarySibling([string] $Path, [string] $Purpose) {
    $directory = [IO.Path]::GetDirectoryName($Path)
    $filename = [IO.Path]::GetFileName($Path)
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $suffix = [Guid]::NewGuid().ToString("N")
        $candidate = Join-Path $directory ".$filename.$Purpose.$suffix"
        if (-not (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    Stop-Install "无法在安装目录创建安全临时文件。"
}

function Write-TextAtomic([string] $Path, [string] $Content) {
    $temporary = New-TemporarySibling $Path "tmp"
    $utf8NoBom = New-Object Text.UTF8Encoding($false)
    try {
        [IO.File]::WriteAllText($temporary, $Content, $utf8NoBom)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporary, $Path, $null)
        } else {
            [IO.File]::Move($temporary, $Path)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Initialize-DurableFilesystemApi {
    if ($null -ne ("MemoryPlatform.NativeFilesystem" -as [type])) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace MemoryPlatform {
    public static class NativeFilesystem {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool MoveFileExW(string existingName, string newName, uint flags);

        public static void MoveWriteThrough(string source, string destination) {
            const uint MOVEFILE_REPLACE_EXISTING = 0x1;
            const uint MOVEFILE_WRITE_THROUGH = 0x8;
            if (!MoveFileExW(source, destination,
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
    }
}
'@
}

function Move-PathWriteThrough([string] $Source, [string] $Destination) {
    Initialize-DurableFilesystemApi
    [MemoryPlatform.NativeFilesystem]::MoveWriteThrough($Source, $Destination)
}

function Sync-File([string] $Path) {
    $stream = [IO.FileStream]::new(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::Read,
        4096,
        [IO.FileOptions]::WriteThrough
    )
    try {
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

function Write-DurableTextAtomic([string] $Path, [string] $Content) {
    $temporary = New-TemporarySibling $Path "durable"
    $utf8NoBom = New-Object Text.UTF8Encoding($false)
    $bytes = $utf8NoBom.GetBytes($Content)
    try {
        $stream = [IO.FileStream]::new(
            $temporary,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None,
            4096,
            [IO.FileOptions]::WriteThrough
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally {
            $stream.Dispose()
        }
        Move-PathWriteThrough $temporary $Path
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-BytesAtomic([string] $Path, [byte[]] $Bytes) {
    $temporary = New-TemporarySibling $Path "bytes"
    try {
        [IO.File]::WriteAllBytes($temporary, $Bytes)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporary, $Path, $null)
        } else {
            [IO.File]::Move($temporary, $Path)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Acquire-InstallerLock([string] $Path) {
    try {
        $script:InstallLockStream = [IO.FileStream]::new(
            $Path,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None,
            4096,
            [IO.FileOptions]::WriteThrough
        )
        $script:InstallLockStream.SetLength(0)
        $bytes = [Text.Encoding]::ASCII.GetBytes("$PID`n")
        $script:InstallLockStream.Write($bytes, 0, $bytes.Length)
        $script:InstallLockStream.Flush($true)
        Protect-PrivatePath $Path
    } catch {
        if ($null -ne $script:InstallLockStream) {
            $script:InstallLockStream.Dispose()
            $script:InstallLockStream = $null
        }
        Stop-Install "另一安装器仍在运行，或无法取得安装事务排他锁；本次未修改任何状态。"
    }
}

function Release-InstallerLock {
    if ($null -ne $script:InstallLockStream) {
        $script:InstallLockStream.Dispose()
        $script:InstallLockStream = $null
    }
}

function Resolve-CredentialFile([string] $CredentialDirectory, [string] $Role) {
    # Prefer .txt so Windows/macOS open plain text; accept legacy .key.
    foreach ($extension in @("txt", "key")) {
        $candidate = Join-Path $CredentialDirectory "$Role.$extension"
        if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and
            (Get-Item -LiteralPath $candidate).Length -gt 0) {
            return $candidate
        }
    }
    return $null
}

function Protect-PrivatePath([string] $Path) {
    try {
        $item = Get-Item -LiteralPath $Path -Force
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl = Get-Acl -LiteralPath $Path
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($existingRule in @($acl.Access)) {
            [void] $acl.RemoveAccessRuleAll($existingRule)
        }
        if ($item.PSIsContainer) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $identity,
                [Security.AccessControl.FileSystemRights]::FullControl,
                ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                    [Security.AccessControl.InheritanceFlags]::ObjectInherit),
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
        } else {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $identity,
                [Security.AccessControl.FileSystemRights]::FullControl,
                [Security.AccessControl.AccessControlType]::Allow
            )
        }
        [void] $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $Path -AclObject $acl
    } catch {
        Stop-Install "无法把私有文件权限限制为当前 Windows 用户；请使用本机 NTFS 目录后重试。"
    }
}

function Test-ComposeEnvKey([string] $Path, [string] $Key) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $escaped = [Regex]::Escape($Key)
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match "^\s*(?:export\s+)?$escaped\s*=") { return $true }
    }
    return $false
}

function Get-ComposeEnvValue([string] $Path, [string] $Key) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    $escaped = [Regex]::Escape($Key)
    $value = ""
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match "^\s*(?:export\s+)?$escaped\s*=(.*)$") {
            $value = $Matches[1].TrimEnd("`r")
        }
    }
    return $value
}

function Update-ComposeEnvironment(
    [string] $Path,
    [hashtable] $Values,
    [string[]] $RemoveKeys
) {
    $lines = if (Test-Path -LiteralPath $Path -PathType Leaf) {
        @([IO.File]::ReadAllLines($Path))
    } else {
        @()
    }
    $result = New-Object System.Collections.Generic.List[string]
    $written = @{}
    foreach ($line in $lines) {
        $matchedKey = $null
        foreach ($key in @($Values.Keys) + @($RemoveKeys)) {
            $escaped = [Regex]::Escape([string] $key)
            if ($line -match "^\s*(?:export\s+)?$escaped\s*=") {
                $matchedKey = [string] $key
                break
            }
        }
        if ($null -eq $matchedKey) {
            [void] $result.Add($line)
            continue
        }
        if ($Values.ContainsKey($matchedKey) -and -not $written.ContainsKey($matchedKey)) {
            [void] $result.Add("$matchedKey=$($Values[$matchedKey])")
            $written[$matchedKey] = $true
        }
    }
    foreach ($key in $Values.Keys) {
        if (-not $written.ContainsKey([string] $key)) {
            [void] $result.Add("$key=$($Values[$key])")
        }
    }
    $content = if ($result.Count -gt 0) {
        ([string]::Join("`n", $result) + "`n")
    } else {
        ""
    }
    Write-TextAtomic $Path $content
    Protect-PrivatePath $Path
}

function Write-CandidateEnvironment(
    [string] $Path,
    [string] $InitImage,
    [string] $ModelImage,
    [string] $MemoryImage
) {
    if ($script:EnvironmentSnapshotExists) {
        [IO.File]::WriteAllBytes($Path, $script:EnvironmentSnapshotBytes)
    } else {
        [IO.File]::WriteAllBytes($Path, [byte[]]@())
    }
    Update-ComposeEnvironment $Path @{
        "MEMORY_PLATFORM_INIT_IMAGE" = $InitImage
        "MEMORY_PLATFORM_MODEL_IMAGE" = $ModelImage
        "MEMORY_PLATFORM_MEMORY_IMAGE" = $MemoryImage
        "MEMORY_CREDENTIAL_DIR" = "./credentials"
        "HOST_UID" = "10001"
        "HOST_GID" = "10001"
        "MEMORY_PORT" = [string] $script:PublishPort
        "MEMORY_HOST" = $script:PublishHost
        "COMPOSE_PROJECT_NAME" = $script:ProjectName
    } @(
        "GATEWAY_API_KEY", "MEMORY_CONSOLE_ADMIN_KEY",
        "COMPOSE_ENV_FILES", "COMPOSE_DISABLE_ENV_FILE", "COMPOSE_PROFILES",
        "COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR"
    )
    Protect-PrivatePath $Path
}

function Get-ExistingInstallDirectories {
    $directories = New-Object System.Collections.Generic.List[string]
    foreach ($service in @("model-gateway", "memory-gateway", "memory-platform")) {
        $found = @(& docker ps -a `
            --filter "label=com.docker.compose.service=$service" `
            --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>$null)
        foreach ($directory in $found) {
            if (-not [string]::IsNullOrWhiteSpace($directory) -and
                -not $directories.Contains($directory.Trim())) {
                [void] $directories.Add($directory.Trim())
            }
        }
    }
    return $directories.ToArray()
}

function Get-ProjectsForInstallDirectory([string] $InstallDirectory) {
    $projects = New-Object System.Collections.Generic.List[string]
    $expected = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\', '/')
    foreach ($service in @("model-gateway", "memory-gateway", "memory-platform")) {
        $found = @(& docker ps -a `
            --filter "label=com.docker.compose.service=$service" `
            --format '{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.project"}}' `
            2>$null)
        foreach ($entry in $found) {
            $parts = ([string] $entry).Split('|', 2)
            if ($parts.Count -ne 2 -or [string]::IsNullOrWhiteSpace($parts[0]) -or
                [string]::IsNullOrWhiteSpace($parts[1])) {
                continue
            }
            try {
                $workingDirectory = [IO.Path]::GetFullPath($parts[0]).TrimEnd('\', '/')
            } catch {
                continue
            }
            if ([string]::Equals(
                $workingDirectory,
                $expected,
                [StringComparison]::OrdinalIgnoreCase
            ) -and -not $projects.Contains($parts[1])) {
                [void] $projects.Add($parts[1])
            }
        }
    }
    return $projects.ToArray()
}

function Get-ComposeServices([string] $ComposeFile) {
    $services = @(& docker compose -p $script:ProjectName -f $ComposeFile `
        config --services 2>$null)
    if ($LASTEXITCODE -ne 0) { return @() }
    return @($services | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Test-ComposeOwnsPort([string] $ComposeFile, [int] $Port) {
    if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) { return $false }
    foreach ($service in @("model-gateway", "memory-gateway", "memory-platform")) {
        $published = @(& docker compose -p $script:ProjectName -f $ComposeFile `
            port $service 2026 2>$null)
        if ($LASTEXITCODE -eq 0 -and $published.Count -gt 0 -and
            $published[-1].Trim() -match ":$Port$") {
            return $true
        }
    }
    return $false
}

function Test-PortInUse([int] $Port) {
    $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return [bool]($listeners | Where-Object { $_.Port -eq $Port } | Select-Object -First 1)
}

function Test-HostIp([string] $Address) {
    # 允许操作者绑定任意点分十进制 IPv4（回环、全接口或指定本机地址）。
    if ([string]::IsNullOrWhiteSpace($Address)) { return $false }
    $parts = $Address.Split(".")
    if ($parts.Count -ne 4) { return $false }
    foreach ($part in $parts) {
        if ($part -notmatch '^[0-9]{1,3}$') { return $false }
        if ($part.Length -gt 1 -and $part.StartsWith("0")) { return $false }
        if ([int] $part -gt 255) { return $false }
    }
    return $true
}

function Test-HttpEndpoint([string] $Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri $Url
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-HttpEndpoint([string] $Url, [int] $Attempts) {
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if (Test-HttpEndpoint $Url) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Test-CandidateContainerHttp(
    [string] $ComposeFile,
    [string] $OverrideFile,
    [string] $EnvironmentFile,
    [string] $Service,
    [string] $Url
) {
    $code = @'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
'@
    & docker compose --env-file $EnvironmentFile -p $script:ProjectName `
        -f $ComposeFile -f $OverrideFile exec -T $Service `
        python -c $code $Url *> $null
    return $LASTEXITCODE -eq 0
}

function Wait-CandidateContainerHttp(
    [string] $ComposeFile,
    [string] $OverrideFile,
    [string] $EnvironmentFile,
    [string] $Service,
    [string] $Url,
    [int] $Attempts
) {
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if (Test-CandidateContainerHttp `
            $ComposeFile $OverrideFile $EnvironmentFile $Service $Url) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function ConvertTo-NativeQuotedArgument([string] $Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    $escaped = [Regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [Regex]::Replace($escaped, '(\\*)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-DockerWithInputFile(
    [string[]] $Arguments,
    [string] $InputFile
) {
    if (-not (Test-Path -LiteralPath $InputFile -PathType Leaf) -or
        (Get-Item -LiteralPath $InputFile).Length -le 0) {
        return $false
    }
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $dockerCommand) { return $false }
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $dockerCommand.Source
    $quoted = @($Arguments | ForEach-Object {
        ConvertTo-NativeQuotedArgument ([string] $_)
    })
    $startInfo.Arguments = [string]::Join(' ', $quoted)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $input = $null
    try {
        if (-not $process.Start()) { return $false }
        $input = [IO.File]::OpenRead($InputFile)
        $input.CopyTo($process.StandardInput.BaseStream)
        $process.StandardInput.Close()
        [void] $process.StandardOutput.ReadToEnd()
        [void] $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return $process.ExitCode -eq 0
    } catch {
        return $false
    } finally {
        if ($null -ne $input) { $input.Dispose() }
        $process.Dispose()
    }
}

function Test-CandidateCredential(
    [string] $ComposeFile,
    [string] $OverrideFile,
    [string] $EnvironmentFile,
    [string] $Service,
    [string] $Url,
    [string] $CredentialFile
) {
    $code = @'
import sys, urllib.request
token = sys.stdin.read().strip()
if not token:
    raise SystemExit(1)
request = urllib.request.Request(
    sys.argv[1], headers={"Authorization": "Bearer " + token}
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
'@
    $arguments = @(
        'compose', '--env-file', $EnvironmentFile,
        '-p', $script:ProjectName, '-f', $ComposeFile, '-f', $OverrideFile,
        'exec', '-T', $Service, 'python', '-c', $code, $Url
    )
    return Invoke-DockerWithInputFile $arguments $CredentialFile
}

function Get-FirstLanIp {
    try {
        $addresses = [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
            Where-Object {
                $_.OperationalStatus -eq [Net.NetworkInformation.OperationalStatus]::Up -and
                $_.NetworkInterfaceType -ne [Net.NetworkInformation.NetworkInterfaceType]::Loopback
            } |
            ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
            ForEach-Object { $_.Address } |
            Where-Object {
                $_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and
                $_.ToString() -notmatch "^169\.254\."
            }
        $first = $addresses | Select-Object -First 1
        if ($null -ne $first) { return $first.ToString() }
    } catch { }
    return ""
}

function Get-ProjectVolume([string] $VolumeKey) {
    $volumes = @(& docker volume ls `
        --filter "label=com.docker.compose.project=$script:ProjectName" `
        --filter "label=com.docker.compose.volume=$VolumeKey" `
        --format '{{.Name}}' 2>$null)
    return [string](@($volumes | Where-Object { $_ } | Select-Object -First 1))
}

function Test-LegacyTargetVolumeExists([string] $VolumeKey) {
    if (-not [string]::IsNullOrWhiteSpace((Get-ProjectVolume $VolumeKey))) {
        return $true
    }
    $expected = "$($script:ProjectName)_$VolumeKey"
    $names = @(& docker volume inspect $expected --format '{{.Name}}' 2>$null)
    return $LASTEXITCODE -eq 0 -and
        [string](@($names | Where-Object { $_ } | Select-Object -First 1)) -eq $expected
}

function Remove-LegacyTransactionVolumes {
    try {
        $metadataPath = Join-Path $script:CutoverJournal "metadata.json"
        if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
            return $false
        }
        $metadata = [IO.File]::ReadAllText($metadataPath) | ConvertFrom-Json
        if ((Get-JsonPropertyValue $metadata "legacy_targets_absent") -ne $true) {
            return $false
        }
        $containers = @(& docker ps -aq `
            --filter "label=com.docker.compose.project=$($script:ProjectName)" 2>$null)
        foreach ($container in @($containers | Where-Object { $_ })) {
            & docker rm -f $container *> $null
            if ($LASTEXITCODE -ne 0) { return $false }
        }
        foreach ($key in @(
            "memory-data", "memory-secrets", "model-data", "model-secrets"
        )) {
            $volume = Get-ProjectVolume $key
            if ([string]::IsNullOrWhiteSpace($volume)) { continue }
            $labels = @(& docker volume inspect $volume --format `
                '{{ index .Labels "com.docker.compose.project" }}|{{ index .Labels "com.docker.compose.volume" }}' `
                2>$null)
            $labelValue = [string](@($labels | Where-Object { $_ } | Select-Object -First 1))
            if ($LASTEXITCODE -ne 0 -or
                $labelValue -ne "$($script:ProjectName)|$key") {
                return $false
            }
            & docker volume rm $volume *> $null
            if ($LASTEXITCODE -ne 0) { return $false }
        }
        return $true
    } catch {
        return $false
    }
}

function Get-ContainerVolume([string] $Container, [string] $Destination) {
    $format = "{{range .Mounts}}{{if eq .Destination `"$Destination`"}}{{.Name}}{{end}}{{end}}"
    $values = @(& docker inspect $Container --format $format 2>$null)
    if ($LASTEXITCODE -ne 0) { return "" }
    return [string](@($values | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -First 1))
}

function Get-ServiceImageId([string] $ComposeFile, [string] $Service) {
    $containers = @(& docker compose -p $script:ProjectName -f $ComposeFile `
        ps -aq $Service 2>$null)
    $container = [string](@($containers | ForEach-Object { $_.Trim() } |
        Where-Object { $_ } | Select-Object -First 1))
    if ([string]::IsNullOrWhiteSpace($container)) { return "" }
    $images = @(& docker inspect $container --format '{{.Image}}' 2>$null)
    if ($LASTEXITCODE -ne 0) { return "" }
    return [string](@($images | ForEach-Object { $_.Trim() } |
        Where-Object { $_ } | Select-Object -First 1))
}

function Resolve-ImageDigest([string] $Tag) {
    $separator = $Tag.LastIndexOf(":")
    if ($separator -lt 1) { Stop-Install "发布镜像名称无效。" }
    $repository = $Tag.Substring(0, $separator)
    $references = @(& docker image inspect $Tag `
        --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "无法检查已拉取镜像的 digest。"
    }
    $pattern = "^$([Regex]::Escape($repository))@sha256:[0-9a-f]{64}$"
    $matching = @($references | ForEach-Object { $_.Trim() } |
        Where-Object { $_ -match $pattern } | Select-Object -First 1)
    if ($matching.Count -ne 1) {
        Stop-Install "无法把发布镜像解析为该仓库的不可变 digest。"
    }
    return $matching[0]
}

function Initialize-CosignVerifier {
    if (-not [string]::IsNullOrWhiteSpace($script:CosignPath)) { return }
    $installed = Get-Command cosign -ErrorAction SilentlyContinue
    if ($null -ne $installed) {
        $script:CosignPath = $installed.Source
        return
    }
    $architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    if ($architecture -ne "X64") {
        Stop-Install "当前 Windows 架构缺少内置 cosign 下载；请先安全安装 cosign。"
    }
    $version = "v3.0.6"
    $expectedSha256 = "9b85a88ebff2d9dd30ff4984a6f61f2cedc232dd87d81fa7f2ff3c0ed96c241c"
    $temporary = Join-Path $script:InstallDirectory `
        ".cosign.$([Guid]::NewGuid().ToString('N')).exe"
    try {
        Invoke-WebRequest -UseBasicParsing `
            -Uri "https://github.com/sigstore/cosign/releases/download/$version/cosign-windows-amd64.exe" `
            -OutFile $temporary
        $actualSha256 = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha256 -ne $expectedSha256) {
            Stop-Install "cosign 固定版本 SHA-256 校验失败。"
        }
        Protect-PrivatePath $temporary
    } catch {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
        throw
    }
    $script:CosignTemporary = $temporary
    $script:CosignPath = $temporary
}

function Test-ReleaseComposeSignature(
    [string] $ComposeFile,
    [string] $Bundle,
    [string] $Release
) {
    $identity = "https://github.com/SparkHello/Memory_Platform/.github/workflows/docker.yml@refs/tags/$Release"
    & $script:CosignPath verify-blob `
        --bundle $Bundle `
        --certificate-identity $identity `
        --certificate-oidc-issuer "https://token.actions.githubusercontent.com" `
        $ComposeFile *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "发布 Compose 的 Sigstore 签名无效。"
    }
}

function Test-ReleaseSignature([string] $Image, [string] $Release) {
    $identity = "https://github.com/SparkHello/Memory_Platform/.github/workflows/docker.yml@refs/tags/$Release"
    & $script:CosignPath verify `
        --certificate-identity $identity `
        --certificate-oidc-issuer "https://token.actions.githubusercontent.com" `
        $Image *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "发布镜像签名无效或不是由固定 tag 的官方工作流生成。"
    }
}

function Get-JsonPropertyValue([object] $Object, [string] $Name) {
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-JsonPropertyNames([object] $Object) {
    if ($null -eq $Object) { return @() }
    return @($Object.PSObject.Properties | ForEach-Object { $_.Name })
}

function Test-ExactStringSet([object[]] $Actual, [string[]] $Expected) {
    $actualValues = @($Actual | ForEach-Object { [string] $_ } | Sort-Object -Unique)
    $expectedValues = @($Expected | Sort-Object -Unique)
    if ($actualValues.Count -ne $expectedValues.Count) { return $false }
    return [string]::Join("`n", $actualValues) -eq [string]::Join("`n", $expectedValues)
}

function Test-CandidateCompose(
    [string] $ComposeFile,
    [string] $EnvironmentFile,
    [hashtable] $ExpectedImages
) {
    $jsonLines = @(& docker compose --env-file $EnvironmentFile `
        -p $script:ProjectName --profile maintenance `
        -f $ComposeFile config --format json 2>$null)
    if ($LASTEXITCODE -ne 0 -or $jsonLines.Count -eq 0) {
        Stop-Install "候选 Compose 语法无效。"
    }
    try {
        $configuration = ([string]::Join("`n", $jsonLines) | ConvertFrom-Json)
    } catch {
        Stop-Install "候选 Compose 无法转换为可审计配置。请升级 Docker Desktop。"
    }
    $services = Get-JsonPropertyValue $configuration "services"
    $expectedServices = @(
        "stack-init", "model-gateway", "memory-gateway", "stack-maintenance"
    )
    if (-not (Test-ExactStringSet `
        (Get-JsonPropertyNames $services) $expectedServices)) {
        Stop-Install "候选 Compose 的 split stack 服务集合不安全。"
    }
    foreach ($name in $expectedServices) {
        if ($null -eq (Get-JsonPropertyValue $services $name)) {
            Stop-Install "候选 Compose 缺少 split stack 服务 $name。"
        }
    }
    foreach ($name in $ExpectedImages.Keys) {
        $service = Get-JsonPropertyValue $services ([string] $name)
        $image = [string](Get-JsonPropertyValue $service "image")
        if ($image -ne [string] $ExpectedImages[$name]) {
            Stop-Install "候选 Compose 没有使用指定发布镜像。"
        }
    }
    # The full split-topology isolation contract (ports, networks, UID,
    # volumes) is enforced against the release Compose by
    # deploy/validate_compose.py in the repository's CI release gates.
    $rendered = $configuration | ConvertTo-Json -Depth 100 -Compress
    if ($rendered -match '"(?:GATEWAY_API_KEY|MEMORY_CONSOLE_ADMIN_KEY)"\s*:') {
        Stop-Install "候选 Compose 仍试图通过环境变量传递访问密钥。"
    }
}

function Test-InternalOverrideCompose(
    [string] $ComposeFile,
    [string] $OverrideFile,
    [string] $EnvironmentFile
) {
    & docker compose --env-file $EnvironmentFile `
        -p $script:ProjectName --profile maintenance `
        -f $ComposeFile -f $OverrideFile config *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "本地验收 override 无法生成 Compose 配置。"
    }
}

function Restore-ImageEnvironment {
    foreach ($name in $script:OriginalImageEnvironment.Keys) {
        $value = $script:OriginalImageEnvironment[$name]
        if ($null -eq $value) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$name" -Value ([string] $value)
        }
    }
}

function Restore-ComposeEnvironmentSnapshot {
    if ($script:EnvironmentSnapshotExists) {
        Write-BytesAtomic `
            (Join-Path $script:InstallDirectory ".env") `
            $script:EnvironmentSnapshotBytes
        Protect-PrivatePath (Join-Path $script:InstallDirectory ".env")
    } else {
        Remove-Item -LiteralPath (Join-Path $script:InstallDirectory ".env") `
            -Force -ErrorAction SilentlyContinue
    }
    Restore-ImageEnvironment
}

function Remove-CutoverJournal {
    if ([string]::IsNullOrWhiteSpace($script:CutoverJournal) -or
        -not (Test-Path -LiteralPath $script:CutoverJournal)) {
        return $true
    }
    try {
        $directory = Get-Item -LiteralPath $script:CutoverJournal -Force
        if (-not $directory.PSIsContainer -or
            ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            return $false
        }
        $phasePath = Join-Path $script:CutoverJournal "phase.txt"
        if (-not (Test-Path -LiteralPath $phasePath -PathType Leaf) -or
            ((Get-Item -LiteralPath $phasePath -Force).Attributes -band
                [IO.FileAttributes]::ReparsePoint) -or
            [IO.File]::ReadAllText($phasePath).Trim() -ne "committed") {
            return $false
        }
        if (Test-Path -LiteralPath $script:CutoverCommittedCleanup) {
            $stale = Get-Item -LiteralPath $script:CutoverCommittedCleanup -Force
            if (-not $stale.PSIsContainer -or
                ($stale.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                return $false
            }
            Remove-Item -LiteralPath $script:CutoverCommittedCleanup `
                -Recurse -Force
        }
        # A durable directory rename removes the active rollback marker in one
        # operation.  If power is lost during the following deletion, only a
        # committed-cleanup tombstone survives and no future run can mistake a
        # successfully accepted new stack for an interrupted cutover.
        Move-PathWriteThrough `
            $script:CutoverJournal $script:CutoverCommittedCleanup
        try {
            Remove-Item -LiteralPath $script:CutoverCommittedCleanup `
                -Recurse -Force
        } catch {
            # The active journal is already durably gone.  A later run safely
            # removes the private tombstone before beginning another cutover.
        }
        return $true
    } catch {
        return $false
    }
}

function Remove-CommittedCutoverTombstone {
    if ([string]::IsNullOrWhiteSpace($script:CutoverCommittedCleanup) -or
        -not (Test-Path -LiteralPath $script:CutoverCommittedCleanup)) {
        return $true
    }
    try {
        $directory = Get-Item -LiteralPath $script:CutoverCommittedCleanup -Force
        if (-not $directory.PSIsContainer -or
            ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            return $false
        }
        Remove-Item -LiteralPath $script:CutoverCommittedCleanup -Recurse -Force
        return $true
    } catch {
        return $false
    }
}

function Mark-CutoverCommitted {
    if ($script:Layout -eq "fresh") { return $true }
    try {
        Write-DurableTextAtomic `
            (Join-Path $script:CutoverJournal "phase.txt") "committed`n"
    } catch {
        return $false
    }
    # From this point onward the current state is accepted. Cleanup failure is
    # not a reason to roll back a healthy new stack; startup recovery will only
    # resume committed cleanup.
    try {
        Protect-PrivatePath (Join-Path $script:CutoverJournal "phase.txt")
    } catch {
        # The durable committed marker is the one-way state transition. ACL or
        # cleanup errors after it must never trigger a contradictory rollback.
    }
    return $true
}

function Complete-CutoverJournal {
    if ($script:Layout -eq "fresh") { return $true }
    $phasePath = Join-Path $script:CutoverJournal "phase.txt"
    try {
        $phase = if (Test-Path -LiteralPath $phasePath -PathType Leaf) {
            [IO.File]::ReadAllText($phasePath).Trim()
        } else {
            ""
        }
    } catch {
        return $false
    }
    if ($phase -ne "committed" -and -not (Mark-CutoverCommitted)) {
        return $false
    }
    [void](Remove-CutoverJournal)
    return $true
}

function Invoke-OldComposeUp(
    [string] $ComposeFile,
    [string] $Project,
    [string] $Layout,
    [string] $InitImage,
    [string] $ModelImage,
    [string] $MemoryImage
) {
    $saved = @{}
    foreach ($name in @(
        "MEMORY_PLATFORM_INIT_IMAGE",
        "MEMORY_PLATFORM_MODEL_IMAGE",
        "MEMORY_PLATFORM_MEMORY_IMAGE"
    )) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name)
    }
    try {
        if ($Layout -eq "split") {
            $env:MEMORY_PLATFORM_INIT_IMAGE = $InitImage
            $env:MEMORY_PLATFORM_MODEL_IMAGE = $ModelImage
            $env:MEMORY_PLATFORM_MEMORY_IMAGE = $MemoryImage
        } else {
            foreach ($name in $saved.Keys) {
                Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
            }
        }
        & docker compose -p $Project -f $ComposeFile up -d --pull never *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        foreach ($name in $saved.Keys) {
            if ($null -eq $saved[$name]) {
                Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
            } else {
                Set-Item -Path "Env:$name" -Value ([string] $saved[$name])
            }
        }
    }
}

function Test-ImmutableOldImageReference(
    [string] $Image,
    [string] $Repository
) {
    # $Repository 是不含 registry 主机的仓库路径（如 sparkhello/memory-platform-init）。
    # 允许任意 registry 主机（GHCR 或镜像加速站）；digest 固定保证不可变性。
    if ($Image -match '^sha256:[0-9a-f]{64}$') { return $true }
    $escapedRepository = [Regex]::Escape($Repository)
    return $Image -match "^[A-Za-z0-9._:-]+/${escapedRepository}@sha256:[0-9a-f]{64}$"
}

function New-CutoverJournal {
    if ($script:Layout -eq "fresh") { return }
    if (Test-Path -LiteralPath $script:CutoverJournal) {
        Stop-Install "已有未恢复的升级事务 journal。"
    }
    $pending = "$($script:CutoverJournal).pending.$([Guid]::NewGuid().ToString('N'))"
    try {
        if ($script:Layout -eq "split" -and
            (-not (Test-ImmutableOldImageReference $script:RollbackInitImage `
                "sparkhello/memory-platform-init") -or
             -not (Test-ImmutableOldImageReference $script:RollbackModelImage `
                "sparkhello/memory-platform-model") -or
             -not (Test-ImmutableOldImageReference $script:RollbackMemoryImage `
                "sparkhello/memory-platform-memory"))) {
            Stop-Install "无法把旧 split 栈解析为不可变镜像；拒绝开始 cutover。"
        }
        $legacyTargetsAbsent = $false
        if ($script:Layout -eq "legacy") {
            foreach ($key in @(
                "memory-data", "memory-secrets", "model-data", "model-secrets"
            )) {
                if (Test-LegacyTargetVolumeExists $key) {
                    Stop-Install "legacy 迁移目标卷已存在；拒绝覆盖不明 split 状态。"
                }
            }
            $legacyTargetsAbsent = $true
        }
        New-Item -ItemType Directory -Path $pending | Out-Null
        $oldCompose = Join-Path $pending "old-compose.yml"
        $oldEnvironment = Join-Path $pending "old.env"
        [IO.File]::Copy($script:OldComposeBackup, $oldCompose, $false)
        [IO.File]::WriteAllBytes($oldEnvironment, $script:EnvironmentSnapshotBytes)
        $utf8NoBom = New-Object Text.UTF8Encoding($false)
        # 停写备份在旧栈停止后才创建，journal 建立时先登记为 pending。
        $backupReference = "pending"
        if (-not [string]::IsNullOrWhiteSpace($script:BackupPath)) {
            $backupReference = [IO.Path]::GetFileName($script:BackupPath)
        }
        $metadata = [ordered]@{
            version = 2
            project = $script:ProjectName
            layout = $script:Layout
            backup = $backupReference
            old_init_image = $script:RollbackInitImage
            old_model_image = $script:RollbackModelImage
            old_memory_image = $script:RollbackMemoryImage
            legacy_targets_absent = $legacyTargetsAbsent
            old_env_exists = $script:EnvironmentSnapshotExists
            publish_host = $script:PublishHost
            publish_port = $script:PublishPort
        } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText((Join-Path $pending "metadata.json"), $metadata, $utf8NoBom)
        [IO.File]::WriteAllText((Join-Path $pending "phase.txt"), "prepared`n", $utf8NoBom)
        foreach ($file in @(
            $oldCompose,
            $oldEnvironment,
            (Join-Path $pending "metadata.json"),
            (Join-Path $pending "phase.txt")
        )) {
            Sync-File $file
        }
        foreach ($path in @(
            $pending,
            $oldCompose,
            $oldEnvironment,
            (Join-Path $pending "metadata.json"),
            (Join-Path $pending "phase.txt")
        )) {
            Protect-PrivatePath $path
        }
        Move-PathWriteThrough $pending $script:CutoverJournal
        Protect-PrivatePath $script:CutoverJournal
    } catch {
        if (Test-Path -LiteralPath $pending) {
            Remove-Item -LiteralPath $pending -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

function Set-CutoverDataMayChange {
    if ($script:Layout -eq "fresh") { return }
    Write-DurableTextAtomic `
        (Join-Path $script:CutoverJournal "phase.txt") "data_may_change`n"
    Protect-PrivatePath (Join-Path $script:CutoverJournal "phase.txt")
}

function Update-CutoverBackupReference([string] $BackupPath) {
    if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf) -or
        (Get-Item -LiteralPath $BackupPath).Length -le 0) {
        return $false
    }
    $metadataPath = Join-Path $script:CutoverJournal "metadata.json"
    try {
        $metadata = [IO.File]::ReadAllText($metadataPath) | ConvertFrom-Json
        $metadata.backup = [IO.Path]::GetFileName($BackupPath)
        $serialized = $metadata | ConvertTo-Json -Compress
        Write-DurableTextAtomic $metadataPath ($serialized + "`n")
        Protect-PrivatePath $metadataPath
        return $true
    } catch {
        return $false
    }
}

function Test-BackupArchive([string] $BackupPath, [string] $VerifyImage) {
    # 真实复验：归档内每个成员必须通过 ZIP CRC 校验，且每个 SQLite 库
    # 必须能重新打开并通过 quick_check=ok。
    $verifyScript = @'
import os, shutil, sqlite3, sys, tempfile, zipfile
archive = zipfile.ZipFile("/backup/verify.zip")
corrupt = archive.testzip()
assert corrupt is None, f"CRC mismatch: {corrupt}"
for member in archive.namelist():
    if not member.endswith(".db"):
        continue
    with tempfile.NamedTemporaryFile(dir="/tmp", suffix=".db", delete=False) as staged:
        with archive.open(member) as source:
            shutil.copyfileobj(source, staged)
        staged_path = staged.name
    connection = sqlite3.connect(staged_path)
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
        os.unlink(staged_path)
    assert row and row[0] == "ok", f"quick_check failed: {member}"
'@
    $arguments = @(
        "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--mount", "type=bind,source=$BackupPath,target=/backup/verify.zip,readonly",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=268435456",
        "--entrypoint", "python", $VerifyImage, "-c", $verifyScript
    )
    & docker @arguments *> $null
    return $LASTEXITCODE -eq 0
}

function New-QuiescedBackup(
    [string] $OldMemoryContainer,
    [bool] $UpdateJournal = $true
) {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-$PID-quiesced"
    $backupName = "pre-upgrade-$stamp.zip"
    $backupDirectory = Join-Path $script:InstallDirectory "backups"
    $backupPath = Join-Path $backupDirectory $backupName
    $runner = "$($script:ProjectName)-cutover-backup-$PID"
    $existing = @(& docker ps -aq --filter "name=^/$runner$" 2>$null)
    if (@($existing | Where-Object { $_ }).Count -gt 0) { return $false }
    $directBackup = $false

    if ($script:Layout -eq "split") {
        $memoryData = Get-ProjectVolume "memory-data"
        $memorySecrets = Get-ProjectVolume "memory-secrets"
        $modelData = Get-ProjectVolume "model-data"
        $missingBackupInputs = @(@(
            $memoryData, $memorySecrets, $modelData, $script:RollbackInitImage
        ) | Where-Object { [string]::IsNullOrWhiteSpace([string] $_) })
        if ($missingBackupInputs.Count -gt 0) {
            return $false
        }
        $arguments = @(
            "run", "--name", $runner, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
            "-e", "MEMGW_HOME=/data/config",
            "-e", "MEMGW_SETTINGS_PATH=/secrets/settings.env",
            "-e", "MEMGW_PROJECT_ROOT=/app/services/memory-gateway",
            "-e", "MODEL_GATEWAY_HOME=/model-data",
            "--mount", "type=volume,source=$memoryData,target=/data",
            "--mount", "type=volume,source=$memorySecrets,target=/secrets",
            "--mount", "type=volume,source=$modelData,target=/model-data",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=134217728",
            "--entrypoint", "memgw", $script:RollbackInitImage,
            "--home", "/data/config", "--project-root", "/app/services/memory-gateway",
            "stack", "backup", "--model-gateway-home", "/model-data",
            "--output", "/data/$backupName"
        )
        $cleanupImage = $script:RollbackInitImage
        $cleanupVolume = $memoryData
        $verifyImage = $script:RollbackInitImage
    } else {
        $legacyVolume = Get-ContainerVolume $OldMemoryContainer "/data"
        if ([string]::IsNullOrWhiteSpace($legacyVolume) -or
            [string]::IsNullOrWhiteSpace($script:InitImage)) {
            return $false
        }
        $arguments = @(
            "run", "--rm", "--name", $runner, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE",
            "--cap-add", "FOWNER",
            "--mount", "type=volume,source=$legacyVolume,target=/legacy,readonly",
            "--mount", "type=bind,source=$backupDirectory,target=/backup",
            "--tmpfs", "/scratch:rw,noexec,nosuid,size=33554432",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=134217728",
            "--entrypoint", "python", $script:InitImage,
            "/usr/local/libexec/memory-platform/backup_legacy.py",
            $backupName, "10001", "10001"
        )
        $directBackup = $true
        $verifyImage = $script:InitImage
    }

    & docker @arguments *> $null
    if ($LASTEXITCODE -ne 0) {
        & docker rm -f $runner *> $null
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        return $false
    }
    if (-not $directBackup) {
        & docker cp "${runner}:/data/$backupName" $backupPath *> $null
        $copied = $LASTEXITCODE -eq 0 -and
            (Test-Path -LiteralPath $backupPath -PathType Leaf) -and
            (Get-Item -LiteralPath $backupPath).Length -gt 0
        & docker rm -f $runner *> $null
        if (-not $copied -or $LASTEXITCODE -ne 0) { return $false }
        $cleanupArguments = @(
            "run", "--rm", "--network", "none", "--read-only",
            "--user", "10001:10001", "--cap-drop", "ALL",
            "--mount", "type=volume,source=$cleanupVolume,target=/data",
            "--entrypoint", "python", $cleanupImage,
            "-c", "import os,sys; os.unlink(sys.argv[1])", "/data/$backupName"
        )
        & docker @cleanupArguments *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf) -or
        (Get-Item -LiteralPath $backupPath).Length -le 0) {
        return $false
    }
    if (-not (Test-BackupArchive $backupPath $verifyImage)) {
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        return $false
    }
    try {
        Protect-PrivatePath $backupPath
        if ($UpdateJournal -and
            -not (Update-CutoverBackupReference $backupPath)) {
            return $false
        }
    } catch {
        return $false
    }
    $script:BackupPath = $backupPath
    return $true
}

function Restore-InterruptedCutover([string] $EnvironmentPath) {
    if (-not (Remove-CommittedCutoverTombstone)) {
        Stop-Install "无法安全清理已验收升级的 committed journal tombstone。"
    }
    if (-not (Test-Path -LiteralPath $script:CutoverJournal)) { return }
    $journalDirectory = Get-Item -LiteralPath $script:CutoverJournal -Force
    if (-not $journalDirectory.PSIsContainer -or
        ($journalDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Stop-Install "升级事务 journal 不是安全目录；拒绝继续。"
    }
    $phasePath = Join-Path $script:CutoverJournal "phase.txt"
    if (-not (Test-Path -LiteralPath $phasePath)) {
        if (@(Get-ChildItem -LiteralPath $script:CutoverJournal -Force).Count -eq 0) {
            Remove-Item -LiteralPath $script:CutoverJournal -Force
            return
        }
        Stop-Install "升级事务 journal 不完整；拒绝覆盖当前状态。"
    }
    if (-not (Test-Path -LiteralPath $phasePath -PathType Leaf) -or
        ((Get-Item -LiteralPath $phasePath -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint)) {
        Stop-Install "升级事务 journal 阶段文件不安全。"
    }
    $phase = [IO.File]::ReadAllText($phasePath).Trim()
    $committedPhase = $phase -eq "committed"
    $metadataPath = Join-Path $script:CutoverJournal "metadata.json"
    if ($committedPhase -and
        -not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
        # Compatibility with an older cleanup sequence that could remove
        # metadata after durable commit but before deleting the directory.
        if (-not (Remove-CutoverJournal)) {
            Stop-Install "无法清理已验收升级的 committed journal。"
        }
        return
    }
    $required = @("metadata.json", "old-compose.yml", "old.env")
    foreach ($name in $required) {
        $path = Join-Path $script:CutoverJournal $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
            ((Get-Item -LiteralPath $path -Force).Attributes -band
                [IO.FileAttributes]::ReparsePoint)) {
            Stop-Install "升级事务 journal 不完整；拒绝覆盖当前状态。"
        }
    }
    try {
        $metadata = [IO.File]::ReadAllText(
            (Join-Path $script:CutoverJournal "metadata.json")
        ) | ConvertFrom-Json
    } catch {
        Stop-Install "升级事务 journal metadata 无效；拒绝继续。"
    }
    $version = [int](Get-JsonPropertyValue $metadata "version")
    $project = [string](Get-JsonPropertyValue $metadata "project")
    $layout = [string](Get-JsonPropertyValue $metadata "layout")
    $backupName = [string](Get-JsonPropertyValue $metadata "backup")
    $initImage = [string](Get-JsonPropertyValue $metadata "old_init_image")
    $modelImage = [string](Get-JsonPropertyValue $metadata "old_model_image")
    $memoryImage = [string](Get-JsonPropertyValue $metadata "old_memory_image")
    $legacyTargetsAbsent = Get-JsonPropertyValue $metadata "legacy_targets_absent"
    $oldEnvironmentExists = Get-JsonPropertyValue $metadata "old_env_exists"
    $publishHost = [string](Get-JsonPropertyValue $metadata "publish_host")
    $publishPortText = [string](Get-JsonPropertyValue $metadata "publish_port")
    if ($version -notin @(1, 2) -or
        $project -notmatch '^[a-z0-9][a-z0-9_-]*$' -or
        $layout -notin @("split", "legacy") -or
        $phase -notin @("prepared", "data_may_change", "committed")) {
        Stop-Install "升级事务 journal 字段无效；拒绝继续。"
    }
    if ($backupName -eq "pending") {
        # `pending` 在停写备份创建前写入，且总在 data_may_change 之前被替换；
        # 从 prepared 恢复不会回写数据，因此不需要备份档案。
        if ($phase -ne "prepared") {
            Stop-Install "升级事务 journal 在数据阶段缺少备份引用。"
        }
    } elseif ($backupName -notmatch '^pre-upgrade-[A-Za-z0-9_.-]+\.zip$') {
        Stop-Install "升级事务 journal 字段无效；拒绝继续。"
    }
    $publishPort = 0
    if ($version -eq 2 -and
        ($oldEnvironmentExists -isnot [bool] -or
         -not (Test-HostIp $publishHost) -or
         -not [int]::TryParse($publishPortText, [ref] $publishPort) -or
         $publishPort -lt 1 -or $publishPort -gt 65535)) {
        Stop-Install "升级事务 journal v2 发布或环境字段无效。"
    }
    if ($layout -eq "legacy" -and $legacyTargetsAbsent -ne $true) {
        Stop-Install "legacy 升级事务没有可验证的新卷所有权边界。"
    }
    if ($layout -eq "split") {
        $imageReferences = @(
            @{ Image = $initImage; Repository = "sparkhello/memory-platform-init" },
            @{ Image = $modelImage; Repository = "sparkhello/memory-platform-model" },
            @{ Image = $memoryImage; Repository = "sparkhello/memory-platform-memory" }
        )
        foreach ($entry in $imageReferences) {
            $image = [string] $entry.Image
            if (-not (Test-ImmutableOldImageReference `
                $image ([string] $entry.Repository))) {
                Stop-Install "升级事务 journal 的旧镜像引用无效。"
            }
        }
    }

    if ($committedPhase) {
        if ($version -eq 1) {
            if (-not (Remove-CutoverJournal)) {
                Stop-Install "无法清理已验收升级的 committed journal。"
            }
            return
        }
        if (-not (Test-Path -LiteralPath $script:ComposePath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $EnvironmentPath -PathType Leaf) -or
            (Get-ComposeEnvValue $EnvironmentPath "COMPOSE_PROJECT_NAME") -ne $project -or
            (Get-ComposeEnvValue $EnvironmentPath "MEMORY_HOST") -ne $publishHost -or
            (Get-ComposeEnvValue $EnvironmentPath "MEMORY_PORT") -ne ([string] $publishPort)) {
            Stop-Install "已提交升级的候选 Compose/.env 不完整；journal 已保留。"
        }
        $script:ProjectName = $project
        $script:Layout = $layout
        & docker compose --env-file $EnvironmentPath -p $project `
            -f $script:ComposePath up -d *> $null
        if ($LASTEXITCODE -ne 0 -or
            -not (Wait-HttpEndpoint "http://127.0.0.1:$publishPort/health" 180) -or
            -not (Wait-HttpEndpoint "http://127.0.0.1:$publishPort/readyz" 90)) {
            Stop-Install "已提交升级尚未完成端口发布；journal 已保留供下次幂等恢复。"
        }
        if (-not (Remove-CutoverJournal)) {
            Stop-Install "新栈已公开，但无法清理 committed journal。"
        }
        Write-Host "    已完成中断升级的端口发布；继续校验当前版本。"
        return
    }
    $backupPath = ""
    if ($backupName -ne "pending") {
        $backupPath = Join-Path (Join-Path $script:InstallDirectory "backups") $backupName
        if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf) -or
            (Get-Item -LiteralPath $backupPath).Length -le 0) {
            Stop-Install "升级事务 journal 对应的备份不存在。"
        }
    }

    Write-Step "检测到中断的升级事务，先幂等恢复旧栈"
    $script:ProjectName = $project
    $script:Layout = $layout
    $containers = @(& docker ps -aq `
        --filter "label=com.docker.compose.project=$project" 2>$null)
    foreach ($container in @($containers | Where-Object { $_ })) {
        & docker stop $container *> $null
        if ($LASTEXITCODE -ne 0) {
            Stop-Install "无法停止中断事务中的容器；journal 已保留。"
        }
    }
    if ($layout -eq "legacy" -and -not (Remove-LegacyTransactionVolumes)) {
        Stop-Install "无法安全清理中断 legacy 迁移创建的 split 卷；journal 已保留。"
    }

    try {
        $composeTemporary = New-TemporarySibling $script:ComposePath "recovery"
        [IO.File]::Copy(
            (Join-Path $script:CutoverJournal "old-compose.yml"),
            $composeTemporary,
            $false
        )
        Replace-ComposeAtomically $composeTemporary $script:ComposePath
        if ($version -eq 1 -or $oldEnvironmentExists) {
            $environmentBytes = [IO.File]::ReadAllBytes(
                (Join-Path $script:CutoverJournal "old.env")
            )
            Write-BytesAtomic $EnvironmentPath $environmentBytes
            Protect-PrivatePath $EnvironmentPath
        } else {
            Remove-Item -LiteralPath $EnvironmentPath -Force `
                -ErrorAction SilentlyContinue
        }
    } catch {
        Stop-Install "无法原子恢复旧 Compose/.env；journal 已保留。"
    }

    $script:BackupPath = $backupPath
    $script:RollbackInitImage = $initImage
    $script:RollbackModelImage = $modelImage
    $script:RollbackMemoryImage = $memoryImage
    if ($layout -eq "split" -and $phase -eq "data_may_change") {
        $memoryData = Get-ProjectVolume "memory-data"
        $memorySecrets = Get-ProjectVolume "memory-secrets"
        $modelData = Get-ProjectVolume "model-data"
        if ([string]::IsNullOrWhiteSpace($memoryData) -or
            [string]::IsNullOrWhiteSpace($memorySecrets) -or
            [string]::IsNullOrWhiteSpace($modelData)) {
            Stop-Install "无法定位中断事务的分卷；journal 已保留。"
        }
        $restoreArguments = @(
            "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
            "-e", "RESTORE_ARCHIVE=/backup/restore.zip",
            "--mount", "type=volume,source=$memoryData,target=/data",
            "--mount", "type=volume,source=$memorySecrets,target=/secrets",
            "--mount", "type=volume,source=$modelData,target=/model-data",
            "--volume", "${backupPath}:/backup/restore.zip:ro",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=134217728",
            "--entrypoint", "python", $initImage,
            "/usr/local/libexec/memory-platform/restore_split.py"
        )
        & docker @restoreArguments *> $null
        if ($LASTEXITCODE -ne 0) {
            Stop-Install "中断事务的数据恢复失败；journal 已保留。"
        }
    }
    if (-not (Invoke-OldComposeUp `
        $script:ComposePath $project $layout $initImage $modelImage $memoryImage)) {
        Stop-Install "旧栈重启失败；journal 已保留。"
    }
    if (-not (Complete-CutoverJournal)) {
        Stop-Install "旧栈已恢复，但无法提交升级事务 journal。"
    }
    Write-Host "    中断升级已恢复；继续重新执行发布校验。"
}

function Replace-ComposeAtomically([string] $Source, [string] $Destination) {
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        [IO.File]::Replace($Source, $Destination, $null)
    } else {
        [IO.File]::Move($Source, $Destination)
    }
}

function Invoke-Rollback {
    if ($script:Layout -eq "fresh") { return $false }
    Write-Step "新版本未通过验收，恢复旧 Compose"
    & docker compose -p $script:ProjectName -f $script:ComposePath stop *> $null

    if ($script:Layout -eq "legacy" -and
        -not (Remove-LegacyTransactionVolumes)) {
        return $false
    }

    if ($script:Layout -eq "split") {
        $memoryData = Get-ProjectVolume "memory-data"
        $memorySecrets = Get-ProjectVolume "memory-secrets"
        $modelData = Get-ProjectVolume "model-data"
        if ([string]::IsNullOrWhiteSpace($memoryData) -or
            [string]::IsNullOrWhiteSpace($memorySecrets) -or
            [string]::IsNullOrWhiteSpace($modelData) -or
            [string]::IsNullOrWhiteSpace($script:BackupPath) -or
            -not (Test-Path -LiteralPath $script:BackupPath -PathType Leaf)) {
            return $false
        }
        $restoreImage = if ([string]::IsNullOrWhiteSpace($script:RollbackInitImage)) {
            $script:InitImage
        } else {
            $script:RollbackInitImage
        }
        $restoreArguments = @(
            "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
            "-e", "RESTORE_ARCHIVE=/backup/restore.zip",
            "--mount", "type=volume,source=$memoryData,target=/data",
            "--mount", "type=volume,source=$memorySecrets,target=/secrets",
            "--mount", "type=volume,source=$modelData,target=/model-data",
            "--volume", "$($script:BackupPath):/backup/restore.zip:ro",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=134217728",
            "--entrypoint", "python", $restoreImage,
            "/usr/local/libexec/memory-platform/restore_split.py"
        )
        & docker @restoreArguments *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
    }

    try {
        Restore-ComposeEnvironmentSnapshot
        $temporary = New-TemporarySibling $script:ComposePath "rollback"
        [IO.File]::Copy($script:OldComposeBackup, $temporary, $false)
        Replace-ComposeAtomically $temporary $script:ComposePath
        if (-not (Invoke-OldComposeUp `
            $script:ComposePath $script:ProjectName $script:Layout `
            $script:RollbackInitImage $script:RollbackModelImage `
            $script:RollbackMemoryImage)) {
            return $false
        }
        if (-not (Complete-CutoverJournal)) { return $false }
    } catch {
        return $false
    }
    return $true
}

function Remove-StaleHostBackups([string] $BackupDirectory, [int] $Retention) {
    $resolvedBackupDirectory = [IO.Path]::GetFullPath($BackupDirectory)
    $archives = @(Get-ChildItem -LiteralPath $resolvedBackupDirectory -File `
        -Filter "pre-upgrade-*.zip" | Sort-Object Name -Descending)
    foreach ($archive in @($archives | Select-Object -Skip $Retention)) {
        if (-not [string]::Equals(
            [IO.Path]::GetFullPath($archive.DirectoryName),
            $resolvedBackupDirectory,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            Stop-Install "拒绝清理非预期备份路径。"
        }
        Remove-Item -LiteralPath $archive.FullName -Force
        $composeBackup = [IO.Path]::ChangeExtension($archive.FullName, ".compose.yml")
        if (Test-Path -LiteralPath $composeBackup -PathType Leaf) {
            Remove-Item -LiteralPath $composeBackup -Force
        }
    }
}

function Invoke-MemoryPlatformInstall {
    $release = [Environment]::GetEnvironmentVariable("MEMORY_PLATFORM_VERSION")
    if ([string]::IsNullOrWhiteSpace($release)) { $release = "v0.2.0" }
    if (-not [Regex]::IsMatch($release, '^v[0-9]+\.[0-9]+\.[0-9]+$')) {
        Stop-Install "MEMORY_PLATFORM_VERSION 必须是 vX.Y.Z 形式的发布版本。"
    }
    $repoRaw = "https://raw.githubusercontent.com/SparkHello/Memory_Platform/$release"

    foreach ($secretName in @("GATEWAY_API_KEY", "MEMORY_CONSOLE_ADMIN_KEY")) {
        if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($secretName))) {
            Stop-Install "新版安装器不接受环境变量中的密钥；请让离线初始化写入 credentials\*.key。"
        }
        Remove-Item -Path "Env:$secretName" -ErrorAction SilentlyContinue
    }

    $script:RequestedComposeProject = [Environment]::GetEnvironmentVariable(
        "COMPOSE_PROJECT_NAME"
    )
    $script:RequestedMemoryHost = [Environment]::GetEnvironmentVariable(
        "MEMORY_HOST"
    )
    $script:RequestedMemoryPort = [Environment]::GetEnvironmentVariable(
        "MEMORY_PORT"
    )
    foreach ($composeVariable in @(
        "COMPOSE_PROJECT_NAME", "COMPOSE_ENV_FILES",
        "COMPOSE_DISABLE_ENV_FILE", "COMPOSE_PROFILES", "COMPOSE_FILE",
        "COMPOSE_PATH_SEPARATOR", "MEMORY_HOST", "MEMORY_PORT",
        "MEMORY_CREDENTIAL_DIR", "HOST_UID", "HOST_GID",
        "MEMORY_PLATFORM_INIT_IMAGE", "MEMORY_PLATFORM_MODEL_IMAGE",
        "MEMORY_PLATFORM_MEMORY_IMAGE"
    )) {
        Remove-Item -Path "Env:$composeVariable" -ErrorAction SilentlyContinue
    }

    $retentionText = [Environment]::GetEnvironmentVariable("MEMORY_BACKUP_RETENTION")
    if ([string]::IsNullOrWhiteSpace($retentionText)) { $retentionText = "5" }
    $backupRetention = 0
    if (-not [int]::TryParse($retentionText, [ref] $backupRetention) -or
        $backupRetention -lt 1 -or $backupRetention -gt 50) {
        Stop-Install "MEMORY_BACKUP_RETENTION 必须是 1–50 的整数。"
    }

    # Sigstore verification is opt-in: it needs four extra GitHub endpoints
    # that are unreachable in several target networks, while images are
    # already pulled by immutable digest.
    $verifyText = [Environment]::GetEnvironmentVariable("MEMORY_VERIFY_SIGNATURES")
    if ([string]::IsNullOrWhiteSpace($verifyText)) { $verifyText = "0" }
    if ($verifyText -notin @("0", "1")) {
        Stop-Install "MEMORY_VERIFY_SIGNATURES 只允许 0 或 1。"
    }
    $verifySignatures = $verifyText -eq "1"

    # GHCR 在部分网络不可达；MEMORY_IMAGE_REGISTRY 只替换 registry 主机，
    # 仓库路径与 digest 固定不变，隔离契约不受影响。
    $imageRegistry = [Environment]::GetEnvironmentVariable("MEMORY_IMAGE_REGISTRY")
    if ([string]::IsNullOrWhiteSpace($imageRegistry)) { $imageRegistry = "ghcr.io" }
    if ($imageRegistry -notmatch '^[A-Za-z0-9._:-]+$') {
        Stop-Install "MEMORY_IMAGE_REGISTRY 只能是 registry 主机名（可带端口），如 ghcr.nju.edu.cn。"
    }

    Write-Step "检查运行环境"
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        Stop-Install "未找到 Docker。请先安装并启动 Docker Desktop。"
    }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { Stop-Install "Docker Desktop 尚未运行。" }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { Stop-Install "需要 Docker Compose v2。" }

    $installDirectory = [Environment]::GetEnvironmentVariable("MEMORY_PLATFORM_DIR")
    if ([string]::IsNullOrWhiteSpace($installDirectory)) {
        $existingDirectories = @(Get-ExistingInstallDirectories)
        if ($existingDirectories.Count -gt 1) {
            Stop-Install "检测到多套安装；请显式设置 MEMORY_PLATFORM_DIR。"
        }
        if ($existingDirectories.Count -eq 1) {
            $installDirectory = $existingDirectories[0]
            Write-Host "    已找到现有安装：$installDirectory"
        } else {
            $profileDirectory = [Environment]::GetFolderPath("UserProfile")
            if ([string]::IsNullOrWhiteSpace($profileDirectory)) {
                Stop-Install "无法确定当前 Windows 用户目录。"
            }
            $installDirectory = Join-Path $profileDirectory "memory-platform"
        }
    }
    $installDirectory = [IO.Path]::GetFullPath($installDirectory)
    if ($null -eq [IO.Directory]::GetParent($installDirectory)) {
        Stop-Install "MEMORY_PLATFORM_DIR 不能是磁盘根目录。"
    }
    New-Item -ItemType Directory -Force -Path $installDirectory | Out-Null
    Set-Location -LiteralPath $installDirectory
    $script:InstallDirectory = $installDirectory
    $script:ComposePath = Join-Path $installDirectory $script:ComposeName
    $script:CutoverJournal = Join-Path $installDirectory ".memory-platform-cutover"
    $script:CutoverCommittedCleanup = `
        Join-Path $installDirectory ".memory-platform-cutover.committed-cleanup"
    $environmentPath = Join-Path $installDirectory ".env"
    $credentialDirectory = Join-Path $installDirectory "credentials"
    $backupDirectory = Join-Path $installDirectory "backups"
    Acquire-InstallerLock (Join-Path $installDirectory ".memory-platform-install.lock")
    New-Item -ItemType Directory -Force -Path $credentialDirectory | Out-Null
    New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
    Protect-PrivatePath $credentialDirectory
    Protect-PrivatePath $backupDirectory
    Restore-InterruptedCutover $environmentPath

    $script:EnvironmentSnapshotExists = Test-Path -LiteralPath $environmentPath -PathType Leaf
    if ($script:EnvironmentSnapshotExists) {
        $script:EnvironmentSnapshotBytes = [IO.File]::ReadAllBytes($environmentPath)
        $script:EnvironmentSnapshot = [IO.File]::ReadAllText($environmentPath)
    } else {
        $script:EnvironmentSnapshotBytes = [byte[]]@()
        $script:EnvironmentSnapshot = ""
    }

    $storedProject = Get-ComposeEnvValue $environmentPath "COMPOSE_PROJECT_NAME"
    $discoveredProjects = @(Get-ProjectsForInstallDirectory $installDirectory)
    if ($discoveredProjects.Count -gt 1) {
        Stop-Install "安装目录关联多个 Compose project；拒绝猜测数据身份。"
    }
    $discoveredProject = if ($discoveredProjects.Count -eq 1) {
        [string] $discoveredProjects[0]
    } else {
        ""
    }
    if (-not [string]::IsNullOrWhiteSpace($discoveredProject)) {
        if (-not [string]::IsNullOrWhiteSpace($script:RequestedComposeProject) -and
            $script:RequestedComposeProject -ne $discoveredProject) {
            Stop-Install "COMPOSE_PROJECT_NAME 与旧容器 project 身份冲突；旧栈未修改。"
        }
        if (-not [string]::IsNullOrWhiteSpace($storedProject) -and
            $storedProject -ne $discoveredProject) {
            Stop-Install ".env 的 COMPOSE_PROJECT_NAME 与旧容器身份冲突；拒绝迁移。"
        }
        $projectName = $discoveredProject
    } else {
        if (-not [string]::IsNullOrWhiteSpace($script:RequestedComposeProject) -and
            -not [string]::IsNullOrWhiteSpace($storedProject) -and
            $script:RequestedComposeProject -ne $storedProject) {
            Stop-Install "本次 COMPOSE_PROJECT_NAME 与现有 .env 冲突；拒绝切换数据 project。"
        }
        $projectName = $script:RequestedComposeProject
        if ([string]::IsNullOrWhiteSpace($projectName)) {
            $projectName = $storedProject
        }
    }
    if ([string]::IsNullOrWhiteSpace($projectName)) {
        $projectName = ([IO.Path]::GetFileName($installDirectory).ToLowerInvariant() -replace "[^a-z0-9_-]", "")
    }
    if ([string]::IsNullOrWhiteSpace($projectName)) { $projectName = "memory-platform" }
    if ($projectName -notmatch '^[a-z0-9][a-z0-9_-]*$') {
        Stop-Install "COMPOSE_PROJECT_NAME 只能包含小写字母、数字、下划线和连字符。"
    }
    $script:ProjectName = $projectName

    $services = @()
    if (Test-Path -LiteralPath $script:ComposePath -PathType Leaf) {
        $services = @(Get-ComposeServices $script:ComposePath)
        if ($services -contains "memory-gateway") {
            $script:Layout = "split"
        } elseif ($services -contains "memory-platform") {
            $script:Layout = "legacy"
        } else {
            Stop-Install "现有 Compose 不是可识别的 Memory Platform 栈；拒绝覆盖。请保留该文件并使用 WSL/手工迁移。"
        }
    }
    if ($script:Layout -eq "fresh") {
        if (-not [string]::IsNullOrWhiteSpace((Get-ProjectVolume "memory-platform-data"))) {
            Stop-Install "发现旧数据卷但没有可验证的旧 Compose；拒绝猜测迁移。请使用固定 release 的 WSL 安装器或手工恢复备份。"
        }
        # 安装目录丢失但四个分卷仍在：直接跑新安装会走到凭据验收才失败且
        # 提示无法行动，这里提前检测并给出接回旧数据的具体做法。
        $splitVolumesPresent = $true
        foreach ($volumeKey in @("memory-data", "memory-secrets", "model-data", "model-secrets")) {
            if ([string]::IsNullOrWhiteSpace((Get-ProjectVolume $volumeKey))) {
                $splitVolumesPresent = $false
                break
            }
        }
        if ($splitVolumesPresent) {
            $gatewayCred = Resolve-CredentialFile $credentialDirectory "gateway"
            $adminCred = Resolve-CredentialFile $credentialDirectory "admin"
            if (-not $gatewayCred -or -not $adminCred) {
                Stop-Install ("检测到 project '$($script:ProjectName)' 的四个数据卷仍在，" +
                    "但 $installDirectory\credentials\ 缺少 gateway/admin 凭据（优先 .txt，兼容旧版 .key）。" +
                    "数据没有丢：把原安装目录里的 credentials\gateway.txt（或 gateway.key）和 credentials\admin.txt（或 admin.key） " +
                    "放回 $installDirectory\credentials\ 后重跑同一条安装命令即可接回旧数据。" +
                    "若两枚密钥确实遗失，参见 docs/stack-operations.md 的密钥重置章节。")
            }
        }
    }
    if ($script:Layout -eq "legacy") {
        foreach ($volumeKey in @("memory-data", "memory-secrets", "model-data", "model-secrets")) {
            if (-not [string]::IsNullOrWhiteSpace((Get-ProjectVolume $volumeKey))) {
                Stop-Install "检测到旧单卷旁已有 split 目标卷；拒绝覆盖不明状态。请保留旧卷并使用 WSL/手工迁移。"
            }
        }
    }
    if ($script:Layout -eq "split") {
        $script:RollbackInitImage = Get-ServiceImageId $script:ComposePath "stack-init"
        if ([string]::IsNullOrWhiteSpace($script:RollbackInitImage)) {
            $script:RollbackInitImage = Get-ComposeEnvValue `
                $environmentPath "MEMORY_PLATFORM_INIT_IMAGE"
        }
        $script:RollbackModelImage = Get-ServiceImageId $script:ComposePath "model-gateway"
        if ([string]::IsNullOrWhiteSpace($script:RollbackModelImage)) {
            $script:RollbackModelImage = Get-ComposeEnvValue `
                $environmentPath "MEMORY_PLATFORM_MODEL_IMAGE"
        }
        $script:RollbackMemoryImage = Get-ServiceImageId $script:ComposePath "memory-gateway"
        if ([string]::IsNullOrWhiteSpace($script:RollbackMemoryImage)) {
            $script:RollbackMemoryImage = Get-ComposeEnvValue `
                $environmentPath "MEMORY_PLATFORM_MEMORY_IMAGE"
        }
    }

    $explicitPort = $script:RequestedMemoryPort
    $existingPort = Get-ComposeEnvValue $environmentPath "MEMORY_PORT"
    $portConfigured = $false
    if (-not [string]::IsNullOrWhiteSpace($explicitPort)) {
        $portText = $explicitPort
        $portConfigured = $true
    } elseif (-not [string]::IsNullOrWhiteSpace($existingPort)) {
        $portText = $existingPort
        $portConfigured = $true
    } else {
        $portText = "2026"
    }
    $port = 0
    if (-not [int]::TryParse($portText, [ref] $port) -or $port -lt 1 -or $port -gt 65535) {
        Stop-Install "MEMORY_PORT 必须是 1–65535 的整数。"
    }
    $requestedPortBeforeSkip = $port
    if ((Test-PortInUse $port) -and -not (Test-ComposeOwnsPort $script:ComposePath $port)) {
        if ($portConfigured) {
            Stop-Install "端口 $port 已被占用；请显式选择另一个端口。"
        }
        $candidatePort = $port + 1
        while ($candidatePort -le 2099 -and (Test-PortInUse $candidatePort)) {
            $candidatePort++
        }
        if ($candidatePort -gt 2099) { Stop-Install "2026–2099 端口均被占用。" }
        $port = $candidatePort
    }
    if ($port -ne $requestedPortBeforeSkip) {
        Write-Host "提示：端口 $requestedPortBeforeSkip 已被占用，本次改用 ${port}。"
        Write-Host "      后续文档和示例中的 $requestedPortBeforeSkip 请替换为 ${port}。"
    }

    $explicitHost = $script:RequestedMemoryHost
    $existingHost = Get-ComposeEnvValue $environmentPath "MEMORY_HOST"
    if (-not [string]::IsNullOrWhiteSpace($explicitHost)) {
        $listenHost = $explicitHost
    } elseif (-not [string]::IsNullOrWhiteSpace($existingHost)) {
        $listenHost = $existingHost
    } else {
        $listenHost = "127.0.0.1"
    }
    if (-not (Test-HostIp $listenHost)) {
        Stop-Install "MEMORY_HOST 必须是本机可绑定的 IPv4 地址（如 127.0.0.1、0.0.0.0 或局域网 IP）。"
    }
    $script:PublishHost = $listenHost
    $script:PublishPort = $port
    if ($listenHost -ne "127.0.0.1") {
        Write-Host "    已开启可信局域网监听；请确认路由器没有公网端口映射。"
    }

    $oldMemoryContainer = ""
    if ($script:Layout -ne "fresh") {
        $oldService = if ($script:Layout -eq "legacy") { "memory-platform" } else { "memory-gateway" }
        $containers = @(& docker compose -p $script:ProjectName -f $script:ComposePath `
            ps -q $oldService 2>$null)
        $oldMemoryContainer = [string](@($containers | Where-Object { $_ } | Select-Object -First 1))
        if ([string]::IsNullOrWhiteSpace($oldMemoryContainer)) {
            $identityVolumeKey = if ($script:Layout -eq "legacy") {
                "memory-platform-data"
            } else {
                "memory-data"
            }
            if ([string]::IsNullOrWhiteSpace((Get-ProjectVolume $identityVolumeKey))) {
                Stop-Install "现有 Compose 没有同 project 的容器或数据卷；拒绝在空 project 上迁移。"
            }
            if ($script:Layout -eq "legacy") {
                & docker compose -p $script:ProjectName -f $script:ComposePath `
                    up -d --pull never *> $null
                if ($LASTEXITCODE -ne 0) {
                    Stop-Install "无法按旧 Compose 启动服务以定位旧数据卷；现有数据未修改。"
                }
                $containers = @(& docker compose -p $script:ProjectName -f $script:ComposePath `
                    ps -q $oldService 2>$null)
                $oldMemoryContainer = [string](@($containers | Where-Object { $_ } | Select-Object -First 1))
            }
        }
        if ($script:Layout -eq "legacy") {
            if ([string]::IsNullOrWhiteSpace($oldMemoryContainer)) {
                Stop-Install "找不到旧 Memory 容器；拒绝在未备份状态下升级。"
            }
            $legacyImages = @(& docker inspect $oldMemoryContainer `
                --format '{{.Image}}' 2>$null)
            $script:RollbackMemoryImage = [string](@(
                $legacyImages | Where-Object { $_ } | Select-Object -First 1
            ))
            if ([string]::IsNullOrWhiteSpace($script:RollbackMemoryImage)) {
                Stop-Install "无法解析旧单卷容器镜像；拒绝开始离线迁移。"
            }
        }
        Write-Step "保存旧 Compose 快照"
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-$PID"
        $script:OldComposeBackup = Join-Path $backupDirectory "pre-upgrade-$stamp.compose.yml"
        [IO.File]::Copy($script:ComposePath, $script:OldComposeBackup, $false)
        Protect-PrivatePath $script:OldComposeBackup

        # Exactly one data backup is created per upgrade.  For split layouts it
        # is the quiesced snapshot taken right after the old stack stops
        # writing; for legacy layouts the complete v2 archive is created from
        # the read-only old volume once the signed init image is available.
        $script:BackupPath = ""
        if ($script:Layout -eq "legacy") {
            Write-Host "    旧单卷备份将在签名 init 镜像就绪后从只读卷创建。"
        } else {
            Write-Host "    升级备份将在旧服务停写后创建（每次升级一份一致性备份）。"
        }
    }

    Remove-StaleHostBackups $backupDirectory $backupRetention

    Write-Step "下载 $release Compose 并校验"
    $script:CandidateCompose = New-TemporarySibling $script:ComposePath "candidate"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$repoRaw/deploy/$($script:ComposeName)" `
            -OutFile $script:CandidateCompose
    } catch {
        Stop-Install ("下载固定发布版 Compose 失败；旧服务未变。" +
            "raw.githubusercontent.com 在部分网络不可达：可先为 PowerShell 会话设置代理" +
            "（`$env:HTTPS_PROXY = 'http://127.0.0.1:7890'）后重跑安装命令。")
    }
    if ($verifySignatures) {
        $script:ComposeBundle = New-TemporarySibling $script:ComposePath "sigstore"
        try {
            Invoke-WebRequest -UseBasicParsing `
                -Uri "https://github.com/SparkHello/Memory_Platform/releases/download/$release/$($script:ComposeName).sigstore.json" `
                -OutFile $script:ComposeBundle
        } catch {
            Stop-Install "下载发布 Compose 的 Sigstore bundle 失败。"
        }
        Initialize-CosignVerifier
        Test-ReleaseComposeSignature `
            $script:CandidateCompose $script:ComposeBundle $release
    } else {
        Write-Host "    已按默认跳过 Sigstore 签名验证；镜像仍按不可变 digest 固定。"
        Write-Host "    如需启用，设 `$env:MEMORY_VERIFY_SIGNATURES = '1' 后重跑安装命令。"
    }

    $initTag = "$imageRegistry/sparkhello/memory-platform-init:$release"
    $modelTag = "$imageRegistry/sparkhello/memory-platform-model:$release"
    $memoryTag = "$imageRegistry/sparkhello/memory-platform-memory:$release"
    if ($imageRegistry -ne "ghcr.io") {
        Write-Host "    已用 MEMORY_IMAGE_REGISTRY=$imageRegistry 覆盖镜像源；仓库路径与 digest 固定不变。"
    }
    $script:CandidateEnvironment = New-TemporarySibling $environmentPath "candidate"
    Write-CandidateEnvironment `
        $script:CandidateEnvironment $initTag $modelTag $memoryTag
    $imageEnvironmentNames = @(
        "MEMORY_PLATFORM_INIT_IMAGE",
        "MEMORY_PLATFORM_MODEL_IMAGE",
        "MEMORY_PLATFORM_MEMORY_IMAGE"
    )
    foreach ($name in $imageEnvironmentNames) {
        $script:OriginalImageEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
    }
    Test-CandidateCompose $script:CandidateCompose $script:CandidateEnvironment @{
        "stack-init" = $initTag
        "model-gateway" = $modelTag
        "memory-gateway" = $memoryTag
        "stack-maintenance" = $initTag
    }

    Write-Step "拉取三枚 semver 发布镜像"
    & docker compose --env-file $script:CandidateEnvironment `
        -p $script:ProjectName -f $script:CandidateCompose pull
    if ($LASTEXITCODE -ne 0) {
        Stop-Install ("镜像拉取失败；旧服务未变。GHCR 在部分网络不可达：" +
            "可设 `$env:MEMORY_IMAGE_REGISTRY = '<GHCR 镜像站域名>' 重跑安装命令，" +
            "或在 Docker Desktop → Settings → Resources → Proxies 配置代理。")
    }
    $script:InitImage = Resolve-ImageDigest $initTag
    $modelImage = Resolve-ImageDigest $modelTag
    $memoryImage = Resolve-ImageDigest $memoryTag
    if ($verifySignatures) {
        Write-Step "验证三枚镜像的 Sigstore 发布签名"
        Test-ReleaseSignature $script:InitImage $release
        Test-ReleaseSignature $modelImage $release
        Test-ReleaseSignature $memoryImage $release
    }
    Write-CandidateEnvironment `
        $script:CandidateEnvironment $script:InitImage $modelImage $memoryImage
    Test-CandidateCompose $script:CandidateCompose $script:CandidateEnvironment @{
        "stack-init" = $script:InitImage
        "model-gateway" = $modelImage
        "memory-gateway" = $memoryImage
        "stack-maintenance" = $script:InitImage
    }
    $script:CandidateInternalOverride = `
        New-TemporarySibling $script:ComposePath "internal"
    $utf8NoBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText(
        $script:CandidateInternalOverride,
        "services:`n  memory-gateway:`n    ports: !reset []`n",
        $utf8NoBom
    )
    Protect-PrivatePath $script:CandidateInternalOverride
    Test-InternalOverrideCompose `
        $script:CandidateCompose $script:CandidateInternalOverride `
        $script:CandidateEnvironment
    if ($script:Layout -eq "legacy") {
        Write-Step "从只读旧单卷创建并复验 v2 升级前备份"
        if (-not (New-QuiescedBackup $oldMemoryContainer $false)) {
            Stop-Install "无法从只读旧单卷创建完整 v2 备份；旧服务和旧卷未修改。"
        }
    }
    try {
        New-CutoverJournal
    } catch {
        Stop-Install "无法持久创建升级事务 journal；旧服务未变。"
    }

    if ($script:Layout -ne "fresh") {
        & docker compose -p $script:ProjectName -f $script:ComposePath stop *> $null
        if ($LASTEXITCODE -ne 0) {
            Restore-ComposeEnvironmentSnapshot
            if (-not (Invoke-OldComposeUp `
                $script:ComposePath $script:ProjectName $script:Layout `
                $script:RollbackInitImage $script:RollbackModelImage `
                $script:RollbackMemoryImage)) {
                Stop-Install "无法停止旧服务，且精确旧镜像重启失败；journal 已保留。"
            }
            [void](Complete-CutoverJournal)
            Stop-Install "无法停止旧服务；未开始迁移。"
        }
        Write-Step "旧服务已停写，创建并复验最终一致性备份"
        if (-not (New-QuiescedBackup $oldMemoryContainer)) {
            if (Invoke-OldComposeUp `
                $script:ComposePath $script:ProjectName $script:Layout `
                $script:RollbackInitImage $script:RollbackModelImage `
                $script:RollbackMemoryImage) {
                [void](Complete-CutoverJournal)
                Stop-Install "停写后的最终一致性备份失败；旧服务已恢复。"
            }
            Stop-Install "停写后的最终一致性备份失败且旧服务重启失败；journal 已保留。"
        }
    }
    try {
        Replace-ComposeAtomically $script:CandidateCompose $script:ComposePath
    } catch {
        if ($script:Layout -ne "fresh" -and (Invoke-Rollback)) {
            Stop-Install "无法原子换入候选 Compose；旧栈已恢复。"
        }
        Stop-Install "无法原子换入候选 Compose；请保留候选、备份和现有数据卷。"
    }
    $script:CandidateCompose = ""
    try {
        if (Test-Path -LiteralPath $environmentPath -PathType Leaf) {
            [IO.File]::Replace($script:CandidateEnvironment, $environmentPath, $null)
        } else {
            [IO.File]::Move($script:CandidateEnvironment, $environmentPath)
        }
        $script:CandidateEnvironment = ""
        Protect-PrivatePath $environmentPath
    } catch {
        if ($script:Layout -ne "fresh" -and (Invoke-Rollback)) {
            Stop-Install "无法原子换入候选环境；旧栈已恢复。"
        }
        Restore-ComposeEnvironmentSnapshot
        Stop-Install "无法原子换入候选环境；请保留备份和数据卷。"
    }
    Set-CutoverDataMayChange

    if ($script:Layout -eq "legacy") {
        $legacyVolume = Get-ContainerVolume $oldMemoryContainer "/data"
        if ([string]::IsNullOrWhiteSpace($legacyVolume)) {
            if (Invoke-Rollback) {
                Stop-Install "无法定位旧单卷；旧栈已恢复。请使用固定 release 的 WSL 安装器或手工迁移。"
            }
            Stop-Install "无法定位旧单卷且自动回滚不完整；请保留 backups 与所有 Docker 卷。"
        }
        & docker compose --env-file $environmentPath `
            -p $script:ProjectName -f $script:ComposePath `
            create stack-init *> $null
        if ($LASTEXITCODE -ne 0) {
            if (Invoke-Rollback) { Stop-Install "无法创建新分卷；旧栈已恢复。" }
            Stop-Install "无法创建新分卷且自动回滚不完整。"
        }
        $initContainers = @(& docker compose --env-file $environmentPath `
            -p $script:ProjectName -f $script:ComposePath `
            ps -aq stack-init 2>$null)
        $initContainer = [string](@($initContainers | Where-Object { $_ } | Select-Object -First 1))
        $memoryData = Get-ContainerVolume $initContainer "/memory-data"
        $memorySecrets = Get-ContainerVolume $initContainer "/memory-secrets"
        $modelData = Get-ContainerVolume $initContainer "/model-data"
        $modelSecrets = Get-ContainerVolume $initContainer "/model-secrets"
        & docker compose --env-file $environmentPath `
            -p $script:ProjectName -f $script:ComposePath `
            rm -f stack-init *> $null
        $missingVolumes = @(@($memoryData, $memorySecrets, $modelData, $modelSecrets) |
            Where-Object { [string]::IsNullOrWhiteSpace($_) })
        if ($missingVolumes.Count -gt 0) {
            if (Invoke-Rollback) {
                Stop-Install "新分卷解析失败；旧栈已恢复。请使用 WSL/手工迁移。"
            }
            Stop-Install "新分卷解析失败且自动回滚不完整。"
        }
        $migrationArguments = @(
            "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
            "--mount", "type=volume,source=$legacyVolume,target=/legacy,readonly",
            "--mount", "type=volume,source=$memoryData,target=/memory-data",
            "--mount", "type=volume,source=$memorySecrets,target=/memory-secrets",
            "--mount", "type=volume,source=$modelData,target=/model-data",
            "--mount", "type=volume,source=$modelSecrets,target=/model-secrets",
            "--volume", "$credentialDirectory`:/credentials",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=134217728",
            "--entrypoint", "python", $script:InitImage,
            "/usr/local/libexec/memory-platform/migrate_legacy.py"
        )
        & docker @migrationArguments *> $null
        if ($LASTEXITCODE -ne 0) {
            if (Invoke-Rollback) {
                Stop-Install "旧单卷离线迁移失败；旧栈已恢复。请保留旧卷和 backups，并改用固定 release 的 WSL 安装器排查。"
            }
            Stop-Install "旧单卷离线迁移失败且自动回滚不完整；请勿删除旧卷或 backups。"
        }
    }

    Write-Step "在无宿主发布端口的隔离模式启动候选服务"
    & docker compose --env-file $environmentPath -p $script:ProjectName `
        -f $script:ComposePath -f $script:CandidateInternalOverride up -d *> $null
    if ($LASTEXITCODE -ne 0) {
        if (Invoke-Rollback) { Stop-Install "新栈启动失败；旧服务和数据已恢复。" }
        Stop-Install "新栈启动失败且自动回滚不完整；请保留 backups 与旧卷。"
    }

    # Only Docker's own port mapping tables are consulted: a host HTTP probe
    # could be answered by an unrelated third-party process on the same port.
    $candidatePublished = @(& docker compose --env-file $environmentPath `
        -p $script:ProjectName -f $script:ComposePath `
        -f $script:CandidateInternalOverride port memory-gateway 2026 2>$null)
    $runtimePublished = @()
    foreach ($candidateService in @("memory-gateway", "model-gateway")) {
        $candidateIds = @(& docker compose --env-file $environmentPath `
            -p $script:ProjectName -f $script:ComposePath `
            -f $script:CandidateInternalOverride ps -q $candidateService 2>$null)
        $candidateId = [string](@(
            $candidateIds | Where-Object { $_ } | Select-Object -First 1
        ))
        if (-not [string]::IsNullOrWhiteSpace($candidateId)) {
            $runtimePublished += @(& docker port $candidateId 2>$null)
        }
    }
    if (@($candidatePublished | Where-Object { $_ }).Count -gt 0 -or
        @($runtimePublished | Where-Object { $_ }).Count -gt 0) {
        if (Invoke-Rollback) {
            Stop-Install "候选验收阶段意外发布宿主端口；旧服务和数据已恢复。"
        }
        Stop-Install "候选验收阶段意外发布宿主端口且自动回滚不完整。"
    }

    Write-Step "通过容器内部链路验收 Memory 与 Model"
    $candidateChecks = @(
        @{ Service = "memory-gateway"; Url = "http://127.0.0.1:2026/health" },
        @{ Service = "model-gateway"; Url = "http://127.0.0.1:2030/health" }
    )
    foreach ($check in $candidateChecks) {
        if (-not (Wait-CandidateContainerHttp `
            $script:ComposePath $script:CandidateInternalOverride `
            $environmentPath ([string] $check.Service) ([string] $check.Url) 180)) {
            if (Invoke-Rollback) {
                Stop-Install "候选内部 liveness 验收失败；旧服务和数据已恢复。"
            }
            Stop-Install "候选内部 liveness 验收失败且自动回滚不完整。"
        }
    }
    if ($script:Layout -ne "fresh") {
        foreach ($check in @(
            @{ Service = "memory-gateway"; Url = "http://127.0.0.1:2026/readyz" },
            @{ Service = "model-gateway"; Url = "http://127.0.0.1:2030/readyz" }
        )) {
            if (-not (Wait-CandidateContainerHttp `
                $script:ComposePath $script:CandidateInternalOverride `
                $environmentPath ([string] $check.Service) ([string] $check.Url) 90)) {
                if (Invoke-Rollback) {
                    Stop-Install "候选内部 readiness 退化；旧服务和数据已恢复。"
                }
                Stop-Install "候选内部 readiness 退化且自动回滚不完整。"
            }
        }
    }

    $gatewayCredential = Resolve-CredentialFile $credentialDirectory "gateway"
    $adminCredential = Resolve-CredentialFile $credentialDirectory "admin"
    if (-not $gatewayCredential -or -not $adminCredential) {
        if ($script:Layout -ne "fresh" -and (Invoke-Rollback)) {
            Stop-Install "新栈未交付完整 credentials 文件；旧服务和数据已恢复。"
        }
        if ($script:Layout -eq "fresh") {
            & docker compose -p $script:ProjectName -f $script:ComposePath stop *> $null
        }
        Stop-Install "离线初始化没有交付完整 credentials 文件；未从日志读取或显示密钥。"
    }
    try {
        foreach ($credential in @($gatewayCredential, $adminCredential)) {
            Protect-PrivatePath $credential
        }
        Protect-PrivatePath $credentialDirectory
    } catch {
        if ($script:Layout -ne "fresh" -and (Invoke-Rollback)) {
            Stop-Install "无法验证 credentials 私有权限；旧服务和数据已恢复。"
        }
        if ($script:Layout -eq "fresh") {
            & docker compose -p $script:ProjectName -f $script:ComposePath stop *> $null
        }
        Stop-Install "无法验证 credentials 私有权限；新栈已停止，请使用本机 NTFS 目录重试。"
    }

    if (-not (Test-CandidateCredential `
        $script:ComposePath $script:CandidateInternalOverride $environmentPath `
        "memory-gateway" "http://127.0.0.1:2026/auth/tokens" `
        $gatewayCredential) -or
        -not (Test-CandidateCredential `
        $script:ComposePath $script:CandidateInternalOverride $environmentPath `
        "model-gateway" "http://127.0.0.1:2030/admin/configuration" `
        $adminCredential)) {
        if ($script:Layout -ne "fresh" -and (Invoke-Rollback)) {
            Stop-Install "候选 credentials 实际鉴权失败；旧服务和数据已恢复。"
        }
        Stop-Install "候选 credentials 实际鉴权失败且自动回滚不完整。"
    }

    if (-not (Mark-CutoverCommitted)) {
        if ($script:Layout -ne "fresh" -and (Invoke-Rollback)) {
            Stop-Install "新栈已验收但无法提交升级事务；旧服务和数据已恢复。"
        }
        Stop-Install "新栈已验收但无法提交升级事务；journal 已保留。"
    }

    Write-Step "发布已验收的 Memory 入口"
    & docker compose --env-file $environmentPath -p $script:ProjectName `
        -f $script:ComposePath up -d --no-deps --force-recreate `
        memory-gateway *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "升级已提交但入口发布失败；不会回滚已接受的新数据，journal 已保留供重试。"
    }
    if (-not (Wait-HttpEndpoint "http://127.0.0.1:$port/health" 180) -or
        ($script:Layout -ne "fresh" -and
         -not (Wait-HttpEndpoint "http://127.0.0.1:$port/readyz" 90))) {
        Stop-Install "升级已提交但宿主入口尚未就绪；不会回滚，journal 已保留供重试。"
    }
    $published = @(& docker compose --env-file $environmentPath `
        -p $script:ProjectName -f $script:ComposePath `
        port memory-gateway 2026 2>$null)
    if (@($published | Where-Object { $_ -and $_.Trim() -match ":$port$" }).Count -eq 0) {
        Stop-Install "升级已提交但宿主端口契约不匹配；journal 已保留供重试。"
    }
    if (-not (Complete-CutoverJournal)) {
        Write-Host "warning: 新栈已验收并发布，committed journal 将在下次安装时清理。"
    }

    Write-Host ""
    Write-Host "Memory Platform $release 已启动"
    Write-Host "  Web Console:  http://127.0.0.1:$port/ui/"
    Write-Host "  Client URL:   http://127.0.0.1:$port/v1"
    Write-Host "  Model:        memory-auto"
    if ($listenHost -ne "127.0.0.1") {
        $lanIp = if ($listenHost -eq "0.0.0.0") { Get-FirstLanIp } else { $listenHost }
        if (-not [string]::IsNullOrWhiteSpace($lanIp)) {
            Write-Host "  局域网/手机:  http://${lanIp}:$port/v1（Web Console 为 http://${lanIp}:$port/ui/）"
        } else {
            Write-Host "  局域网/手机:  http://<本机局域网IP>:$port/v1（PowerShell 用 ipconfig 查询本机 IPv4）"
        }
        Write-Host "已监听可信局域网；请确认路由器没有把端口映射到公网。"
    }
    Write-Host "  Console token: $gatewayCredential"
    Write-Host "  Admin key:    $adminCredential"
    Write-Host "（纯文本 .txt，可用文本编辑器打开；旧版 .key 仍兼容）"
    Write-Host "密钥值没有进入脚本输出、Compose 环境或 Docker 日志。"
    Write-Host "Model Gateway 2030 仅位于 Docker 内部网络，没有发布宿主端口。"
    if ($script:Layout -eq "legacy") {
        Write-Host "Console 凭据在迁移兼容期可能仍是 legacy all-scope；请尽快创建按设备 Chat/MCP token。"
        Write-Host "旧单卷仍保留用于观察期回滚；确认新栈与备份后再显式删除。"
    }
    if (-not [string]::IsNullOrWhiteSpace($script:BackupPath)) {
        Write-Host "升级前备份：$($script:BackupPath)"
    }

    if ([Environment]::GetEnvironmentVariable("MEMORY_NO_OPEN") -ne "1") {
        try { Start-Process "http://127.0.0.1:$port/ui/" } catch { }
    }
}

$exitCode = 0
try {
    Invoke-MemoryPlatformInstall
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
} finally {
    if (-not [string]::IsNullOrWhiteSpace($script:CandidateCompose) -and
        (Test-Path -LiteralPath $script:CandidateCompose)) {
        Remove-Item -LiteralPath $script:CandidateCompose -Force -ErrorAction SilentlyContinue
    }
    if (-not [string]::IsNullOrWhiteSpace($script:CosignTemporary) -and
        (Test-Path -LiteralPath $script:CosignTemporary)) {
        Remove-Item -LiteralPath $script:CosignTemporary -Force -ErrorAction SilentlyContinue
    }
    if (-not [string]::IsNullOrWhiteSpace($script:ComposeBundle) -and
        (Test-Path -LiteralPath $script:ComposeBundle)) {
        Remove-Item -LiteralPath $script:ComposeBundle -Force -ErrorAction SilentlyContinue
    }
    foreach ($temporaryPath in @(
        $script:CandidateEnvironment,
        $script:CandidateInternalOverride,
        $script:CandidateEmptyEnvironment
    )) {
        if (-not [string]::IsNullOrWhiteSpace($temporaryPath) -and
            (Test-Path -LiteralPath $temporaryPath)) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
    Release-InstallerLock
}
if ($exitCode -ne 0) { exit $exitCode }
