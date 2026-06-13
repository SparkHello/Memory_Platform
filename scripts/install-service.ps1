# Register memory-gateway as a Windows service via NSSM.
# Run from an elevated (Administrator) PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
# Re-running after config changes: uninstall first (scripts\uninstall-service.ps1).

$ErrorActionPreference = "Stop"

$nssm = "C:\Users\spari\Tools\nssm.exe"
$serviceName = "memory-gateway"
$projectDir = "C:\Users\spari\Documents\Memory\memory-gateway"
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
$logDir = Join-Path $projectDir "logs"
$port = 2026

if (-not (Test-Path $nssm)) { throw "nssm.exe not found at $nssm" }
if (-not (Test-Path $python)) { throw "venv python not found at $python" }

$inUse = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    $owner = (Get-Process -Id $inUse[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    throw "Port $port is already in use by '$owner' (PID $($inUse[0].OwningProcess)). Stop the foreground uvicorn (Ctrl+C) first."
}

New-Item -ItemType Directory -Force $logDir | Out-Null

& $nssm install $serviceName $python "-m uvicorn app.main:app --host 0.0.0.0 --port $port"
& $nssm set $serviceName AppDirectory $projectDir
& $nssm set $serviceName DisplayName "Memory Gateway (MCP)"
& $nssm set $serviceName Description "Long-term memory MCP service for Kelivo"
& $nssm set $serviceName Start SERVICE_AUTO_START
& $nssm set $serviceName AppStdout (Join-Path $logDir "service.log")
& $nssm set $serviceName AppStderr (Join-Path $logDir "service.log")
& $nssm set $serviceName AppRotateFiles 1
& $nssm set $serviceName AppRotateOnline 1
& $nssm set $serviceName AppRotateBytes 10485760

& $nssm start $serviceName
Start-Sleep -Seconds 3
& $nssm status $serviceName
