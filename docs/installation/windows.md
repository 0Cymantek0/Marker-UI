# Windows Setup Guide

This guide covers setting up and running Marker UI on Windows machines.

---

## One-Click Launcher (Recommended)

Using the quick-start launcher scripts is the recommended method to run Marker UI. Marker UI provides a PowerShell/Batch launcher that automatically checks prerequisites, creates a virtual environment, installs dependencies, and runs both backend and frontend.

### Running with start.bat
1. Double-click `start.bat` in the root folder (or run `.\start.bat` in Command Prompt).
2. It will:
   - Check if **Python** and **Node.js** are installed.
   - Create a Python `.venv` if it doesn't exist.
   - Install backend requirements from `backend/requirements.txt`.
   - Install frontend npm packages.
   - Boot up the Uvicorn backend on port `8000` and the Vite dev server on port `5173`.
   - Wait for both services to become ready, then print the URLs to open.

If backend startup takes longer than the soft readiness timeout, the launcher keeps waiting while the backend process is still running. Set `MARKER_BACKEND_READY_HARD_TIMEOUT_SECONDS` if you need a fixed failure timeout for automation.

### Running with start.ps1
If you prefer PowerShell, you can run:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\start.ps1
```

---

## Manual Installation

If you prefer to configure components manually:

### 1. Prerequisites
- **Python 3.10+** (Ensure "Add Python to PATH" is checked during installation).
- **Node.js 18+** (LTS version recommended, with Corepack/pnpm available).
- **C++ Build Tools** (Sometimes required by Python packages compiling C extensions: e.g. [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)).

### 2. Manual Commands
Run these in PowerShell from the project root:

```powershell
# Setup Backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
uvicorn app.main:app --reload --port 8000

# Setup Frontend (Open a new terminal)
cd frontend
pnpm install
pnpm dev
```

---

## Windows Specific Troubleshooting

### 1. Execution Policy Errors
If PowerShell blocks `start.ps1`, run:
```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
```

### 2. Slow First Startup
The first startup can take longer while Python packages, database tables, or model metadata initialize. The launcher prints progress and continues waiting as long as the backend process is alive.

The launcher records hashes for `backend/requirements.txt`, `pyproject.toml`, `frontend/package.json`, and `pnpm-lock.yaml`. If any dependency input changes, it refreshes the matching Python or Node environment instead of blindly reusing the old install.

### 3. Path Length Issues
Windows has a 260-character path length limit. If python package downloads or model weights downloads fail with path errors, enable long paths:
1. Search "Registry Editor" on Windows.
2. Navigate to `HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\FileSystem`.
3. Set `LongPathsEnabled` to `1`.

### 4. Path Escaping in Settings
When using the **Local Absolute Paths** feature in the web app, use forward slashes `/` or double backslashes `\\` to avoid escaping issues:
- **Correct**: `C:/path/to/document.pdf`
- **Correct**: `C:\\path\\to\\document.pdf`
- **Incorrect**: `C:\path\to\document.pdf`
