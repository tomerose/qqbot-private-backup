$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$env:ASTRBOT_ROOT = Join-Path $Root "astrbot"

function Test-LocalPort([int]$Port) {
    foreach ($endpoint in [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()) {
        if ($endpoint.Port -eq $Port) { return $true }
    }
    return $false
}

function Wait-LocalPort([int]$Port, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-LocalPort $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    return (Test-LocalPort $Port)
}

function Start-HiddenPowerShell([string]$ScriptPath) {
    Start-Process powershell.exe `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
        -WindowStyle Hidden
}

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting QQBot services..."

if (-not (Test-LocalPort 3000)) {
    Start-HiddenPowerShell (Join-Path $Root "start_gemini_proxy.ps1")
    Wait-LocalPort 3000 20 | Out-Null
}

# The watchdog owns a named mutex, so duplicate launches exit immediately.
Start-HiddenPowerShell (Join-Path $Root "watchdog_gemini.ps1")

if (-not (Test-LocalPort 8766)) {
    Start-HiddenPowerShell (Join-Path $Root "services\local_tts\start_local_tts.ps1")
    Wait-LocalPort 8766 180 | Out-Null
}

if (-not (Test-LocalPort 6185)) {
    Start-Process powershell.exe `
        -ArgumentList "-NoProfile -Command Set-Location -LiteralPath `"$Root\astrbot`"; astrbot run" `
        -WindowStyle Hidden
    Wait-LocalPort 6185 60 | Out-Null
}

if (-not (Test-LocalPort 5701)) {
    Start-Process powershell.exe `
        -ArgumentList "-NoProfile -Command Set-Location -LiteralPath `"$Root\napcat-runtime`"; node index.js" `
        -WindowStyle Hidden
}

$failed = @(3000, 5701, 6185, 6199, 8766) | Where-Object { -not (Test-LocalPort $_) }
if ($failed.Count -eq 0) {
    Write-Host "QQBot services are online."
} else {
    Write-Host "Ports not ready: $($failed -join ', ')"
    exit 1
}
