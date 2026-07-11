# Docker Deployment Guide

The fastest way to deploy Marker UI is via Docker Compose. The container bundles the FastAPI backend, the React frontend, and an Nginx reverse proxy into a single deployment.

---

## Prerequisites
- **Docker** and **Docker Compose** installed.
- Minimum **8 GB RAM** (16 GB recommended for GPU acceleration or large documents).
- **10 GB free disk** for the image and first-run model downloads.
- Active internet connection (required on first launch to download model weights).

## Native Runtime Dependencies

The container installs these system binaries at build time:

| Binary | Version | Used by |
|--------|---------|---------|
| `ffmpeg` / `ffprobe` | 5.x (Debian 12) | Video conversion (demux, keyframe extraction, audio probe) |
| `tesseract-ocr` | 5.x | Frame OCR for video, fallback OCR for images |

If video conversion fails with `NATIVE_DEPENDENCY_MISSING`, verify the binaries
are present inside the container:

```bash
docker exec <container> sh -lc 'command -v ffmpeg && command -v ffprobe'
```

Both commands must return a path. If not, rebuild the image from the latest
Dockerfile.

---

## Quick Start

1. Clone this repository and navigate to the root directory.
2. Run the compose environment:
   ```bash
   docker compose up -d
   ```
3. Open `http://localhost:3000` in your web browser.

---

## How It Works

- **Reverse Proxy**: Nginx runs on port `80` inside the container and is mapped to port `3000` on your host. It routes `/api/*` requests to the FastAPI backend and serves static React frontend assets for other routes.
- **Model Storage & Persistence**: All model weights, local SQLite databases, uploads, and outputs are stored inside `/app/backend/data`. This directory is backed up to a persistent Docker named volume called `marker-data`.
- **Health Checks**: The service runs a health check against `/api/health` every 30 seconds to ensure the API and task systems are responding.

---

## Configuration

You can customize port bindings and host addresses inside `docker-compose.yml`:
```yaml
ports:
  - "127.0.0.1:3000:80"  # Change to "3000:80" to make it accessible across your LAN
```

### Viewing Logs

Since model weight downloads happen automatically in the background during the first startup, you can monitor download speed and progress:
```bash
docker compose logs -f marker-ui
```

### Cleaning or Resetting Data

If you need to purge the local database and remove cached model weights:
```bash
# Stop the container
docker compose down

# Remove the persistent volume
docker volume rm marker_marker-data
```
