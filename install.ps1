param(
    [string]$Repository = "https://github.com/tomerose/qqbot-private-backup.git",
    [string]$Ref = "main",
    [string]$InstallDir = (Join-Path $env:USERPROFILE "Xiaoning")
)

$ErrorActionPreference = "Stop"

function Require-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) { return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Git is required. Install Git for Windows, then rerun this command."
    }
    & winget install --id Git.Git --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Git installation failed." }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git was installed but is not available in this PowerShell session."
    }
}

Require-Git
$target = [IO.Path]::GetFullPath($InstallDir)
if (Test-Path -LiteralPath $target) {
    if (-not (Test-Path -LiteralPath (Join-Path $target ".git"))) {
        throw "Install directory exists and is not a Git checkout: $target"
    }
    Write-Host "Using existing Xiaoning checkout."
} else {
    & git clone --depth 1 --branch $Ref $Repository $target
    if ($LASTEXITCODE -ne 0) { throw "Could not download the Xiaoning repository." }
}

$setup = Join-Path $target "setup.ps1"
if (-not (Test-Path -LiteralPath $setup)) { throw "Downloaded repository has no setup.ps1." }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $setup
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
