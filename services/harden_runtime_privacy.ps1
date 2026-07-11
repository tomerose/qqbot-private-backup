param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$projectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\")
$projectPrefix = $projectRoot + "\"
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$unsafePrincipals = @(
    "*S-1-5-11",     # Authenticated Users
    "*S-1-5-32-545", # BUILTIN\Users
    "*S-1-1-0"       # Everyone
)

$targets = @(
    "astrbot\data",
    "claude_workspace",
    "astrbot-startup.stdout.log",
    "astrbot-startup.stderr.log",
    "astrbot\astrbot.log",
    "astrbot\data\logs",
    "claude_workspace\state",
    "claude_workspace\tts_audio",
    "services\local_tts\local-tts.stdout.log",
    "services\local_tts\local-tts.stderr.log"
)

function Assert-ProjectPath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Privacy ACL target escaped project root"
    }
    return $resolved
}

function Set-PrivateAcl([string]$Path) {
    $resolved = Assert-ProjectPath $Path
    if (-not (Test-Path -LiteralPath $resolved)) {
        return
    }
    $item = Get-Item -LiteralPath $resolved -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Privacy ACL target cannot be a reparse point"
    }
    if ($item.PSIsContainer) {
        & icacls.exe $resolved /inheritance:r /grant:r `
            "*$($sid):(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" `
            /remove:g $unsafePrincipals | Out-Null
    }
    else {
        & icacls.exe $resolved /inheritance:r /grant:r `
            "*$($sid):F" "*S-1-5-18:F" "*S-1-5-32-544:F" `
            /remove:g $unsafePrincipals | Out-Null
    }
}

foreach ($relative in $targets) {
    $target = Assert-ProjectPath (Join-Path $projectRoot $relative)
    if (-not (Test-Path -LiteralPath $target)) {
        continue
    }
    Set-PrivateAcl $target
    $rootItem = Get-Item -LiteralPath $target -Force
    if ($rootItem.PSIsContainer) {
        Get-ChildItem -LiteralPath $target -Force -Recurse | ForEach-Object {
            Set-PrivateAcl $_.FullName
        }
    }
}

Write-Output "RUNTIME_PRIVACY_ACL=HARDENED"
