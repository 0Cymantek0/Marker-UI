<#
.SYNOPSIS
    Marker UI - One-click launcher (PowerShell)
.DESCRIPTION
    Checks Python 3.10+ and Node 18+, installs dependencies,
    creates a virtual environment, and starts both backend and frontend.
.EXAMPLE
    .\start.ps1
#>

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "  _  _  _  _  _  _  _  _  _  _  _  _  _  _  _  _  _  _" -ForegroundColor DarkGray
Write-Host " |                                                    |" -ForegroundColor DarkGray
Write-Host " |          Marker UI - One-Click Launcher            |" -ForegroundColor Cyan
Write-Host " |                                                    |" -ForegroundColor DarkGray
Write-Host "  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -" -ForegroundColor DarkGray
Write-Host ""

# ── Utility ──────────────────────────────────────────────────────────

function Test-Command {
    param([string]$Name)
    try { Get-Command $Name -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

function Get-PythonCmd {
    # Prefer python3, fall back to python
    foreach ($cmd in @("python", "python3", "py")) {
        if (Test-Command $cmd) {
            $ver = & $cmd --version 2>&1
            if ($ver -match "3\.(\d+)") {
                $minor = [int]$Matches[1]
                if ($minor -ge 10) { return $cmd }
            }
        }
    }
    return $null
}


# ── Check prerequisites ──────────────────────────────────────────────

Write-Host "[1/6] Checking prerequisites..." -ForegroundColor Yellow

$pythonCmd = Get-PythonCmd
if (-not $pythonCmd) {
    Write-Host "  ERROR: Python 3.10+ not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}
$pyVer = & $pythonCmd --version 2>&1
Write-Host "  Python: $pyVer" -ForegroundColor Green

if (-not (Test-Command "node")) {
    Write-Host "  ERROR: Node.js not found. Install from https://nodejs.org" -ForegroundColor Red
    exit 1
}
$nodeVer = node --version
Write-Host "  Node.js: $nodeVer" -ForegroundColor Green

# ── Virtual environment ──────────────────────────────────────────────

Write-Host ""
Write-Host "[2/6] Setting up Python virtual environment..." -ForegroundColor Yellow

if (-not (Test-Path ".venv")) {
    Write-Host "  Creating .venv..." -ForegroundColor DarkGray
    & $pythonCmd -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: Failed to create virtual environment" -ForegroundColor Red
        exit 1
    }
}

# Activate venv
$venvPython = if ($IsWindows -or $env:OS -match "Windows") {
    ".venv\Scripts\python.exe"
} else {
    ".venv/bin/python"
}

if (-not (Test-Path $venvPython)) {
    Write-Host "  ERROR: Virtual environment broken. Delete .venv and re-run." -ForegroundColor Red
    exit 1
}

$venvPip = $venvPython -replace "python\.exe$", "pip.exe"
if ($IsWindows -or $env:OS -match "Windows") {
    $venvPip = ".venv\Scripts\pip.exe"
} else {
    $venvPip = ".venv/bin/pip"
}

Write-Host "  Virtual environment ready" -ForegroundColor Green

# ── Install Python deps ──────────────────────────────────────────────

Write-Host ""
Write-Host "[3/6] Installing Python dependencies..." -ForegroundColor Yellow

$installedFlag = Join-Path ".venv" "installed"
if (-not (Test-Path $installedFlag)) {
    Write-Host "  Installing dependencies (first run may take a while)..." -ForegroundColor DarkGray
    & $venvPip install -r backend/requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  WARNING: Full install failed, retrying without [full] extra..." -ForegroundColor DarkYellow
        
        $tempReqs = Join-Path "backend" "requirements_min.txt"
        Get-Content backend/requirements.txt | Where-Object {
            $_ -notmatch "marker-pdf\[full\]"
        } | Set-Content $tempReqs
        
        & $venvPip install -r $tempReqs --quiet
        $minInstallStatus = $LASTEXITCODE
        
        if (Test-Path $tempReqs) { Remove-Item $tempReqs -Force }
        
        if ($minInstallStatus -eq 0) {
            & $venvPip install marker-pdf --quiet
        }
    }
    
    if ($LASTEXITCODE -eq 0) {
        New-Item -ItemType File -Path $installedFlag -Force | Out-Null
        Write-Host "  Python dependencies installed" -ForegroundColor Green
    } else {
        Write-Host "  ERROR: Python dependency installation failed." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  Python dependencies already installed, skipping check." -ForegroundColor DarkGray
}

# ── Install Node deps ────────────────────────────────────────────────

Write-Host ""
Write-Host "[4/6] Installing Node.js dependencies..." -ForegroundColor Yellow

Push-Location frontend
if (-not (Test-Path "node_modules")) {
    npm install --loglevel error 2>&1 | ForEach-Object {
        if ($_ -match "error|ERR") { Write-Host "  $_" -ForegroundColor Red }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: npm install failed" -ForegroundColor Red
        Pop-Location
        exit 1
    }
} else {
    Write-Host "  node_modules exists, skipping install" -ForegroundColor DarkGray
}
Pop-Location

Write-Host "  Node.js dependencies installed" -ForegroundColor Green

# ── Create data directories ──────────────────────────────────────────

Write-Host ""
Write-Host "[5/6] Creating data directories..." -ForegroundColor Yellow

@("data", "data/uploads", "data/output") | ForEach-Object {
    if (-not (Test-Path $_)) {
        New-Item -ItemType Directory -Path $_ -Force | Out-Null
    }
}
Write-Host "  Data directories ready" -ForegroundColor Green

# ── Start services ───────────────────────────────────────────────────

Write-Host ""
Write-Host "[6/6] Starting services..." -ForegroundColor Yellow
Write-Host ""

function Find-FreePort {
    param([int]$StartPort = 8000)
    $port = $StartPort
    while ($port -lt 65535) {
        $inUse = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if (-not $inUse) { return $port }
        $port++
    }
    return $null
}

function Wait-BackendReady {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$Port,
        [int]$TimeoutSeconds = 120
    )

    $healthUrl = "http://127.0.0.1:$Port/api/health"
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

    while ($stopwatch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if ($Process.HasExited) {
            Write-Host "  ERROR: Backend exited before it became healthy." -ForegroundColor Red
            return $false
        }

        try {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $healthUrl
            if ($response.StatusCode -eq 200) {
                return $true
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }

    Write-Host "  ERROR: Backend did not pass health check on port $Port within $TimeoutSeconds seconds." -ForegroundColor Red
    return $false
}

$backendPort = Find-FreePort -StartPort 8000
if (-not $backendPort) {
    Write-Host "  ERROR: No free port found for backend." -ForegroundColor Red
    exit 1
}

if ($backendPort -ne 8000) {
    Write-Host "  Port 8000 is in use, using port $backendPort instead." -ForegroundColor DarkYellow
}

$env:BACKEND_PORT = $backendPort

$frontendPort = Find-FreePort -StartPort 5173
if (-not $frontendPort) {
    Write-Host "  ERROR: No free port found for frontend." -ForegroundColor Red
    exit 1
}

if ($frontendPort -ne 5173) {
    Write-Host "  Port 5173 is in use, using port $frontendPort instead." -ForegroundColor DarkYellow
}

# Backend
Write-Host "  Starting backend on http://localhost:$backendPort ..." -ForegroundColor Cyan
$venvPythonFull = (Resolve-Path $venvPython).Path
$backendJob = Start-Process -FilePath $venvPythonFull -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", $backendPort, "--app-dir", "backend" -PassThru -WindowStyle Hidden

Write-Host "  Waiting for backend health check (up to 120 seconds)..." -ForegroundColor DarkGray
if (-not (Wait-BackendReady -Process $backendJob -Port $backendPort -TimeoutSeconds 120)) {
    if (-not $backendJob.HasExited) {
        Stop-Process -Id $backendJob.Id -Force -ErrorAction SilentlyContinue
    }
    exit 1
}
Write-Host "  Backend health check passed on port $backendPort." -ForegroundColor Green

# Frontend - use cmd.exe because npm is a .cmd file on Windows, not a real .exe
Write-Host "  Starting frontend on http://localhost:$frontendPort ..." -ForegroundColor Cyan
if ($IsWindows -or $env:OS -match "Windows") {
    $frontendJob = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "set BACKEND_PORT=$backendPort&& npm run dev -- --port $frontendPort" -WorkingDirectory "$PWD\frontend" -PassThru -WindowStyle Hidden
} else {
    $frontendJob = Start-Process -FilePath "npm" -ArgumentList "run", "dev", "--", "--port", "$frontendPort" -WorkingDirectory "$PWD/frontend" -PassThru -WindowStyle Hidden
}

Start-Sleep -Seconds 3

# ── Done ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ===================================================" -ForegroundColor Green
Write-Host "  Marker UI is running!" -ForegroundColor Green
Write-Host ""
Write-Host "    Frontend:  http://localhost:$frontendPort" -ForegroundColor White
Write-Host "    Backend:   http://localhost:$backendPort" -ForegroundColor White
Write-Host "    API Docs:  http://localhost:$backendPort/docs" -ForegroundColor White
Write-Host ""
Write-Host "  Press Ctrl+C to stop both services." -ForegroundColor DarkGray
Write-Host "  ===================================================" -ForegroundColor Green
Write-Host ""

# Wait for user to press Ctrl+C
try {
    # Monitor processes - if either dies, report it
    while ($true) {
        if ($backendJob.HasExited) {
            Write-Host "  Backend process exited unexpectedly." -ForegroundColor Red
            break
        }
        if ($frontendJob.HasExited) {
            Write-Host "  Frontend process exited unexpectedly." -ForegroundColor Red
            break
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Write-Host ""
    Write-Host "  Stopping services..." -ForegroundColor Yellow
    if (-not $backendJob.HasExited) { Stop-Process -Id $backendJob.Id -Force -ErrorAction SilentlyContinue }
    if (-not $frontendJob.HasExited) { Stop-Process -Id $frontendJob.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "  Services stopped." -ForegroundColor Green
}
