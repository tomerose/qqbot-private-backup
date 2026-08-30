$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$env:ASTRBOT_ROOT = Join-Path $Root "astrbot"
$localConfig = Join-Path $Root "xiaoning.local.ps1"
if (-not (Test-Path -LiteralPath $localConfig)) {
    throw "Missing xiaoning.local.ps1. Run .\setup.ps1 first."
}
. $localConfig

function Test-LocalPort([int]$Port) {
    foreach ($endpoint in [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()) {
        if ($endpoint.Port -eq $Port) { return $true }
    }
    return $false
}

if (-not $env:XIAONING_OUTBOUND_PROXY -and (Test-LocalPort 7890)) {
    $env:XIAONING_OUTBOUND_PROXY = "http://127.0.0.1:7890"
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
    Start-HiddenPowerShell (Join-Path $Root "start_llm_proxy.ps1")
    Wait-LocalPort 3000 20 | Out-Null
}

# The watchdog owns a named mutex, so duplicate launches exit immediately.
Start-HiddenPowerShell (Join-Path $Root "watchdog_llm.ps1")

if ($env:XIAONING_ENABLE_VOICE -eq "1" -and -not (Test-LocalPort 8766)) {
    Start-HiddenPowerShell (Join-Path $Root "services\local_tts\start_local_tts.ps1")
    Wait-LocalPort 8766 180 | Out-Null
}

if (-not (Test-LocalPort 6185)) {
    $python312 = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
    $scripts = (& $python312 -c "import sysconfig; print(sysconfig.get_path('scripts'))").Trim()
    $astrbot = Join-Path $scripts "astrbot.exe"
    if (-not (Test-Path -LiteralPath $astrbot)) {
        throw "AstrBot 4.26.5 is not installed. Run .\setup.ps1 first."
    }
    Start-Process powershell.exe `
        -ArgumentList "-NoProfile -Command Set-Location -LiteralPath `"$Root\astrbot`"; & `"$astrbot`" run" `
        -WindowStyle Hidden
    Wait-LocalPort 6185 60 | Out-Null
}

if (-not (Test-LocalPort 5701)) {
    $napcatNode = Join-Path $Root "napcat-runtime\node.exe"
    if (-not (Test-Path -LiteralPath $napcatNode)) {
        throw "NapCat runtime is missing. Run .\setup.ps1 and finish the NapCat installer."
    }
    Start-Process powershell.exe `
        -ArgumentList "-NoProfile -Command Set-Location -LiteralPath `"$Root\napcat-runtime`"; & `"$napcatNode`" index.js" `
        -WindowStyle Hidden
}

$requiredPorts = @(3000, 5701, 6185, 6199)
if ($env:XIAONING_ENABLE_VOICE -eq "1") { $requiredPorts += 8766 }
$failed = $requiredPorts | Where-Object { -not (Test-LocalPort $_) }
if ($failed.Count -eq 0) {
    Write-Host "QQBot services are online."
} else {
    Write-Host "Ports not ready: $($failed -join ', ')"
    exit 1
}
