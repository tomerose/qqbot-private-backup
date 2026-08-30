$ErrorActionPreference = 'Stop'
$localConfig = Join-Path $PSScriptRoot 'xiaoning.local.ps1'
if (Test-Path -LiteralPath $localConfig) { . $localConfig }
if (-not $env:VERTEX_PROJECT -or $env:VERTEX_PROJECT -eq 'your-google-cloud-project') {
    throw 'Set VERTEX_PROJECT in xiaoning.local.ps1.'
}
if (-not $env:VERTEX_LOCATION) { $env:VERTEX_LOCATION = 'global' }
if (-not $env:XIAONING_OUTBOUND_PROXY) {
    $localProxy = Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 7890 -ErrorAction SilentlyContinue
    if ($localProxy) { $env:XIAONING_OUTBOUND_PROXY = 'http://127.0.0.1:7890' }
}
if ($env:XIAONING_OUTBOUND_PROXY) {
    $env:HTTPS_PROXY = $env:XIAONING_OUTBOUND_PROXY
    $env:HTTP_PROXY = $env:XIAONING_OUTBOUND_PROXY
}
# Use the signed-in Windows user's Application Default Credentials. The old
# service-account key can read model metadata but no longer has predict access.
Remove-Item Env:GOOGLE_APPLICATION_CREDENTIALS -ErrorAction SilentlyContinue
# The bootstrap pins AstrBot and google-auth to Python 3.12.
$python = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
& $python -u (Join-Path $PSScriptRoot 'gemini-proxy.py')
