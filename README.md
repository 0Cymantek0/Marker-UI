# Marker UI

Marker UI is a local-first document-to-agent-context engine: it converts PDFs,
Office files, spreadsheets, slides, images, web/data files, archives, audio, and
experimental video into clean Markdown, manifests, and semantic chunks.
Marker-backed PDF/image/EPUB routes can also render HTML and JSON; native
deterministic routes expose only formats they can actually render.

Run it as a browser app, a scriptable CLI, or a small MCP server for coding
agents. Local parsers and local neural models are the default. Cloud VLM paths
require explicit opt-in; cloud STT providers are listed as planned/deferred
until their adapters ship. Every conversion writes a `.marker.json`
manifest with source metadata, output paths, media type, hashes, assets, and
conversion settings so long outputs can be audited and paged safely.

## System Requirements

| Component | Requirement |
| --- | --- |
| **Python** | 3.11+ |
| **Node** | 22+ (frontend build only) |
| **ffmpeg / ffprobe** | ≥ 5.0 — required for video conversion (`VideoConverter`) and audio preflight (`probe_audio`). The official Docker image installs both via `apt-get install ffmpeg`. |
| **tesseract-ocr** | Required for video frame OCR fallback. Bundled in the Docker image. |
| **RAM** | 8 GB minimum (16 GB recommended for marker + faster-whisper concurrency or large documents) |

If video conversion fails with a `NATIVE_DEPENDENCY_MISSING` error, verify the binaries are on `PATH`:

```bash
command -v ffmpeg && command -v ffprobe
```

## Current Maturity

Marker UI is alpha software. Some surfaces are already useful for local agent
workflows, while others are deliberately exposed as planned or experimental.

| Area | Status |
| --- | --- |
| CLI, MCP, output manifests, and paged output reads | Working alpha |
| PDF/image conversion with Marker-backed Markdown, HTML, JSON, and chunks | Working alpha |
| Native Office/data/archive conversion to Markdown and chunks | Working alpha |
| Semantic chunking | Working alpha with deterministic source refs and lightweight retrieval gate; larger benchmark corpus still planned |
| Audio / voice notes | Partial alpha: local faster-whisper route only; cloud STT, real diarization, and provider comparison are deferred |
| Video | Experimental local demux, transcription, keyframe/OCR provenance |
| VLM image understanding | Opt-in alpha; cloud calls require explicit consent |
| Database migrations | Alembic is the sole schema authority; launch paths migrate to head automatically; incompatible states fail closed |

## 30-Second Agent Demo

```powershell
python -m app.cli self-test --json
python -m app.cli convert ".\paper.pdf" --output-dir ".\out" --json
python -m app.cli mcp start --tool-profile minimal
```

Agents can:

- plan conversions before touching large PDFs or unknown inputs;
- convert local files or guarded public URLs;
- submit long jobs, poll status, and cancel without deleting history;
- page through Markdown or semantic chunks instead of loading huge files;
- inspect output manifests and asset metadata before summarizing;
- keep model-controlled settings writes out of the default MCP profile.

## Why Marker UI?

### 1. Hybrid Conversion Orchestrator
Unlike raw CLI utilities, Marker UI acts as a smart router that selects the most efficient extraction path based on document file types:
- **Deep Neural Path (PDFs & Scans)**: Utilizes the `marker` and `surya` engines for text detection, layout segmentation, and OCR.
- **Fast Deterministic Path (Office, Media & Data formats)**: Routes uploads, local paths, and guarded public URLs for `.docx`, `.xlsx`, legacy `.xls`, `.pptx`, `.msg`, audio, video, `.csv`, `.tsv`, JSON, text, HTML/XML, notebooks, and bounded recursive ZIP child conversion to lightweight local parsers/transcribers. Audio supports local timestamped transcripts, ffprobe media preflight, optional word timestamps, evidence-first notes with source maps, extractive summaries/actions/questions, confidence warnings, and ZIP batch briefs/appendices. Video remains experimental: local audio demux/transcription, bounded keyframe sampling, frame OCR when available, and timestamped frame/audio provenance. Clean deterministic files avoid Marker GPU work where possible.

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
- **CLI & MCP Server**: Headless conversion for Codex, Claude Code, Gemini CLI, OpenCode, Antigravity, and other MCP clients. Agents can plan, convert, and page through outputs without a GUI.
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
- [CLI and MCP Quickstart](docs/usage/cli-and-mcp.md)
- [CLI Guide](docs/usage/cli.md)
- [MCP Guide](docs/usage/mcp.md)
- [LLM Refinement Options](docs/usage/llm-refinement.md)
- [Image Understanding](docs/usage/image-understanding.md)
- [Vision Model Provider Configuration](docs/configuration/vlm-providers.md)
- [History & Storage Lifecycle](docs/usage/history-and-downloads.md)
- [Known Limitations & Maturity](docs/limitations.md)

### Technical Architecture & Reference
- [High-Level Technical Architecture](docs/development/architecture.md)
- [FastAPI Backend Service](docs/development/backend.md)
- [Vite / React Frontend App](docs/development/frontend.md)
- [Task Queue & Executor Backends](docs/development/task-manager.md)
- [Database Models & Migrations](docs/development/database.md)
- [Pytest Verification Suite](docs/development/testing.md)
- [Deterministic Evaluation Harness](docs/development/evaluation.md)
- [Environment Variables Reference](docs/configuration/environment-variables.md)
- [Enterprise Security](docs/enterprise/security.md)
- [Enterprise Deployment](docs/enterprise/deployment.md)
- [Review Status Ledger](docs/planning/review-status-ledger.md)
- [Agent JSON Schemas](docs/reference/json-schemas.md)
- [Agent Error Codes](docs/reference/errors.md)
- [Output Manifest Reference](docs/reference/output-manifest.md)

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
The launcher waits for backend and frontend readiness before printing the URLs. Vite normally serves the client at `http://localhost:5173`; if a port is occupied, the launcher selects the next available port and prints it.

### 2. Docker Compose
Deploy containerized with a single command:
```bash
docker compose up -d
```
The application will be served at `http://localhost:3000` via Nginx.

---

## Testing & Code Quality

The repository currently collects over 2,300 automated backend and frontend tests, covering API endpoints, database operations, worker scheduling, conversion routing, output integrity, manifests, security controls, and UI behavior:

```bash
python -m pytest backend/tests -v
cd frontend
npm test
```

---

## License

This project is licensed under the GPL-3.0 License. See the [LICENSE](LICENSE) file for details.
