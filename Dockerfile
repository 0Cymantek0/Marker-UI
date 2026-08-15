# ──────────────────────────────────────────────────────────────────────
# Marker UI - Single-container Docker image
# Runs: uvicorn (backend:8000) + nginx (frontend:80, proxies /api→8000)
# ──────────────────────────────────────────────────────────────────────

# ---- Stage 1: Frontend build ----
FROM node:22-slim AS frontend-build

RUN corepack enable && corepack prepare pnpm@9.15.4 --activate
WORKDIR /app
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ .
RUN pnpm run build

# ---- Stage 2: Runtime (backend + nginx) ----
FROM python:3.11-slim

# System deps: nginx, supervisord, OCR/video libs, curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    ffmpeg \
    tesseract-ocr \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
# Pre-install torch from the variant-specific index BEFORE marker-pdf so pip
# sees torch satisfied and skips the 14 nvidia-*-cu12 CUDA wheels (~5 GB).
#   VARIANT=cpu (default) → CPU torch from pytorch/whl/cpu (~250 MB)
#   VARIANT=gpu           → CUDA torch from pytorch/whl/cu126 (~2.5 GB)
ARG VARIANT=cpu
ARG COMMIT_SHA=""
ENV MARKER_COMMIT_SHA=${COMMIT_SHA}
COPY backend/requirements.txt backend/requirements-cpu.txt backend/requirements-gpu.txt backend/requirements-cpu.lock backend/requirements-gpu.lock ./backend/
RUN if [ "$VARIANT" = "gpu" ]; then \
        pip install --no-cache-dir \
            --index-url https://download.pytorch.org/whl/cu126 \
            --extra-index-url https://pypi.org/simple \
            -r backend/requirements-gpu.lock; \
    else \
        pip install --no-cache-dir \
            --index-url https://download.pytorch.org/whl/cpu \
            --extra-index-url https://pypi.org/simple \
            -r backend/requirements-cpu.lock; \
    fi

# Backend source
COPY backend/ ./backend/

# Frontend built assets → nginx
COPY --from=frontend-build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# Remove default nginx config that conflicts
RUN rm -f /etc/nginx/sites-enabled/default

# Data dirs (backend CWD is /app/backend, data/ resolves there)
# huggingface/ holds Surya + faster-whisper model weights relocated via HF_HOME
# so they persist under the marker-data volume instead of /root/.cache.
RUN mkdir -p /app/backend/data/uploads /app/backend/data/output /app/backend/data/huggingface

# Supervisord config to manage both processes.
# Runtime directories created explicitly before chown chain below.
RUN mkdir -p /var/log/supervisor /run/supervisor
COPY supervisord.conf /etc/supervisor/conf.d/marker-ui.conf

# Create non-root user for application processes
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
    && chown -R appuser:appuser /app \
    && chown -R appuser:appuser /var/log/supervisor /run/supervisor

EXPOSE 80

WORKDIR /app/backend

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost/api/health || exit 1

# supervisord runs as root (needs it to manage nginx on port 80)
# but child processes (uvicorn) drop to appuser via supervisord.conf
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/marker-ui.conf", "-n"]
