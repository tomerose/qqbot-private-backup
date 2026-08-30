$ErrorActionPreference = "Continue"
$mutex = [Threading.Mutex]::new($false, "Local\XiaoningLLMWatchdog")
if (-not $mutex.WaitOne(0)) { exit 0 }
$proxyScript = Join-Path $PSScriptRoot "start_llm_proxy.ps1"
try {
    while ($true) {
        Start-Sleep -Seconds 60
        try {
            if ((Invoke-WebRequest -Uri "http://127.0.0.1:3000/healthz" -TimeoutSec 10 -UseBasicParsing).StatusCode -eq 200) { continue }
        } catch {}
        $listener = Get-NetTCPConnection -State Listen -LocalPort 3000 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($listener) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
            if ($process.CommandLine -like "*openai_proxy.py*") {
                Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
            }
        }
        Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$proxyScript`"" -WindowStyle Hidden
        Start-Sleep -Seconds 15
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
