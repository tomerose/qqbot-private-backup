$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Stopping all QQBot services..."

# Kill by QQBot-owned ports. 5700 may be occupied by vendor/system services on
# this machine, so NapCat is managed through its actual 5701 listener.
$ports = @(3000, 5701, 6185, 6199, 8766)
$netstat = netstat -ano -p tcp
foreach ($p in $ports) {
    $ownerIds = $netstat | ForEach-Object {
        if ($_ -match "^\s*TCP\s+\S+:${p}\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            [int]$Matches[1]
        }
    } | Sort-Object -Unique
    if ($ownerIds) {
        $ownerIds | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
        Write-Host "  Killed port $p"
    }
}

Start-Sleep -Seconds 3

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting QQBot services..."
& (Join-Path $Root "start_all_services.ps1")
