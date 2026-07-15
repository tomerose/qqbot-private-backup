$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Stopping all QQBot services..."

# Kill by port
$ports = @(3000, 6185, 6199, 8766)
foreach ($p in $ports) {
    $conn = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue
    if ($conn) {
        $conn | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
        Write-Host "  Killed port $p"
    }
}

# Kill remaining project Python processes and the NapCat launcher/worker pairs.
# Node itself is installed under Program Files, so filtering on its executable path
# leaves old NapCat processes behind and makes a restart silently use stale config.
$pythonProcs = @(Get-Process -Name python -ErrorAction SilentlyContinue) |
    Where-Object { $_.Path -like "*qqbot*" }
$napcatEntry = Join-Path $Root "napcat-runtime\napcat\napcat.mjs"
$napcatWorkers = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'") |
    Where-Object { $_.CommandLine -match [regex]::Escape($napcatEntry) }
$napcatPids = @($napcatWorkers.ProcessId + $napcatWorkers.ParentProcessId | Where-Object { $_ } | Select-Object -Unique)

foreach ($p in $pythonProcs) {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    Write-Host "  Killed $($p.ProcessName) pid=$($p.Id)"
}
foreach ($napcatPid in $napcatPids) {
    Stop-Process -Id $napcatPid -Force -ErrorAction SilentlyContinue
    Write-Host "  Killed NapCat node pid=$napcatPid"
}

Start-Sleep -Seconds 3

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting QQBot services..."
& (Join-Path $Root "start_all_services.ps1")
