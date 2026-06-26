# Enterprise Deployment

This page covers production-oriented deployment shapes for the GUI, CLI, REST
API, and MCP server.

## Local Workstation

Use the launcher scripts for interactive local use:

```powershell
.\start.ps1
```

For headless CLI/MCP work from source:

```powershell
cd backend
python -m app.cli self-test --json
python -m app.cli mcp
```

Recommended local policy:

```powershell
$env:MARKER_WORKSPACE_ROOTS="C:\path\to\documents"
$env:MARKER_OUTPUT_ROOT="C:\path\to\outputs"
```

## Docker Compose

Basic launch:

```bash
docker compose up -d
```

The compose stack serves the web app through Nginx and persists backend data in
the configured Docker volume. For LAN or server use, add explicit security
environment:

```yaml
environment:
  - MARKER_HOST=0.0.0.0
  - MARKER_PORT=8000
  - MARKER_MAX_UPLOAD_SIZE_MB=200
  - MARKER_REST_AUTH_TOKEN=${MARKER_REST_AUTH_TOKEN}
  - MARKER_REST_AUTH_SCOPES=capabilities:read jobs:read jobs:write outputs:read settings:read
  - MARKER_MCP_AUTH_TOKEN=${MARKER_MCP_AUTH_TOKEN}
  - MARKER_WORKSPACE_ROOTS=/workspace/documents
  - MARKER_OUTPUT_ROOT=/workspace/output
  - MARKER_SOURCE_URL_ALLOWLIST=docs.example.com,*.trusted.example
```

Bind public ports deliberately:

```yaml
ports:
  - "127.0.0.1:3000:80"
```

Use a reverse proxy in front of Docker when serving remote users.

## GPU Hosts

Marker can use GPU-backed neural models for PDF/OCR work. Deployment notes:

- install compatible NVIDIA drivers and container runtime on GPU hosts;
- mount persistent model/cache storage so first-run downloads are not repeated;
- keep `MARKER_PRELOAD_MODELS=false` for faster CLI/MCP startup unless warm
  workers are required;
- use process worker mode only after verifying GPU memory headroom;
- run `python -m app.cli doctor --json` before enabling traffic.

CPU-only deployments still handle deterministic Office/data/text/audio paths,
but large scanned PDFs need GPU capacity for practical latency.

## Reverse Proxy Auth

For remote HTTP:

- terminate TLS at the proxy;
- protect `/`, `/api/*`, and `/mcp`;
- forward `Authorization` when using Marker bearer auth;
- set REST and MCP tokens;
- do not bind backend services directly to untrusted interfaces.

Example Nginx shape:

```nginx
location /api/ {
  proxy_set_header Authorization $http_authorization;
  proxy_set_header X-Request-ID $request_id;
  proxy_pass http://marker-backend:8000/api/;
}

location /mcp {
  proxy_set_header Authorization $http_authorization;
  proxy_set_header X-Request-ID $request_id;
  proxy_pass http://marker-backend:8000/mcp;
}
```

## Durable Queue

The default in-process queue is enough for local use. Enable SQLite-backed queue
recovery when you need pending/leased job recovery across service restarts:

```powershell
$env:MARKER_QUEUE_BACKEND="sqlite"
```

The durable queue stores queue metadata and job events in the same database as
job history. It can recover pending jobs and expired processing leases.

## Health, Readiness, Metrics, Version

REST endpoints:

- `/api/health`
- `/api/healthz`
- `/api/readyz`
- `/api/version`
- `/api/metrics` when `MARKER_ENABLE_METRICS=true`

Version metadata can be injected:

```powershell
$env:MARKER_VERSION="0.3.0"
$env:MARKER_COMMIT_SHA="commit-sha"
$env:MARKER_ENABLE_METRICS="true"
```

Use readiness for traffic gating and health for process liveness.

## Evaluation Gate

Run deterministic eval manifests before release:

```powershell
cd backend
python -m app.cli eval run --manifest "C:\path\to\eval.json" --output-dir "C:\path\to\reports" --json
```

The eval harness writes JSON and Markdown reports that can be archived with
release artifacts.

## Upgrade Notes

When upgrading from pre-enterprise CLI/MCP builds:

- `docs/usage/cli-and-mcp.md` is now a quickstart; detailed docs moved to
  `docs/usage/cli.md` and `docs/usage/mcp.md`.
- JSON errors use `marker.error.v1`.
- Successful conversions write `.marker.json` output manifests.
- URL conversion uses SSRF guards and optional host allowlists.
- Workspace/output root policy can block local paths that worked before.
- Non-loopback MCP Streamable HTTP requires a bearer token.
- Static token auth is implemented; configured OIDC is rejected until full JWT
  verification lands.
