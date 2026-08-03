param(
    [int]$Port = 2026,
    [switch]$SkipHealthCheck
)

$ErrorActionPreference = "Stop"

function Test-TailscaleIPv4 {
    param([string]$IPAddress)

    try {
        $parts = $IPAddress.Split(".") | ForEach-Object { [int]$_ }
        return $parts.Length -eq 4 -and $parts[0] -eq 100 -and $parts[1] -ge 64 -and $parts[1] -le 127
    }
    catch {
        return $false
    }
}

function New-AccessUrl {
    param(
        [string]$IPAddress,
        [string]$Path
    )

    return "http://${IPAddress}:$Port$Path"
}

function Get-IpconfigIPv4Entries {
    try {
        $entries = @()
        $adapterName = ""
        $addresses = @()
        $hasDefaultGateway = $false

        foreach ($line in (ipconfig)) {
            if ($line -match "^\S.*adapter\s+(.+):\s*$") {
                foreach ($address in $addresses) {
                    $entries += [pscustomobject]@{
                        Adapter = $adapterName
                        IPAddress = $address
                        HasDefaultGateway = $hasDefaultGateway
                    }
                }
                $adapterName = $matches[1].Trim()
                $addresses = @()
                $hasDefaultGateway = $false
                continue
            }

            if ($line -match "IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)") {
                $addresses += $matches[1]
                continue
            }

            if ($line -match "(Default Gateway|默认网关).*?:\s*(.+)\s*$" -and $matches[2].Trim()) {
                $hasDefaultGateway = $true
            }
        }

        foreach ($address in $addresses) {
            $entries += [pscustomobject]@{
                Adapter = $adapterName
                IPAddress = $address
                HasDefaultGateway = $hasDefaultGateway
            }
        }

        return $entries
    }
    catch {
        return @()
    }
}

function Get-LanIPv4Addresses {
    $addresses = @()

    try {
        $configs = Get-NetIPConfiguration -ErrorAction Stop |
            Where-Object {
                $_.IPv4Address -and
                $_.IPv4DefaultGateway -and
                $_.NetAdapter.Status -eq "Up" -and
                $_.InterfaceAlias -notmatch "Tailscale|Loopback|vEthernet|VMware|VirtualBox|Hyper-V|WSL|Docker"
            }
    }
    catch {
        $configs = @()
    }

    foreach ($config in $configs) {
        foreach ($addr in $config.IPv4Address) {
            if (
                $addr.IPAddress -notlike "127.*" -and
                $addr.IPAddress -notlike "169.254.*" -and
                -not (Test-TailscaleIPv4 $addr.IPAddress)
            ) {
                $addresses += $addr.IPAddress
            }
        }
    }

    if (-not $addresses) {
        try {
            $addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
                Where-Object {
                    $_.IPAddress -notlike "127.*" -and
                    $_.IPAddress -notlike "169.254.*" -and
                    -not (Test-TailscaleIPv4 $_.IPAddress) -and
                    $_.InterfaceAlias -notmatch "Tailscale|Loopback|vEthernet|VMware|VirtualBox|Hyper-V|WSL|Docker"
                } |
                Select-Object -ExpandProperty IPAddress
        }
        catch {
            $addresses = @()
        }
    }

    if (-not $addresses) {
        $addresses = Get-IpconfigIPv4Entries |
            Where-Object {
                $_.HasDefaultGateway -and
                $_.Adapter -notmatch "Tailscale|Loopback|vEthernet|VMware|VirtualBox|Hyper-V|WSL|Docker|Clash|Mihomo|sing-box|proxy|Netch|youtu" -and
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*" -and
                -not (Test-TailscaleIPv4 $_.IPAddress)
            } |
            Select-Object -ExpandProperty IPAddress
    }

    if (-not $addresses) {
        $addresses = Get-IpconfigIPv4Entries |
            Where-Object {
                $_.Adapter -notmatch "Tailscale|Loopback|vEthernet|VMware|VirtualBox|Hyper-V|WSL|Docker|Clash|Mihomo|sing-box|proxy|Netch|youtu" -and
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*" -and
                -not (Test-TailscaleIPv4 $_.IPAddress)
            } |
            Select-Object -ExpandProperty IPAddress
    }

    return $addresses | Select-Object -Unique
}

function Get-TailscaleIPv4Addresses {
    $addresses = @()
    $tailscale = Get-Command tailscale -ErrorAction SilentlyContinue

    if ($tailscale) {
        try {
            $addresses += & $tailscale.Source ip -4 2>$null |
                Where-Object { $_ -match "^\d+\.\d+\.\d+\.\d+$" }
        }
        catch {
            # Fall back to adapter inspection below.
        }
    }

    try {
        $addresses += Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object {
                $_.InterfaceAlias -match "Tailscale" -or (Test-TailscaleIPv4 $_.IPAddress)
            } |
            Select-Object -ExpandProperty IPAddress
    }
    catch {
        # Fall back to ipconfig below.
    }

    if (-not $addresses) {
        $addresses += Get-IpconfigIPv4Entries |
            Where-Object {
                $_.Adapter -match "Tailscale" -or (Test-TailscaleIPv4 $_.IPAddress)
            } |
            Select-Object -ExpandProperty IPAddress
    }

    return $addresses | Select-Object -Unique
}

function Test-Health {
    param([string]$IPAddress)

    if ($SkipHealthCheck) {
        return "skipped"
    }

    $uri = New-AccessUrl -IPAddress $IPAddress -Path "/health"
    try {
        $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
            return "OK"
        }
        return "HTTP $($response.StatusCode)"
    }
    catch {
        return "unreachable: $($_.Exception.Message)"
    }
}

function Write-EndpointGroup {
    param(
        [string]$Title,
        [string[]]$Addresses
    )

    Write-Host ""
    Write-Host $Title
    Write-Host ("-" * $Title.Length)

    if (-not $Addresses) {
        Write-Host "No address detected."
        return
    }

    foreach ($address in $Addresses) {
        Write-Host "Address:          $address"
        Write-Host "MCP URL:          $(New-AccessUrl -IPAddress $address -Path "/mcp")"
        Write-Host "Web console:      $(New-AccessUrl -IPAddress $address -Path "/ui")"
        Write-Host "Health:           $(Test-Health -IPAddress $address)"
        Write-Host ""
    }
}

$lanAddresses = @(Get-LanIPv4Addresses)
$tailscaleAddresses = @(Get-TailscaleIPv4Addresses)

Write-Host "memory-gateway access URLs"
Write-Host "Port: $Port"
Write-Host "Use the LAN URL at home with Tailscale off on iPhone."
Write-Host "Use the Tailscale URL when away from home with Tailscale on."

Write-EndpointGroup -Title "LAN" -Addresses $lanAddresses
Write-EndpointGroup -Title "Tailscale" -Addresses $tailscaleAddresses

Write-Host "Kelivo switch:"
Write-Host "1. Add one MCP server named Memory LAN using the LAN /mcp URL."
Write-Host "2. Add one MCP server named Memory Tailscale using the Tailscale /mcp URL."
Write-Host "3. Keep the same Authorization header on both; enable only the one you are using."
