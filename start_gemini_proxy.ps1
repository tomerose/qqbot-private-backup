$ErrorActionPreference = 'Stop'
$env:VERTEX_PROJECT = 'solar-modem-496213-f5'
$env:VERTEX_LOCATION = 'global'
$env:GOOGLE_APPLICATION_CREDENTIALS = Join-Path $PSScriptRoot 'astrbot\data\vertex-key.json'
$python = (Get-Command python -ErrorAction Stop).Source
& $python -u (Join-Path $PSScriptRoot 'gemini-proxy.py')
