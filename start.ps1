$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host '================================'
Write-Host 'Integrated Tool Service'
Write-Host "Project directory: $PWD"
Write-Host '================================'
Write-Host

$python = Join-Path $PWD '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'Creating virtual environment...'
    py -3 -m venv .venv
}

& $python -c 'import flask, requests, socks, curl_cffi, qrcode, PIL, fastapi, uvicorn, pydantic, blinker, httpx, loguru, playwright, pproxy' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing missing dependencies...'
    & $python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
} else {
    Write-Host 'Dependencies already installed. Skipping pip install.'
}

if ($env:CHECK_ONLY -eq '1') {
    Write-Host 'Snapshot dependencies OK.'
    exit 0
}

if (-not $env:PORT) { $env:PORT = '5000' }
Write-Host
Write-Host "Service ready at http://127.0.0.1:$env:PORT"
Write-Host 'Keep this window open while using the app.'
Write-Host
& $python app.py
exit $LASTEXITCODE
