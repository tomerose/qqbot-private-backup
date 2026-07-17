$ErrorActionPreference = 'Stop'
$env:VERTEX_PROJECT = 'solar-modem-496213-f5'
$env:VERTEX_LOCATION = 'global'
# Use the signed-in Windows user's Application Default Credentials. The old
# service-account key can read model metadata but no longer has predict access.
Remove-Item Env:GOOGLE_APPLICATION_CREDENTIALS -ErrorAction SilentlyContinue
# Explicit Python312 — ADC / google.auth only installed here
$python = "C:\Users\liu\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
& $python -u (Join-Path $PSScriptRoot 'gemini-proxy.py')
