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
        throw "Missing $Name. Install $WingetId, then rerun setup.ps1."
    }
    & winget install --id $WingetId --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Failed to install $WingetId" }
}

function Read-SecretText([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $secure.Dispose()
    }
}

function Protect-LocalConfig([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $identity,
        [Security.AccessControl.FileSystemRights]::Read -bor [Security.AccessControl.FileSystemRights]::Write,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

Require-Command "py" "Python.Launcher"
Require-Command "ffmpeg" "Gyan.FFmpeg"

$Python312 = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $Python312) {
    if ($CheckOnly) { throw "Python 3.12 is required" }
    & winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Python 3.12" }
    $Python312 = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
}

$InstalledVersion = (& $Python312 -c "import importlib.metadata as m; print(m.version('AstrBot') if m.packages_distributions().get('astrbot') else '')" 2>$null).Trim()
if ($InstalledVersion -ne $AstrBotVersion) {
    if ($CheckOnly) { throw "AstrBot $AstrBotVersion is required" }
    & $Python312 -m pip install --disable-pip-version-check "AstrBot==$AstrBotVersion"
    if ($LASTEXITCODE -ne 0) { throw "AstrBot installation failed" }
}

$BootstrapRequirements = Join-Path $Root "requirements-bootstrap.txt"
if (-not (Test-Path -LiteralPath $BootstrapRequirements)) { throw "Bootstrap requirements are missing" }
if (-not $CheckOnly) {
    & $Python312 -m pip install --disable-pip-version-check -r $BootstrapRequirements
    if ($LASTEXITCODE -ne 0) { throw "Bootstrap dependency installation failed" }
}

if (-not (Test-Path -LiteralPath $LocalConfig)) {
    if ($CheckOnly) { throw "Missing xiaoning.local.ps1" }
    Write-Host "Xiaoning needs your own OpenAI-compatible model provider. The API key will not be echoed."
    $apiBase = Read-Host "Model API base URL [https://generativelanguage.googleapis.com/v1beta/openai]"
    if (-not $apiBase) { $apiBase = "https://generativelanguage.googleapis.com/v1beta/openai" }
    try { $apiUri = [Uri]$apiBase } catch { throw "Model API base must be a valid HTTPS URL." }
    $loopback = $apiUri.Host -in @("127.0.0.1", "localhost", "::1")
    if (($apiUri.Scheme -ne "https" -and -not ($apiUri.Scheme -eq "http" -and $loopback)) -or $apiUri.UserInfo) {
        throw "Model API base must use HTTPS; HTTP is allowed only for a local loopback provider, without embedded credentials."
    }
    $model = Read-Host "Model name [gemini-2.5-flash]"
    if (-not $model) { $model = "gemini-2.5-flash" }
    $apiKey = Read-SecretText "Your provider API key"
    if (-not $apiKey) { throw "A user-owned API key is required" }

    $passwordBytes = New-Object byte[] 18
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($passwordBytes) } finally { $rng.Dispose() }
    $template = Get-Content (Join-Path $Root "xiaoning.local.example.ps1") -Raw
    $template = $template.Replace("__XIAONING_LLM_API_BASE__", ($apiBase | ConvertTo-Json -Compress))
    $template = $template.Replace("__XIAONING_LLM_API_KEY__", ($apiKey | ConvertTo-Json -Compress))
    $template = $template.Replace("__XIAONING_LLM_MODEL__", ($model | ConvertTo-Json -Compress))
    $template = $template.Replace("__GENERATED_DASHBOARD_PASSWORD__", [Convert]::ToBase64String($passwordBytes))
    [IO.File]::WriteAllText($LocalConfig, $template, [Text.UTF8Encoding]::new($false))
    Protect-LocalConfig $LocalConfig
}

. $LocalConfig
if (-not $env:XIAONING_LLM_API_BASE -or -not $env:XIAONING_LLM_API_KEY -or -not $env:XIAONING_LLM_MODEL) {
    throw "Configure your own model API in xiaoning.local.ps1 first"
}

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

$Plugins = @(Get-Content (Join-Path $Root "xiaoning.plugins.json") -Raw | ConvertFrom-Json)
$MissingPlugins = @($Plugins | Where-Object { -not (Test-Path (Join-Path $AstrBotRoot "data\plugins\$_")) })
if ($MissingPlugins) { throw "Missing plugin directories: $($MissingPlugins -join ', ')" }

if (-not $CheckOnly) {
    $Config = Get-Content $ConfigPath -Encoding UTF8 -Raw | ConvertFrom-Json
    $source = [pscustomobject]@{
        id = "_xiaoning_user_provider"
        name = "user-owned-openai-compatible"
        type = "openai_chat_completion"
        api_base = $env:XIAONING_LLM_API_BASE.TrimEnd('/')
        key = @($env:XIAONING_LLM_API_KEY)
        timeout = 120
        vision = $false
    }
    $Config.provider_sources = @($Config.provider_sources | Where-Object { $_.id -ne $source.id }) + $source
    $aliases = @("gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.5-flash", "gemini-3.7-flash")
    $Config.provider = @($Config.provider | Where-Object { $_.id -notin $aliases })
    foreach ($id in $aliases) {
        $Config.provider += [pscustomobject]@{
            id = $id; enable = $true; model = $env:XIAONING_LLM_MODEL
            provider_source_id = $source.id; modalities = @("text"); custom_extra_body = @{}
        }
    }
    if (-not $Config.provider_settings) { $Config.provider_settings = [pscustomobject]@{} }
    $Config.provider_settings.default_provider_id = "gemini-2.5-flash"
    $Config.provider_settings.fallback_chat_models = @("gemini-2.5-pro")
    $Config.plugin_set = $Plugins
    $Config.timezone = "Asia/Shanghai"
    [IO.File]::WriteAllText($ConfigPath, ($Config | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
    Protect-LocalConfig $ConfigPath
}

$NapCatEntry = Join-Path $Root "napcat-runtime\index.js"
if (-not (Test-Path -LiteralPath $NapCatEntry)) {
    if ($CheckOnly) { throw "NapCat runtime is not installed; finish QQ login first" }
    Start-Process (Join-Path $Root "napcat\NapCatInstaller.exe")
    Write-Host "NapCat installer opened. Finish QQ login and OneBot setup, then rerun setup.ps1."
    exit 2
}

Write-Host "Xiaoning is configured with your own provider. Run .\start_all_services.bat."
