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

function Get-DependencySignature {
    param([string[]]$Paths)
    $parts = @()
    foreach ($path in $Paths) {
        if (Test-Path $path) {
            $hash = Get-FileHash -Algorithm SHA256 -Path $path
            $parts += "$path=$($hash.Hash)"
        }
    }
    return ($parts -join "`n")
}

function Get-LauncherIntEnv {
    param(
        [string]$Name,
        [int]$Default,
        [int]$Minimum = 0
    )

    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $Default
    }

    $parsed = 0
    if ([int]::TryParse($raw, [ref]$parsed) -and $parsed -ge $Minimum) {
        return $parsed
    }

    Write-Host "  WARNING: Ignoring invalid $Name='$raw'; using $Default." -ForegroundColor DarkYellow
    return $Default
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

$isWindowsPlatform = [System.IO.Path]::DirectorySeparatorChar -eq "\"

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
$venvPython = if ($isWindowsPlatform) {
    ".venv\Scripts\python.exe"
} else {
    ".venv/bin/python"
}

if (-not (Test-Path $venvPython)) {
    Write-Host "  ERROR: Virtual environment broken. Delete .venv and re-run." -ForegroundColor Red
    exit 1
}

$venvPip = $venvPython -replace "python\.exe$", "pip.exe"
if ($isWindowsPlatform) {
    $venvPip = ".venv\Scripts\pip.exe"
} else {
    $venvPip = ".venv/bin/pip"
}

Write-Host "  Virtual environment ready" -ForegroundColor Green

# ── Install Python deps ──────────────────────────────────────────────

Write-Host ""
Write-Host "[3/6] Installing Python dependencies..." -ForegroundColor Yellow

$installedFlag = Join-Path ".venv" "installed"
$pythonDepsSignature = Get-DependencySignature @("backend/requirements.txt", "pyproject.toml")
$pythonDepsSignatureFile = Join-Path ".venv" "requirements.sha256"
$pythonDepsInstalled = (Test-Path $installedFlag) -and (Test-Path $pythonDepsSignatureFile) -and ((Get-Content $pythonDepsSignatureFile -Raw).Trim() -eq $pythonDepsSignature.Trim())
if (-not $pythonDepsInstalled) {
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
        & $venvPip check --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ERROR: Python dependency check failed." -ForegroundColor Red
            exit 1
        }
        Set-Content -Path $pythonDepsSignatureFile -Value $pythonDepsSignature -Encoding UTF8
        New-Item -ItemType File -Path $installedFlag -Force | Out-Null
        Write-Host "  Python dependencies installed" -ForegroundColor Green
    } else {
        Write-Host "  ERROR: Python dependency installation failed." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  Python dependencies already current." -ForegroundColor DarkGray
}

# ── Install Node deps ────────────────────────────────────────────────

Write-Host ""
Write-Host "[4/6] Installing Node.js dependencies..." -ForegroundColor Yellow

Push-Location frontend
$nodeDepsSignature = Get-DependencySignature @("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml")
$nodeDepsSignatureFile = Join-Path "node_modules" ".marker-ui-deps.sha256"
$nodeDepsInstalled = (Test-Path "node_modules") -and (Test-Path $nodeDepsSignatureFile) -and ((Get-Content $nodeDepsSignatureFile -Raw).Trim() -eq $nodeDepsSignature.Trim())
if (-not $nodeDepsInstalled) {
    if (-not (Test-Command "pnpm")) {
        if (Test-Command "corepack") {
            corepack enable | Out-Null
        }
    }
    if (-not (Test-Command "pnpm")) {
        Write-Host "  ERROR: pnpm not found. Install pnpm or enable Corepack." -ForegroundColor Red
        Pop-Location
        exit 1
    }
    pnpm install --frozen-lockfile 2>&1 | ForEach-Object {
        if ($_ -match "error|ERR") { Write-Host "  $_" -ForegroundColor Red }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: pnpm install failed" -ForegroundColor Red
        Pop-Location
        exit 1
    }
    Set-Content -Path $nodeDepsSignatureFile -Value $nodeDepsSignature -Encoding UTF8
} else {
    Write-Host "  Node.js dependencies already current." -ForegroundColor DarkGray
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
if (-not (Test-Path "data/logs")) {
    New-Item -ItemType Directory -Path "data/logs" -Force | Out-Null
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

function Write-NewLogLines {
    param(
        [string]$Label,
        [string]$Path,
        [string]$Color = "DarkGray"
    )

    if (-not (Test-Path $Path)) {
        return
    }

    if ($null -eq $script:LogOffsets) {
        $script:LogOffsets = @{}
    }

    $lines = @(Get-Content $Path -ErrorAction SilentlyContinue)
    $last = 0
    if ($script:LogOffsets.ContainsKey($Path)) {
        $last = [int]$script:LogOffsets[$Path]
    }
    if ($lines.Count -lt $last) {
        $last = 0
    }

    for ($i = $last; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        Write-Host "  [$Label] $line" -ForegroundColor $Color
    }

    $script:LogOffsets[$Path] = $lines.Count
}

function Write-LogTail {
    param(
        [string]$Label,
        [string[]]$Paths
    )

    foreach ($path in $Paths) {
        if (Test-Path $path) {
            $lines = Get-Content $path -Tail 20 -ErrorAction SilentlyContinue
            if ($lines) {
                Write-Host ""
                Write-Host "  Last $Label log lines ($path):" -ForegroundColor DarkGray
                $lines | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
            }
        }
    }
}

function Wait-ServiceReady {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Name,
        [string]$Url,
        [int]$SoftTimeoutSeconds = 120,
        [int]$HardTimeoutSeconds = 0,
        [object[]]$LogStreams = @()
    )

    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $warnedAfterSoftTimeout = $false
    $nextProgressAt = 15

    while ($true) {
        foreach ($stream in $LogStreams) {
            Write-NewLogLines -Label $stream.Label -Path $stream.Path -Color $stream.Color
        }

        if ($Process.HasExited) {
            Write-Host "  ERROR: $Name exited before it became ready." -ForegroundColor Red
            return $false
        }

        try {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $Url
            if ($response.StatusCode -eq 200) {
                return $true
            }
        } catch {
            # Service may not have bound its port yet.
        }

        $elapsedSeconds = [int]$stopwatch.Elapsed.TotalSeconds
        if ($elapsedSeconds -ge $nextProgressAt) {
            Write-Host "  Still waiting for $Name ($elapsedSeconds seconds)..." -ForegroundColor DarkGray
            $nextProgressAt += 15
        }

        if (-not $warnedAfterSoftTimeout -and $elapsedSeconds -ge $SoftTimeoutSeconds) {
            Write-Host "  WARNING: $Name is still starting after $SoftTimeoutSeconds seconds." -ForegroundColor DarkYellow
            Write-Host "  Continuing to wait because the process is still running. Press Ctrl+C to stop." -ForegroundColor DarkYellow
            $warnedAfterSoftTimeout = $true
        }

        if ($HardTimeoutSeconds -gt 0 -and $elapsedSeconds -ge $HardTimeoutSeconds) {
            Write-Host "  ERROR: $Name did not become ready within hard timeout $HardTimeoutSeconds seconds." -ForegroundColor Red
            return $false
        }

        Start-Sleep -Seconds 1
    }
}

function Stop-WindowsProcessTree {
    param([int]$ProcessId)

    try {
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue)
        foreach ($child in $children) {
            Stop-WindowsProcessTree -ProcessId ([int]$child.ProcessId)
        }
    } catch {
        # Fall back to taskkill below.
    }

    taskkill.exe /PID $ProcessId /T /F > $null 2>&1
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Stop-ProcessTree {
    param([System.Diagnostics.Process]$Process)

    if (-not $Process) {
        return
    }

    if ($isWindowsPlatform) {
        Stop-WindowsProcessTree -ProcessId $Process.Id
    } elseif (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
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
$frontendHost = "127.0.0.1"

$frontendPort = Find-FreePort -StartPort 5173
if (-not $frontendPort) {
    Write-Host "  ERROR: No free port found for frontend." -ForegroundColor Red
    exit 1
}

if ($frontendPort -ne 5173) {
    Write-Host "  Port 5173 is in use, using port $frontendPort instead." -ForegroundColor DarkYellow
}

# Backend
Write-Host "  Starting backend on http://127.0.0.1:$backendPort ..." -ForegroundColor Cyan
$venvPythonFull = (Resolve-Path $venvPython).Path
$backendOutLog = Join-Path $PSScriptRoot "data\logs\backend.out.log"
$backendErrLog = Join-Path $PSScriptRoot "data\logs\backend.err.log"
$frontendOutLog = Join-Path $PSScriptRoot "data\logs\frontend.out.log"
$frontendErrLog = Join-Path $PSScriptRoot "data\logs\frontend.err.log"

Set-Content -Path $backendOutLog -Value $null
Set-Content -Path $backendErrLog -Value $null
Set-Content -Path $frontendOutLog -Value $null
Set-Content -Path $frontendErrLog -Value $null
$script:LogOffsets = @{}

$backendJob = Start-Process -FilePath $venvPythonFull -ArgumentList "-u", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", $backendPort, "--app-dir", "backend", "--log-level", "info" -PassThru -WindowStyle Hidden -RedirectStandardOutput $backendOutLog -RedirectStandardError $backendErrLog

$backendReadyTimeoutSeconds = Get-LauncherIntEnv -Name "MARKER_BACKEND_READY_TIMEOUT_SECONDS" -Default 120 -Minimum 1
$backendReadyHardTimeoutSeconds = Get-LauncherIntEnv -Name "MARKER_BACKEND_READY_HARD_TIMEOUT_SECONDS" -Default 0 -Minimum 0
Write-Host "  Waiting for backend health check (soft timeout $backendReadyTimeoutSeconds seconds)..." -ForegroundColor DarkGray
if ($backendReadyHardTimeoutSeconds -gt 0) {
    Write-Host "  Backend hard timeout: $backendReadyHardTimeoutSeconds seconds." -ForegroundColor DarkGray
}
if (-not (Wait-ServiceReady -Process $backendJob -Name "Backend" -Url "http://127.0.0.1:$backendPort/api/health" -SoftTimeoutSeconds $backendReadyTimeoutSeconds -HardTimeoutSeconds $backendReadyHardTimeoutSeconds -LogStreams @(
    @{ Label = "backend"; Path = $backendOutLog; Color = "DarkGray" },
    @{ Label = "backend!"; Path = $backendErrLog; Color = "DarkYellow" }
))) {
    Write-LogTail -Label "backend" -Paths @($backendOutLog, $backendErrLog)
    Stop-ProcessTree -Process $backendJob
    exit 1
}
Write-Host "  Backend health check passed on port $backendPort." -ForegroundColor Green

# Frontend - use cmd.exe because pnpm is a .cmd file on Windows, not a real .exe
Write-Host "  Starting frontend on http://${frontendHost}:$frontendPort ..." -ForegroundColor Cyan
if ($isWindowsPlatform) {
    $frontendJob = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "set BACKEND_PORT=$backendPort&& pnpm run dev -- --host $frontendHost --port $frontendPort" -WorkingDirectory "$PWD\frontend" -PassThru -WindowStyle Hidden -RedirectStandardOutput $frontendOutLog -RedirectStandardError $frontendErrLog
} else {
    $frontendJob = Start-Process -FilePath "pnpm" -ArgumentList "run", "dev", "--", "--host", "$frontendHost", "--port", "$frontendPort" -WorkingDirectory "$PWD/frontend" -PassThru -WindowStyle Hidden -RedirectStandardOutput $frontendOutLog -RedirectStandardError $frontendErrLog
}

$frontendReadyTimeoutSeconds = Get-LauncherIntEnv -Name "MARKER_FRONTEND_READY_TIMEOUT_SECONDS" -Default 60 -Minimum 1
$frontendReadyHardTimeoutSeconds = Get-LauncherIntEnv -Name "MARKER_FRONTEND_READY_HARD_TIMEOUT_SECONDS" -Default 180 -Minimum 0
Write-Host "  Waiting for frontend server (soft timeout $frontendReadyTimeoutSeconds seconds)..." -ForegroundColor DarkGray
if (-not (Wait-ServiceReady -Process $frontendJob -Name "Frontend" -Url "http://${frontendHost}:$frontendPort/" -SoftTimeoutSeconds $frontendReadyTimeoutSeconds -HardTimeoutSeconds $frontendReadyHardTimeoutSeconds -LogStreams @(
    @{ Label = "frontend"; Path = $frontendOutLog; Color = "DarkGray" },
    @{ Label = "frontend!"; Path = $frontendErrLog; Color = "DarkYellow" }
))) {
    Write-LogTail -Label "frontend" -Paths @($frontendOutLog, $frontendErrLog)
    Stop-ProcessTree -Process $frontendJob
    Stop-ProcessTree -Process $backendJob
    exit 1
}
Write-Host "  Frontend server ready on port $frontendPort." -ForegroundColor Green

# ── Done ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ===================================================" -ForegroundColor Green
Write-Host "  Marker UI is running!" -ForegroundColor Green
Write-Host ""
Write-Host "    Frontend:  http://${frontendHost}:$frontendPort" -ForegroundColor White
Write-Host "    Backend:   http://127.0.0.1:$backendPort" -ForegroundColor White
Write-Host "    API Docs:  http://127.0.0.1:$backendPort/docs" -ForegroundColor White
Write-Host "    Logs:      $PSScriptRoot\data\logs" -ForegroundColor White
Write-Host ""
Write-Host "  Leave this window open. Press Ctrl+C to stop both services." -ForegroundColor DarkGray
Write-Host "  ===================================================" -ForegroundColor Green
Write-Host ""

# Wait for user to press Ctrl+C
try {
    # Monitor processes - if either dies, report it
    while ($true) {
        Write-NewLogLines -Label "backend" -Path $backendOutLog -Color "DarkGray"
        Write-NewLogLines -Label "backend!" -Path $backendErrLog -Color "DarkYellow"
        Write-NewLogLines -Label "frontend" -Path $frontendOutLog -Color "DarkGray"
        Write-NewLogLines -Label "frontend!" -Path $frontendErrLog -Color "DarkYellow"

        if ($backendJob.HasExited) {
            Write-Host "  Backend process exited unexpectedly." -ForegroundColor Red
            Write-LogTail -Label "backend" -Paths @($backendOutLog, $backendErrLog)
            break
        }
        if ($frontendJob.HasExited) {
            Write-Host "  Frontend process exited unexpectedly." -ForegroundColor Red
            Write-LogTail -Label "frontend" -Paths @($frontendOutLog, $frontendErrLog)
            break
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Write-Host ""
    Write-Host "  Stopping services..." -ForegroundColor Yellow
    Stop-ProcessTree -Process $frontendJob
    Stop-ProcessTree -Process $backendJob
    Write-Host "  Services stopped." -ForegroundColor Green
}
