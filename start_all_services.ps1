$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Test-LocalPort([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Start-HiddenPowerShell([string]$ScriptPath) {
    Start-Process powershell.exe `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
        -WindowStyle Hidden
}

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting QQBot services..."

if (-not (Test-LocalPort 3000)) {
    Start-HiddenPowerShell (Join-Path $Root "start_gemini_proxy.ps1")
    Start-Sleep -Seconds 4
}

$watchdog = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like "*watchdog_gemini.ps1*" -and $_.ProcessId -ne $PID }
if (-not $watchdog) {
    Start-HiddenPowerShell (Join-Path $Root "watchdog_gemini.ps1")
}

if (-not (Test-LocalPort 8766)) {
    Start-HiddenPowerShell (Join-Path $Root "services\local_tts\start_local_tts.ps1")
    Start-Sleep -Seconds 12
}

if (-not (Test-LocalPort 6185)) {
    Start-Process powershell.exe `
        -ArgumentList "-NoProfile -Command Set-Location -LiteralPath `"$Root\astrbot`"; astrbot run" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 12
}

$napcat = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -like "*$Root\napcat-runtime*index.js*" }
if (-not $napcat) {
    Start-Process powershell.exe `
        -ArgumentList "-NoProfile -Command Set-Location -LiteralPath `"$Root\napcat-runtime`"; node index.js" `
        -WindowStyle Hidden
}

Start-Sleep -Seconds 8
$failed = @(3000, 6185, 6199, 8766) | Where-Object { -not (Test-LocalPort $_) }
if ($failed.Count -eq 0) {
    Write-Host "QQBot services are online."
} else {
    Write-Host "Ports not ready: $($failed -join ', ')"
    exit 1
}
