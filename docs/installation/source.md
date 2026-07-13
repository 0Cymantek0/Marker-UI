# Running from Source Guide

Running Marker UI from source is useful if you want to develop features, inspect backend logs in real-time, or build custom layout handlers.

---

## Prerequisites
- **Python 3.11+** (with `pip` and `venv`).
- **Node.js 18+** (with `npm` or `pnpm`).
- **pnpm** (preferred for frontend dependencies, install via `npm i -g pnpm`).

---

## Backend Setup

1. Navigate to the `backend` directory:
   ```bash
   cd backend
   ```
2. Create and activate a Python virtual environment:
   - **Linux/macOS**:
     ```bash
     python -m venv .venv
     source .venv/bin/activate
     ```
   - **Windows (PowerShell)**:
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     ```
3. Install required Python packages:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy the environment configuration (resides at the project root):
   - **Linux/macOS**:
     ```bash
     cp ../.env.example .env
     ```
   - **Windows**:
     ```powershell
     cp ..\.env.example .env
     ```
5. Run the development server with hot-reload enabled:
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

The backend API is now running at `http://127.0.0.1:8000`.

---

## Frontend Setup

1. Open a new terminal window and navigate to the `frontend` directory:
   ```bash
   cd frontend
   ```
2. Install Node dependencies:
   ```bash
   pnpm install
   ```
3. Start the Vite React development server:
   ```bash
   pnpm dev
   ```

The frontend interface is now running at `http://localhost:5173`. Vite will proxy API requests to `http://127.0.0.1:8000` automatically.

---

## Verifying the Setup

To verify everything is working:
1. Open `http://localhost:5173` in your browser.
2. If this is the first launch, the page will redirect to `/onboarding` to download model weights.
3. Check the backend terminal console for detailed weight download progress logs.
