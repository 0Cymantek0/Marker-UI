@echo off
setlocal enabledelayedexpansion
title Marker UI

echo.
echo   ========================================
echo       Marker UI - One-Click Launcher
echo   ========================================
echo.

:: Change to script directory
cd /d "%~dp0"

:: ── Check Python ──────────────────────────────────────────────────
echo [1/6] Checking prerequisites...

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   ERROR: Python not found. Install from https://python.org
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   Python: %PYVER%

where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   ERROR: Node.js not found. Install from https://nodejs.org
    pause
    exit /b 1
)

for /f "tokens=1" %%v in ('node --version 2^>^&1') do set NODEVER=%%v
echo   Node.js: %NODEVER%

:: ── Virtual environment ───────────────────────────────────────────
echo.
echo [2/6] Setting up Python virtual environment...

if not exist ".venv" (
    echo   Creating .venv...
    python -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo   ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
)

:: ── Install Python deps ───────────────────────────────────────────
echo.
echo [3/6] Installing Python dependencies...

if not exist ".venv\installed" (
    .venv\Scripts\pip.exe install -r backend\requirements.txt
    if !ERRORLEVEL! neq 0 (
        echo   WARNING: Full install failed, retrying without [full] extra...
        findstr /v "marker-pdf\[full\]" backend\requirements.txt > backend\requirements_min.txt
        .venv\Scripts\pip.exe install -r backend\requirements_min.txt
        .venv\Scripts\pip.exe install marker-pdf
        del backend\requirements_min.txt
    )
    if !ERRORLEVEL! equ 0 (
        echo. > .venv\installed
        echo   Python dependencies installed
    ) else (
        echo   ERROR: Python dependency installation failed.
        pause
        exit /b 1
    )
) else (
    echo   Python dependencies already installed, skipping check.
)

:: ── Install Node deps ─────────────────────────────────────────────
echo.
echo [4/6] Installing Node.js dependencies...

cd frontend
if not exist "node_modules" (
    call npm install --loglevel error
    if %ERRORLEVEL% neq 0 (
        echo   ERROR: npm install failed
        cd ..
        pause
        exit /b 1
    )
) else (
    echo   node_modules exists, skipping install
)
cd ..

echo   Node.js dependencies installed

:: ── Create data directories ───────────────────────────────────────
echo.
echo [5/6] Creating data directories...

if not exist "data" mkdir data
if not exist "data\uploads" mkdir data\uploads
if not exist "data\output" mkdir data\output

echo   Data directories ready

:: ── Start services ────────────────────────────────────────────────
echo.
echo [6/6] Starting services...
echo.

:: Find a free port for the backend (starting from 8000)
set BACKEND_PORT=8000

:findPort
    netstat -aon | findstr /C:":!BACKEND_PORT!" | findstr "LISTENING" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set /a BACKEND_PORT+=1
        if !BACKEND_PORT! geq 65535 (
            echo   ERROR: No free port found for backend.
            pause
            exit /b 1
        )
        goto findPort
    )

if not "!BACKEND_PORT!"=="8000" (
    echo   Port 8000 is in use, using port !BACKEND_PORT! instead.
)

:: Find a free port for the frontend (starting from 5173)
set FRONTEND_PORT=5173

:findFrontendPort
    netstat -aon | findstr /C:":!FRONTEND_PORT!" | findstr "LISTENING" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set /a FRONTEND_PORT+=1
        if !FRONTEND_PORT! geq 65535 (
            echo   ERROR: No free port found for frontend.
            pause
            exit /b 1
        )
        goto findFrontendPort
    )

if not "!FRONTEND_PORT!"=="5173" (
    echo   Port 5173 is in use, using port !FRONTEND_PORT! instead.
)

echo   Starting backend on http://localhost:!BACKEND_PORT! ...
start /B .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port !BACKEND_PORT! --app-dir backend

:: Wait for backend to start (checks up to 30 times with 1-second delay)
echo   Waiting for backend to start...
set WAIT_COUNT=0
:checkBackendLoop
netstat -aon | findstr /C:":!BACKEND_PORT!" | findstr "LISTENING" >nul 2>&1
if !ERRORLEVEL! equ 0 goto backendStarted
set /a WAIT_COUNT+=1
if !WAIT_COUNT! geq 30 (
    echo   ERROR: Backend failed to start on port !BACKEND_PORT! within 30 seconds.
    pause
    exit /b 1
)
ping -n 2 127.0.0.1 >nul
goto checkBackendLoop

:backendStarted
echo   Backend is listening on port !BACKEND_PORT!.

echo   Starting frontend on http://localhost:!FRONTEND_PORT! ...
cd frontend
start "" /B cmd /c "set BACKEND_PORT=!BACKEND_PORT!&& npm run dev -- --port !FRONTEND_PORT!"
cd ..

ping -n 4 127.0.0.1 >nul

echo.
echo   ========================================
echo   Marker UI is running!
echo.
echo     Frontend:  http://localhost:%FRONTEND_PORT%
echo     Backend:   http://localhost:%BACKEND_PORT%
echo     API Docs:  http://localhost:%BACKEND_PORT%/docs
echo.
echo   Close this window to stop all services.
echo   ========================================
echo.

:: Keep window open - user closes window to stop
pause
