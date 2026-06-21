# Marker UI

Marker UI is a local-first, production-grade web application and orchestrator designed to convert document types (PDFs, Word documents, spreadsheets, slides, and images) into clean, layout-aware, search-optimized Markdown, HTML, or JSON.

By wrapping deep-learning neural models with lightweight deterministic parsers and an intelligent VLM pipeline, Marker UI offers a seamless, high-throughput document conversion platform on your own hardware.

---

## Why Marker UI? The Core Strengths

### 1. Hybrid Conversion Orchestrator
Unlike raw CLI utilities, Marker UI acts as a smart router that selects the most efficient extraction path based on document file types:
- **Deep Neural Path (PDFs & Scans)**: Utilizes the `marker` and `surya` engines for text detection, layout segmentation, and OCR.
- **Fast Deterministic Path (Office & Data formats)**: Routes `.docx`, `.xlsx`, and `.pptx` files to lightweight, model-free Python parsers (`mammoth`, `openpyxl`, `python-pptx`), achieving zero-GPU usage and sub-second conversions for clean files.

### 2. Multi-GPU Parallel Worker Pool
Built for speed and hardware scaling:
- **ThreadExecutorBackend**: Default for CPU-only and single-GPU setups, running in-process conversions concurrently.
- **ProcessExecutorBackend**: Automatically scales across workstation hardware, spawning GPU-pinned Python worker processes (one per detected CUDA device) via `multiprocessing.Pool` to achieve maximum parallel throughput.

### 3. Image Understanding VLM Pipeline
Close the visual context gap for downstream RAG pipelines:
- **Visual Classification**: Classifies extracted figures into 17 distinct types (flowcharts, tables, equations, screenshots, decorative, etc.).
- **Lossless Extraction**:
  - **Charts & Graphs** -> Clean Markdown data tables.
  - **Diagrams** -> Interactive `Mermaid.js` diagrams.
  - **Equations** -> Valid, rendered `LaTeX` blocks.
  - **Photos & Screenshots** -> Context-aware alt-text descriptions.
  - **Decorative Elements** -> Filtered and omitted automatically.

### 4. Smart Image Router & Perceptual Dedup
Optimizes token usage and third-party API costs:
- **Layout-Aware Router**: Classifies layout blocks using local Surya models to route text-heavy graphics to local OCR and complex layouts to the VLM.
- **Perceptual Hashing (aHash)**: Deduplicates identical images (e.g. logos or repeating backgrounds) across pages to prevent duplicate VLM/OCR processing.

### 5. Local-First Privacy & Security
- **Credentials Encryption**: LLM keys (OpenAI, Gemini, Claude, Ollama, Azure, Vertex) are encrypted at rest using AES-128 Fernet cryptography.
- **Response Masking**: Sensitive keys are automatically masked before they reach browser logs or frontend stores.
- **Zero-Latency Local Paths**: Supply absolute file paths on the server filesystem to bypass HTTP upload overhead for multi-gigabyte files.

---

## Key Features

- **Drag-and-Drop Zone**: Intuitive React 19 interface supporting multiple simultaneous file uploads.
- **Live Execution Console**: Watch stages progress (`Extracting Text`, `Running Layout Models`, `OCR Processing`) in real-time, streamed via Server-Sent Events (SSE).
- **Onboarding Page**: Clear, real-time download and speed progress indicators during the first-time setup of local neural weights.
- **System Maintenance**: Built-in self-healing checkers to verify weight file integrity and reset pipelines.

---

## Documentation Index

Our documentation is structured to help you get started quickly or dive deep into the codebase:

### Getting Started & Installation
- [Getting Started Guide](docs/getting-started.md)
- [Docker Compose Deployment](docs/installation/docker.md)
- [Running from Source (Manual Setup)](docs/installation/source.md)
- [Windows Setup Guide](docs/installation/windows.md)
- [Linux & macOS Setup Guide](docs/installation/linux-macos.md)

### Detailed Usage & Configuration
- [Converting Documents & Formats](docs/usage/convert-documents.md)
- [Supported Output Formats](docs/usage/output-formats.md)
- [Using Local Absolute Paths](docs/usage/local-file-paths.md)
- [LLM Refinement Options](docs/usage/llm-refinement.md)
- [Image Understanding & VLM Config](docs/usage/image-understanding.md)
- [History & Storage Lifecycle](docs/usage/history-and-downloads.md)

### Technical Architecture & Reference
- [High-Level Technical Architecture](docs/development/architecture.md)
- [FastAPI Backend Service](docs/development/backend.md)
- [Vite / React Frontend App](docs/development/frontend.md)
- [Task Queue & Executor Backends](docs/development/task-manager.md)
- [Database Models & Migrations](docs/development/database.md)
- [Pytest Verification Suite](docs/development/testing.md)
- [Environment Variables Reference](docs/configuration/environment-variables.md)

---

## Quick Start

### 1. Unified Launcher Scripts (Recommended)
Marker UI provides unified launchers that automatically prepare a virtual environment, verify Python/Node.js dependencies, install packages, and boot both servers:

- **Linux & macOS**:
  ```bash
  chmod +x start.sh
  ./start.sh
  ```
- **Windows (Command Prompt)**:
  ```cmd
  start.bat
  ```
- **Windows (PowerShell)**:
  ```powershell
  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
  .\start.ps1
  ```
Vite will serve the client at `http://localhost:5173`.

### 2. Docker Compose
Deploy containerized with a single command:
```bash
docker compose up -d
```
The application will be served at `http://localhost:3000` via Nginx.

---

## Testing & Code Quality

The backend includes a comprehensive suite of over 540 automated tests validating API endpoints, database operations, worker pool IPC scheduling, and encryption integrity:

```bash
cd backend
python -m pytest tests/ -v
```

---

## License

This project is licensed under the GPL-3.0 License. See the [LICENSE](LICENSE) file for details.
