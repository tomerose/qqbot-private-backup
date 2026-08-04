param([switch]$CheckOnly)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$AstrBotRoot = Join-Path $Root "astrbot"
$LocalConfig = Join-Path $Root "xiaoning.local.ps1"
$AstrBotVersion = "4.26.5"

function Require-Command([string]$Name, [string]$WingetId) {
    if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
    if ($CheckOnly) { throw "Missing prerequisite: $Name" }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Missing $Name and winget. Install $WingetId, then rerun setup.ps1."
    }
    & winget install --id $WingetId --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Failed to install $WingetId" }
}

Require-Command "py" "Python.Launcher"
foreach ($version in @("3.12", "3.11")) {
    & py "-$version" -c "import sys" 2>$null
    if ($LASTEXITCODE -ne 0) {
        if ($CheckOnly) { throw "Python $version is required" }
        & winget install --id "Python.Python.$version" --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "Failed to install Python $version" }
    }
}
Require-Command "ffmpeg" "Gyan.FFmpeg"

$Python312 = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
$InstalledVersion = (& $Python312 -c "import importlib.metadata as m; print(m.version('AstrBot') if m.packages_distributions().get('astrbot') else '')" 2>$null).Trim()
if ($InstalledVersion -ne $AstrBotVersion) {
    if ($CheckOnly) { throw "AstrBot $AstrBotVersion is required; found '$InstalledVersion'" }
    & $Python312 -m pip install "AstrBot==$AstrBotVersion"
    if ($LASTEXITCODE -ne 0) { throw "AstrBot installation failed" }
}

if (-not (Test-Path -LiteralPath $LocalConfig)) {
    if ($CheckOnly) { throw "Missing xiaoning.local.ps1; run setup.ps1 without -CheckOnly" }
    $passwordBytes = New-Object byte[] 18
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($passwordBytes) } finally { $rng.Dispose() }
    $password = [Convert]::ToBase64String($passwordBytes)
    $template = Get-Content (Join-Path $Root "xiaoning.local.example.ps1") -Raw
    [IO.File]::WriteAllText(
        $LocalConfig,
        $template.Replace("__GENERATED_DASHBOARD_PASSWORD__", $password),
        [Text.UTF8Encoding]::new($false)
    )
}
. $LocalConfig

$env:ASTRBOT_ROOT = $AstrBotRoot
$Scripts = (& $Python312 -c "import sysconfig; print(sysconfig.get_path('scripts'))").Trim()
$AstrBotExe = Join-Path $Scripts "astrbot.exe"
$ConfigPath = Join-Path $AstrBotRoot "data\cmd_config.json"
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    if ($CheckOnly) { throw "AstrBot config is not initialized" }
    Push-Location $AstrBotRoot
    try { & $AstrBotExe init } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ConfigPath)) {
        throw "AstrBot initialization failed"
    }
}

$Plugins = Get-Content (Join-Path $Root "xiaoning.plugins.json") -Raw | ConvertFrom-Json
$MissingPlugins = @($Plugins | Where-Object { -not (Test-Path (Join-Path $AstrBotRoot "data\plugins\$_")) })
if ($MissingPlugins) { throw "Missing plugin directories: $($MissingPlugins -join ', ')" }
if (-not $CheckOnly) {
    $Config = Get-Content $ConfigPath -Encoding UTF8 -Raw | ConvertFrom-Json
    $Config.plugin_set = @($Plugins)
    $Config.timezone = "Asia/Shanghai"
    [IO.File]::WriteAllText(
        $ConfigPath,
        ($Config | ConvertTo-Json -Depth 100),
        [Text.UTF8Encoding]::new($false)
    )
}

$NapCatEntry = Join-Path $Root "napcat-runtime\index.js"
if (-not (Test-Path -LiteralPath $NapCatEntry)) {
    if ($CheckOnly) { throw "NapCat runtime is not installed" }
    Start-Process (Join-Path $Root "napcat\NapCatInstaller.exe")
    Write-Host "NapCat installer opened. Finish QQ login and OneBot setup, then rerun setup.ps1."
    exit 2
}

if ($env:VERTEX_PROJECT -eq "your-google-cloud-project") {
    Write-Warning "Set VERTEX_PROJECT in xiaoning.local.ps1 and run Google ADC login before starting."
}
Write-Host "Xiaoning bootstrap is ready. Run .\start_all_services.bat after NapCat and provider login are configured."
