$ErrorActionPreference = "Continue"
$mutex = [Threading.Mutex]::new($false, "Local\QQBotGeminiWatchdog")
if (-not $mutex.WaitOne(0)) { exit 0 }

$ProxyScript = Join-Path $PSScriptRoot "start_gemini_proxy.ps1"
$HealthUrl = "http://127.0.0.1:3000/healthz"
$fails = 0

try {
    while ($true) {
        Start-Sleep -Seconds 60
        try {
            $response = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec 10 -UseBasicParsing
            if ($response.StatusCode -eq 200) {
                $fails = 0
                continue
            }
        } catch {}

        $fails++
        if ($fails -lt 3) { continue }

        $listener = Get-NetTCPConnection -State Listen -LocalPort 3000 -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($listener) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
            if ($process.CommandLine -like "*gemini-proxy.py*") {
                Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
            }
        }

        Start-Process powershell.exe `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$ProxyScript`"" `
            -WindowStyle Hidden
        $fails = 0
        Start-Sleep -Seconds 15
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
