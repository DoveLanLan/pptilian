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
if (-not $env:FRONT_PROXY) { $env:FRONT_PROXY = 'socks5://127.0.0.1:10808' }
if (-not (Test-Path (Join-Path $PWD 'gost.exe')) -and -not (Test-Path (Join-Path $PWD 'bin\gost.exe'))) {
    Write-Host
    Write-Host '  提示: 未找到 gost.exe。勾选"使用 GOST"或使用前置代理时提链会用到它。' -ForegroundColor Yellow
    Write-Host '  请下载 gost v2 放到本目录或 bin\ 目录: https://github.com/ginuerzh/gost/releases/tag/v2.12.0' -ForegroundColor Yellow
}
Write-Host
Write-Host "前置代理: $env:FRONT_PROXY"
Write-Host "Service ready at http://127.0.0.1:$env:PORT"
Write-Host 'Keep this window open while using the app.'
Write-Host
& $python app.py
exit $LASTEXITCODE
