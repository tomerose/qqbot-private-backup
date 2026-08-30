$ErrorActionPreference = "Stop"

$serviceRoot = $PSScriptRoot
$projectRoot = (Resolve-Path (Join-Path $serviceRoot "..\..")).Path
$venv = Join-Path $serviceRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$stateRoot = Join-Path $projectRoot "claude_workspace\state"
$audioRoot = Join-Path $projectRoot "claude_workspace\tts_audio"
$tokenFile = Join-Path $stateRoot "local_tts.token"
$tokenArg = "..\..\claude_workspace\state\local_tts.token"
$audioArg = "..\..\claude_workspace\tts_audio"
$installMarker = Join-Path $venv ".melo-installed"
$stdoutLog = Join-Path $serviceRoot "local-tts.stdout.log"
$stderrLog = Join-Path $serviceRoot "local-tts.stderr.log"
$nltkTaggers = Join-Path $serviceRoot "nltk_data\taggers"
$nltkTagger = Join-Path $nltkTaggers "averaged_perceptron_tagger"
$nltkZip = Join-Path $nltkTaggers "averaged_perceptron_tagger.zip"
$nltkSha256 = "E1F13CF2532DAADFD6F3BC481A49859F0B8EA6432CCDCD83E6A49A5F19008DE9"
$nltkUrl = "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/taggers/averaged_perceptron_tagger.zip"
$authorizedVoice = $false

New-Item -ItemType Directory -Force -Path $stateRoot, $audioRoot | Out-Null

if (-not (Test-Path -LiteralPath $venv)) {
    py -3.11 -m venv $venv
}

if (-not (Test-Path -LiteralPath $installMarker)) {
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $serviceRoot "requirements-melo.txt")
    New-Item -ItemType File -Force -Path $installMarker | Out-Null
}

if (-not (Test-Path -LiteralPath $nltkTagger -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $nltkTaggers | Out-Null
    $validZip = (Test-Path -LiteralPath $nltkZip -PathType Leaf) -and `
        ((Get-FileHash -LiteralPath $nltkZip -Algorithm SHA256).Hash -eq $nltkSha256)
    if (-not $validZip) {
        $download = "$nltkZip.download"
        Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
        & curl.exe -L --fail --retry 2 --output $download $nltkUrl
        if ($LASTEXITCODE -ne 0 -or
            (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $nltkSha256) {
            Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
            throw "Local TTS NLTK resource verification failed"
        }
        Move-Item -LiteralPath $download -Destination $nltkZip -Force
    }
    Expand-Archive -LiteralPath $nltkZip -DestinationPath $nltkTaggers -Force
}

if (-not (Test-Path -LiteralPath $tokenFile)) {
    $tokenBytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($tokenBytes)
    }
    finally {
        $rng.Dispose()
    }
    $token = ([BitConverter]::ToString($tokenBytes) -replace "-", "").ToLowerInvariant()
    [IO.File]::WriteAllText($tokenFile, $token, [Text.UTF8Encoding]::new($false))
}

$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($directory in @($stateRoot, $audioRoot)) {
    & icacls.exe $directory /inheritance:r /grant:r "*$($sid):(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
}
& icacls.exe $tokenFile /inheritance:r /grant:r "*$($sid):F" "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null

$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Output "LOCAL_TTS_ALREADY_RUNNING PID=$($listener.OwningProcess)"
    exit 0
}

$serverArgs = @(
    (Join-Path $serviceRoot "server.py"),
    "--host", "127.0.0.1",
    "--port", "8766",
    "--token-file", $tokenArg,
    "--audio-root", $audioArg
)
if ($authorizedVoice) {
    $serverArgs += "--authorized-voice"
}

$startArgs = @{
    FilePath = $python
    ArgumentList = $serverArgs
    WorkingDirectory = $serviceRoot
    WindowStyle = "Hidden"
    RedirectStandardOutput = $stdoutLog
    RedirectStandardError = $stderrLog
    PassThru = $true
}
$process = Start-Process @startArgs

Write-Output "LOCAL_TTS_STARTED PID=$($process.Id)"
