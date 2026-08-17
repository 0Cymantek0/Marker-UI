# Contributing to Marker UI

Thank you for your interest in improving Marker UI! This guide outlines how to submit issues, propose changes, and set up your local development environment.

---

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## How to Contribute

### 1. Reporting Bugs & Requesting Features
- Search existing issues/discussions before opening a new one.
- Use the issue templates to provide as much context as possible.
- If reporting a bug, include details about:
  - Your operating system (Windows, Linux, macOS)
  - Python and Node.js versions
  - Hardware specifications (CPU-only, NVIDIA GPU VRAM, etc.)
  - Relevant logs from Docker or standard terminal outputs.

### 2. Submitting Pull Requests
- Fork the repository and create a new branch from `main`.
- Focus changes on a single feature or bug fix. Avoid large, unrelated refactors in a single PR.
- Ensure all tests pass before submitting.
- Commit messages should be clear and descriptive. Follow conventional commit patterns (e.g. `feat: add ollama model overrides`, `fix: handle spaces in file paths`).
- **No Co-authored-by trailers**: Do not add trailers identifying AI bots or helpers. The commit author should reflect only the human committer.

---

## Local Development Setup

To run Marker UI from source, you need **Python 3.11+** and **Node.js 22+**.

### Backend Setup

1. Navigate to the backend folder and create a virtual environment:
   ```bash
   cd backend
   python -m venv .venv
   ```
2. Activate the virtual environment:
   - **Linux/macOS**: `source .venv/bin/activate`
   - **Windows (PowerShell)**: `.venv\Scripts\Activate.ps1`
   - **Windows (CMD)**: `.venv\Scripts\activate.bat`
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy the environment template and configure:
   ```bash
   cp .env.example .env
   ```
5. Run the FastAPI development server:
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

### Frontend Setup

1. Navigate to the frontend folder:
   ```bash
   cd frontend
   ```
2. Install dependencies (we recommend `pnpm`):
   ```bash
   pnpm install
   ```
3. Run the Vite development server:
   ```bash
   pnpm dev
   ```
4. Open your browser and navigate to `http://localhost:5173`.

---

## Coding Standards

### Python (Backend)
- Use PEP 8 styling conventions.
- All public functions and route handlers must have type hints.
- Write docstrings for new services and utilities.
- Keep components decoupled: keep database schemas, API routes, and background tasks separate.

### TypeScript / React (Frontend)
- Use functional React components with hooks.
- Strict TypeScript types/interfaces are required for all component props and state.
- Component layouts must be responsive and follow the high-density, low-clutter theme guidelines.

---

## Running Tests

We expect new features or bug fixes to have accompanying tests.

Run the test suite using `pytest`:
```bash
cd backend
python -m pytest tests/ -v
```

Before committing, make sure:
1. No existing tests are broken.
2. New code paths (encryption, database updates, models) are fully covered.
3. Tests run without hanging.
