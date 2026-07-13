# Environment Variables

Marker UI backend is configured using standard environment variables. You can set these in your local terminal session, inside a `.env` file in the project root, or under the `environment` key in `docker-compose.yml`.

---

## Configuration Reference

| Environment Variable | Description | Default Value |
|----------------------|-------------|---------------|
| `MARKER_HOST` | The host address the FastAPI server binds to. | `127.0.0.1` |
| `MARKER_PORT` | The port the FastAPI server listens on. | `8000` |
| `MARKER_DEBUG` | Enables verbose FastAPI debugging and stack traces. | `false` |
| `MARKER_MAX_UPLOAD_SIZE_MB` | Maximum file size allowed for uploads (in Megabytes). | `100` |
| `MARKER_SOURCE_URL_ALLOWLIST` | Optional comma-separated host allowlist for `source_url` downloads. Empty allows any public, non-local HTTP(S) host that passes SSRF checks. Entries match exact hosts and subdomains; `*.example.com` is also supported. | unset |
| `MARKER_SOURCE_URL_REQUIRE_ALLOWLIST` | Require `MARKER_SOURCE_URL_ALLOWLIST` before any `source_url` download is accepted. Recommended for shared or production deployments. | `false` |
| `MARKER_WORKSPACE_ROOTS` | Optional `os.pathsep`-separated list of local filesystem roots allowed for `local_filepath`/agent local file conversion. On Windows use `;`; on Linux/macOS use `:`. REST `local_filepath` requires this unless unrestricted local paths are explicitly enabled. | unset |
| `MARKER_OUTPUT_ROOT` | Optional root that output reads and explicit output directories/paths must stay inside. REST `output_dir` requires this unless unrestricted local paths are explicitly enabled. When unset, `marker_read_output` only reads Marker outputs that carry a valid `.marker.json` manifest. | unset |
| `MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS` | Development-only escape hatch that allows REST `local_filepath` and `output_dir` without `MARKER_WORKSPACE_ROOTS`/`MARKER_OUTPUT_ROOT`. Do not enable on shared or exposed deployments. | `false` |
| `MARKER_DATABASE_URL` | SQLAlchemy connection URL for database persistence. | `sqlite+aiosqlite:///data/marker_ui.db` |
| `MARKER_PRELOAD_MODELS` | Preload Marker models at worker startup when `true`; keep lazy for lightweight CLI/MCP startup with `false`. | `false` |
| `MARKER_MCP_AUTH_TOKEN` | Bearer token required when MCP Streamable HTTP binds to any non-loopback host. Localhost stdio and loopback HTTP do not require it. | unset |
| `MARKER_MCP_AUTH_SCOPES` | Space- or comma-separated scopes granted to `MARKER_MCP_AUTH_TOKEN`. | all MCP scopes |
| `MARKER_MCP_ENABLE_SETTINGS_WRITE` | Registers MCP settings write/delete tools in the `admin` tool profile. Keep unset unless a trusted agent should modify stored settings. | `false` |
| `MARKER_REST_AUTH_TOKEN` | Enables REST bearer auth for `/api/*` except health/version endpoints. | unset |
| `MARKER_AUTH_TOKEN` | Legacy alias used as a REST token when `MARKER_REST_AUTH_TOKEN` is unset. | unset |
| `MARKER_REST_AUTH_SCOPES` | Space- or comma-separated scopes granted to the REST token. | REST scopes |
| `MARKER_AUTH_TOKENS` | Semicolon-separated static token map such as `token-a=jobs:read outputs:read;token-b=*`. | unset |
| `MARKER_OIDC_ISSUER` | Reserved OIDC issuer setting. Configured OIDC is rejected until verified JWT support lands. | unset |
| `MARKER_OIDC_AUDIENCE` | Reserved OIDC audience setting. | unset |
| `MARKER_OIDC_JWKS_URL` | Reserved OIDC JWKS URL setting. | unset |
| `MARKER_QUEUE_BACKEND` | Set to `sqlite` to enable durable queue recovery primitives. | unset |
| `MARKER_ENABLE_METRICS` | Enables `/api/metrics`. | `false` |
| `MARKER_VERSION` | Version string reported by `/api/version` and MCP version resource. | `0.1.0` or `unknown` |
| `MARKER_COMMIT_SHA` | Optional commit SHA reported by version metadata. | unset |

---

## Removed/Legacy Variables

- **`MARKER_ACCESS_TOKEN`**: Some early draft documentation mentioned this variable for API authentication. It is **not** implemented. Use `MARKER_REST_AUTH_TOKEN`, `MARKER_MCP_AUTH_TOKEN`, `MARKER_AUTH_TOKENS`, or reverse-proxy auth.

---

## Setting Variables

### Docker Compose
Modify the `environment` section of `docker-compose.yml`:
```yaml
environment:
  - MARKER_HOST=0.0.0.0
  - MARKER_PORT=8000
  - MARKER_MAX_UPLOAD_SIZE_MB=200
  - MARKER_SOURCE_URL_ALLOWLIST=docs.example.com,*.trusted.example
  - MARKER_WORKSPACE_ROOTS=/workspace/documents
  - MARKER_OUTPUT_ROOT=/workspace/output
```

### Source (Local shell)
Create a `.env` file in the root of the project:
```env
MARKER_HOST=127.0.0.1
MARKER_PORT=8000
MARKER_MAX_UPLOAD_SIZE_MB=50
MARKER_SOURCE_URL_ALLOWLIST=docs.example.com
MARKER_WORKSPACE_ROOTS=C:\path\to\documents
MARKER_OUTPUT_ROOT=C:\path\to\output
```
FastAPI reads these variables on startup via Python's `os.getenv` system.
