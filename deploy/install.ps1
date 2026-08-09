# Memory Platform one-click installer for Windows PowerShell 5.1+.
# Run after Docker Desktop has started:
#   irm https://raw.githubusercontent.com/SparkHello/Memory_Platform/main/deploy/install.ps1 | iex

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$RepoRaw = "https://raw.githubusercontent.com/SparkHello/Memory_Platform/main"
$ComposeName = "docker-compose.user.yml"

function Write-Step([string] $Message) {
    Write-Host "==> $Message"
}

function Stop-Install([string] $Message) {
    throw "安装失败：$Message"
}

function Test-CustomKey([string] $Name, [string] $Value) {
    if ([string]::IsNullOrEmpty($Value)) { return }
    if ($Value -match "\s") {
        Stop-Install "$Name 不能包含空格或换行。"
    }
    if ($Value.Length -lt 16) {
        Stop-Install "$Name 至少需要 16 个字符；不设置则自动生成高强度密钥。"
    }
}

function Get-ComposeEnvValue([string] $Key) {
    if (-not (Test-Path -LiteralPath ".env" -PathType Leaf)) { return "" }
    $value = ""
    foreach ($line in [IO.File]::ReadAllLines((Join-Path (Get-Location) ".env"))) {
        if ($line.StartsWith("$Key=")) {
            $value = $line.Substring($Key.Length + 1).TrimEnd("`r")
        }
    }
    return $value
}

function Set-ComposeEnvValue([string] $Key, [string] $Value) {
    $path = Join-Path (Get-Location) ".env"
    $lines = if (Test-Path -LiteralPath $path -PathType Leaf) {
        @([IO.File]::ReadAllLines($path))
    } else {
        @()
    }
    $result = New-Object System.Collections.Generic.List[string]
    $updated = $false
    foreach ($line in $lines) {
        if ($line.StartsWith("$Key=")) {
            if (-not $updated) {
                $result.Add("$Key=$Value")
                $updated = $true
            }
        } else {
            $result.Add($line)
        }
    }
    if (-not $updated) { $result.Add("$Key=$Value") }
    $utf8NoBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($path, $result, $utf8NoBom)
}

function Test-PortInUse([int] $Port) {
    $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return [bool]($listeners | Where-Object { $_.Port -eq $Port } | Select-Object -First 1)
}

function Test-ComposeOwnsPort([int] $Port) {
    $published = @(& docker compose -f $ComposeName port memory-platform 2026 2>$null)
    if ($LASTEXITCODE -ne 0 -or $published.Count -eq 0) { return $false }
    return [bool]($published[-1] -match ":$Port$")
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
                -not $_.IsIPv6LinkLocal -and
                $_.ToString() -notmatch "^169\.254\."
            }
        $first = $addresses | Select-Object -First 1
        if ($null -ne $first) { return $first.ToString() }
    } catch {
        return ""
    }
    return ""
}

function Find-GeneratedKey([string[]] $Logs, [string] $Marker) {
    for ($index = 0; $index -lt $Logs.Count; $index++) {
        if ($Logs[$index].Contains($Marker)) {
            for ($next = $index + 1; $next -lt [Math]::Min($Logs.Count, $index + 5); $next++) {
                if ($Logs[$next] -match "^\s{2,}(\S+)\s*$") {
                    return $Matches[1]
                }
            }
        }
    }
    return ""
}

$gatewayApiKey = [Environment]::GetEnvironmentVariable("GATEWAY_API_KEY")
$adminKeyInput = [Environment]::GetEnvironmentVariable("MEMORY_CONSOLE_ADMIN_KEY")
Test-CustomKey "GATEWAY_API_KEY" $gatewayApiKey
Test-CustomKey "MEMORY_CONSOLE_ADMIN_KEY" $adminKeyInput

Write-Step "检查运行环境"
if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-Install "未找到 Docker。请先安装并启动 Docker Desktop，再重新运行本命令。"
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-Install "Docker 已安装但尚未运行。请启动 Docker Desktop 后重试。"
}
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-Install "未找到 docker compose。请升级 Docker Desktop 后重试。"
}

$installDir = [Environment]::GetEnvironmentVariable("MEMORY_PLATFORM_DIR")
if ([string]::IsNullOrWhiteSpace($installDir)) {
    $existingDirs = @(& docker ps -a `
        --filter "label=com.docker.compose.service=memory-platform" `
        --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>$null) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
    if ($existingDirs.Count -gt 1) {
        Stop-Install "检测到多套 Memory Platform。请先设置 `$env:MEMORY_PLATFORM_DIR 为要升级的目录。"
    }
    if ($existingDirs.Count -eq 1) {
        $installDir = $existingDirs[0]
        Write-Host "    已找到现有安装：$installDir"
    } else {
        $installDir = Join-Path $HOME "memory-platform"
    }
}

$installDir = [IO.Path]::GetFullPath($installDir)
Write-Step "下载 Compose 文件到 $installDir"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Set-Location -LiteralPath $installDir
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$RepoRaw/deploy/$ComposeName" -OutFile $ComposeName
} catch {
    Stop-Install "下载 $ComposeName 失败，请检查网络后重试。$($_.Exception.Message)"
}

$explicitPort = [Environment]::GetEnvironmentVariable("MEMORY_PORT")
$existingPort = Get-ComposeEnvValue "MEMORY_PORT"
$portConfigured = $false
$portFromEnvironment = $false
if (-not [string]::IsNullOrWhiteSpace($explicitPort)) {
    $portText = $explicitPort
    $portConfigured = $true
    $portFromEnvironment = $true
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
if ((Test-PortInUse $port) -and -not (Test-ComposeOwnsPort $port)) {
    if ($portConfigured) {
        Stop-Install "端口 $port 已被占用。请换一个空闲端口，例如先运行 `$env:MEMORY_PORT='3026'。"
    }
    $candidate = $port + 1
    while (Test-PortInUse $candidate) {
        $candidate++
        if ($candidate -ge 2100) {
            Stop-Install "2026–2099 端口均被占用，请设置 `$env:MEMORY_PORT 为其他空闲端口。"
        }
    }
    Write-Host "    默认端口 $port 已被占用，改用 $candidate。"
    $port = $candidate
}
if ($port -ne 2026 -or $portFromEnvironment -or -not [string]::IsNullOrWhiteSpace($existingPort)) {
    Set-ComposeEnvValue "MEMORY_PORT" $port.ToString()
}

$explicitHost = [Environment]::GetEnvironmentVariable("MEMORY_HOST")
$existingHost = Get-ComposeEnvValue "MEMORY_HOST"
$hostFromEnvironment = $false
if (-not [string]::IsNullOrWhiteSpace($explicitHost)) {
    $listenHost = $explicitHost
    $hostFromEnvironment = $true
} elseif (-not [string]::IsNullOrWhiteSpace($existingHost)) {
    $listenHost = $existingHost
} else {
    $listenHost = "127.0.0.1"
}
if ($listenHost -notin @("127.0.0.1", "0.0.0.0")) {
    Stop-Install "MEMORY_HOST 只支持 127.0.0.1（仅本机）或 0.0.0.0（局域网）。"
}
if ($listenHost -ne "127.0.0.1" -or $hostFromEnvironment -or -not [string]::IsNullOrWhiteSpace($existingHost)) {
    Set-ComposeEnvValue "MEMORY_HOST" $listenHost
}
if ($listenHost -eq "0.0.0.0") {
    Write-Host "    已开启局域网访问，请只在可信家庭网络中使用。"
}
$env:MEMORY_PORT = $port.ToString()
$env:MEMORY_HOST = $listenHost

$currentContainer = @(& docker compose -f $ComposeName ps -aq memory-platform 2>$null) | Select-Object -First 1
$preexisting = -not [string]::IsNullOrWhiteSpace($currentContainer)
if (-not $preexisting) {
    $projectName = [Environment]::GetEnvironmentVariable("COMPOSE_PROJECT_NAME")
    if ([string]::IsNullOrWhiteSpace($projectName)) {
        $projectName = Get-ComposeEnvValue "COMPOSE_PROJECT_NAME"
    }
    if ([string]::IsNullOrWhiteSpace($projectName)) {
        $projectName = ([IO.Path]::GetFileName($installDir).ToLowerInvariant() -replace "[^a-z0-9_-]", "")
    }
    $volumes = @(& docker volume ls `
        --filter "label=com.docker.compose.project=$projectName" `
        --filter "label=com.docker.compose.volume=memory-platform-data" `
        --format '{{.Name}}' 2>$null)
    $preexisting = [bool]($volumes | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -First 1)
}

if ($preexisting) {
    Write-Step "升级前创建安全备份"
    $currentContainer = @(& docker compose -f $ComposeName ps -q memory-platform 2>$null) | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($currentContainer)) {
        & docker compose -f $ComposeName up -d --pull never *> $null
        if ($LASTEXITCODE -ne 0) {
            Stop-Install "检测到已有数据，但无法启动旧版本完成备份；现有数据未被修改。"
        }
        $currentContainer = @(& docker compose -f $ComposeName ps -q memory-platform 2>$null) | Select-Object -First 1
    }
    if ([string]::IsNullOrWhiteSpace($currentContainer)) {
        Stop-Install "检测到已有数据，但没有找到可执行备份的容器；现有数据未被修改。"
    }
    $backupName = "pre-upgrade-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))-$PID.zip"
    $backupDir = Join-Path $installDir "backups"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    & docker compose -f $ComposeName exec -T memory-platform `
        memgw stack backup --output "/data/$backupName"
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "升级前备份失败，已停止升级；现有服务和数据未被替换。"
    }
    $backupPath = Join-Path $backupDir $backupName
    & docker cp "${currentContainer}:/data/$backupName" $backupPath
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "备份已在数据卷中生成，但复制到安装目录失败，已停止升级。"
    }
    Write-Host "    备份已保存：$backupPath"
}

Write-Step "拉取镜像并启动（首次需要几分钟）"
& docker compose -f $ComposeName pull
if ($LASTEXITCODE -ne 0) {
    Stop-Install "镜像下载失败。现有数据未被删除，请检查网络后重新运行本脚本。"
}
& docker compose -f $ComposeName up -d
if ($LASTEXITCODE -ne 0) {
    Stop-Install "容器启动失败。升级前备份仍保存在 $installDir\backups。"
}

Write-Step "等待基础服务就绪（首次通常 1–2 分钟）"
$ready = $false
for ($attempt = 0; $attempt -lt 180; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$port/health"
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $ready) {
    Stop-Install "服务 3 分钟内未就绪。请在 $installDir 运行 docker compose -f $ComposeName logs memory-platform。"
}

$logs = @(& docker compose -f $ComposeName logs --no-log-prefix memory-platform 2>$null)
$generatedGatewayKey = Find-GeneratedKey $logs "自动生成客户端访问密钥"
$generatedAdminKey = Find-GeneratedKey $logs "自动生成 Web 配置管理密钥"
$lanIp = Get-FirstLanIp

Write-Host ""
Write-Host "============================================"
Write-Host "Memory Platform 基础服务已启动"
Write-Host ""
Write-Host "  Web Console（管理台）  http://127.0.0.1:$port/ui/"
Write-Host "  客户端 Base URL        http://127.0.0.1:$port/v1"
Write-Host "  客户端模型名           memory-auto"
if ($listenHost -eq "0.0.0.0" -and -not [string]::IsNullOrWhiteSpace($lanIp)) {
    Write-Host "  手机/其他设备地址      http://${lanIp}:$port/v1"
}
Write-Host ""
if (-not [string]::IsNullOrEmpty($gatewayApiKey)) {
    Write-Host "  GATEWAY_API_KEY（客户端和 Web Console 登录用）：使用了你提供的值"
} elseif (-not [string]::IsNullOrEmpty($generatedGatewayKey)) {
    Write-Host "  GATEWAY_API_KEY（客户端和 Web Console 登录用）："
    Write-Host "    $generatedGatewayKey"
}
if (-not [string]::IsNullOrEmpty($adminKeyInput)) {
    Write-Host "  admin key（浏览器里解锁模型渠道配置用）：使用了你提供的值"
} elseif (-not [string]::IsNullOrEmpty($generatedAdminKey)) {
    Write-Host "  admin key（仅在电脑浏览器里使用，权限更高）："
    Write-Host "    $generatedAdminKey"
}
Write-Host ""
Write-Host "  只有 GATEWAY_API_KEY 需要填进客户端（含手机）。admin key 不要传到手机。"
if (([string]::IsNullOrEmpty($generatedGatewayKey) -and [string]::IsNullOrEmpty($gatewayApiKey)) -or
    ([string]::IsNullOrEmpty($generatedAdminKey) -and [string]::IsNullOrEmpty($adminKeyInput))) {
    if ($preexisting) {
        Write-Host "  这是已有安装：密钥沿用首次生成的值，本次不会重新打印。"
        Write-Host "  找不回时请在 $installDir 重设（旧 key 会立即失效）："
        Write-Host "    docker compose -f $ComposeName exec memory-platform memgw secret set gateway"
        Write-Host "    docker compose -f $ComposeName exec memory-platform modelgw secret set memory-console-admin"
    } else {
        Write-Host "  首次安装未能从日志解析密钥，请运行："
        Write-Host "    docker compose -f $ComposeName logs memory-platform"
    }
}
Write-Host "============================================"
Write-Host ""
Write-Host "请先保存上面的密钥。浏览器首次设置只需按顺序粘贴密钥、选择渠道和模型。"
if ($listenHost -eq "127.0.0.1") {
    Write-Host "手机接入：先运行 `$env:MEMORY_HOST='0.0.0.0'，再重新执行本安装命令。"
    Write-Host "仅限可信家庭网络，不要暴露到公网。"
}
Write-Host "以后升级：重新运行同一条安装命令即可；脚本会先自动备份数据。"

if ([Environment]::GetEnvironmentVariable("MEMORY_NO_OPEN") -ne "1") {
    try { Start-Process "http://127.0.0.1:$port/ui/" } catch { }
}
