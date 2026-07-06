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
   - Open your browser to `http://localhost:5173`.

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
- **Node.js 18+** (LTS version recommended).
- **C++ Build Tools** (Sometimes required by Python packages compiling C extensions: e.g. [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)).

### 2. Manual Commands
Run these in PowerShell from the project root:

```powershell
# Setup Backend
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Setup Frontend (Open a new terminal)
cd frontend
npm install
npm run dev
```

---

## Windows Specific Troubleshooting

### 1. Execution Policy Errors
If PowerShell blocks `start.ps1`, run:
```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
```

### 2. Path Length Issues
Windows has a 260-character path length limit. If python package downloads or model weights downloads fail with path errors, enable long paths:
1. Search "Registry Editor" on Windows.
2. Navigate to `HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\FileSystem`.
3. Set `LongPathsEnabled` to `1`.

### 3. Path Escaping in Settings
When using the **Local Absolute Paths** feature in the web app, use forward slashes `/` or double backslashes `\\` to avoid escaping issues:
- **Correct**: `C:/path/to/document.pdf`
- **Correct**: `C:\\path\\to\\document.pdf`
- **Incorrect**: `C:\path\to\document.pdf`
