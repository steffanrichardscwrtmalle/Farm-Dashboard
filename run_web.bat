@echo off
title Farm Dashboard
cd /d "%~dp0"

echo.
echo Farm Dashboard
echo ==============
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python was not found on this PC.
  echo Install Python from https://www.python.org/downloads/
  echo Tick "Add Python to PATH" during install.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment ^(first run only^)...
  python -m venv .venv
  if errorlevel 1 (
    echo ERROR: Could not create virtual environment.
    pause
    exit /b 1
  )
  call .venv\Scripts\activate.bat
  pip install -r requirements.txt
  if errorlevel 1 (
    echo ERROR: Could not install dependencies.
    pause
    exit /b 1
  )
)

set PY=.venv\Scripts\python.exe

set PORT=8000
netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
  echo Port %PORT% is already in use.
  echo If the app is already running, open http://127.0.0.1:%PORT% in your browser.
  echo.
  echo Trying port 8080 instead...
  set PORT=8080
)

echo Starting server at http://127.0.0.1:%PORT%
echo.
echo Keep this window open while you use the app.
echo Press Ctrl+C to stop the server.
echo.

start "" "http://127.0.0.1:%PORT%"
"%PY%" -m uvicorn app.main:app --reload --host 127.0.0.1 --port %PORT%

echo.
echo Server stopped.
pause
