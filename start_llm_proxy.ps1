$ErrorActionPreference = "Stop"
$localConfig = Join-Path $PSScriptRoot "xiaoning.local.ps1"
if (-not (Test-Path -LiteralPath $localConfig)) { throw "Run setup.ps1 first." }
. $localConfig
if (-not $env:XIAONING_LLM_API_BASE -or -not $env:XIAONING_LLM_API_KEY -or -not $env:XIAONING_LLM_MODEL) {
    throw "Configure your own model API in xiaoning.local.ps1 first."
}
$python = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
& $python -u (Join-Path $PSScriptRoot "openai_proxy.py")
