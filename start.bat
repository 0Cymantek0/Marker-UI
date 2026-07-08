@echo off
setlocal

cd /d "%~dp0"

where powershell >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: PowerShell not found. Cannot start Marker UI.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo Marker UI launcher exited with code %EXITCODE%.
    pause
)

exit /b %EXITCODE%
