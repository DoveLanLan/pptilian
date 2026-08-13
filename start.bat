@echo off
setlocal
cd /d "%~dp0"

echo ================================
echo Integrated Tool Service
echo Project directory: %CD%
echo ================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 goto failed
)

set "PYTHON=%CD%\.venv\Scripts\python.exe"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
"%PYTHON%" -c "import flask, requests, socks, curl_cffi, qrcode, PIL, fastapi, uvicorn, pydantic, blinker, httpx, loguru, playwright, pproxy" >nul 2>nul
if errorlevel 1 (
  echo Installing missing dependencies...
  "%PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto failed
) else (
  echo Dependencies already installed. Skipping pip install.
)

if "%CHECK_ONLY%"=="1" (
  echo Snapshot dependencies OK.
  exit /b 0
)

if "%PORT%"=="" set "PORT=5000"
echo.
echo Service ready at http://127.0.0.1:%PORT%
echo Keep this window open while using the app.
echo.
"%PYTHON%" app.py
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" goto failed_code
exit /b 0

:failed_code
echo.
echo Application exited with code %APP_EXIT%.
pause
exit /b %APP_EXIT%

:failed
echo.
echo Startup failed. See the error above.
pause
exit /b 1
