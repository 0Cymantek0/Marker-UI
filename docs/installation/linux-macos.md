# Linux & macOS Setup Guide

This guide covers setting up and running Marker UI on Linux and macOS environments.

---

## One-Click Launcher (Recommended)

Using the quick-start launcher script is the recommended method to run Marker UI. Marker UI provides a shell launcher script (`start.sh`) that validates Python and Node.js environments, installs dependencies, chooses available ports, and boots the backend and frontend.

### Quick Start
1. Ensure the script is executable:
   ```bash
   chmod +x start.sh
   ```
2. Execute the launcher:
   ```bash
   ./start.sh
   ```
3. Open the frontend URL printed by the launcher. It is usually `http://127.0.0.1:5173`.

---

## Manual Installation

If you prefer to configure components manually:

### 1. Prerequisites
- **Python 3.10+** (with `python3-venv` package on Linux).
- **Node.js 18+** (LTS version recommended).
- **Poppler & Tesseract** (Common document parsing libraries, recommended for general PDF OCR).
  - **macOS (via Homebrew)**: `brew install poppler tesseract`
  - **Ubuntu/Debian**: `sudo apt-get install -y poppler-utils tesseract-ocr`

### 2. Manual Commands
Run these in your terminal from the project root:

```bash
# Setup Backend
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Setup Frontend (Open a new terminal)
cd frontend
pnpm install
pnpm dev
```

---

## Linux & macOS Specific Notes

### 1. Port Collisions
If ports `8000` (FastAPI) or `5173` (Vite) are occupied, the `start.sh` launcher uses the next available ports and prints the chosen URLs. If running manually, verify no other local servers occupy those ports.

### 2. Slow First Startup
The launcher waits for backend and frontend readiness before printing success. If backend startup takes longer than the soft readiness timeout, it keeps waiting while the backend process is still running. Set `MARKER_BACKEND_READY_HARD_TIMEOUT_SECONDS` if automation needs a fixed failure timeout.

### 3. GPU/CUDA Support (Linux)
If you run Marker UI on Linux with an NVIDIA GPU:
1. Ensure the NVIDIA Container Toolkit is installed if using Docker.
2. If running from source, ensure your PyTorch version matches your CUDA environment:
   ```bash
   pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
   ```

### 4. Apple Silicon (macOS M1/M2/M3)
PyTorch on Apple Silicon uses Metal Performance Shaders (MPS) for acceleration. Ensure you are running Python natively (not under Rosetta 2) to leverage full hardware capabilities during PDF segmentation.
