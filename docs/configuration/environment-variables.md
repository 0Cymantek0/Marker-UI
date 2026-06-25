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
| `MARKER_DATABASE_URL` | SQLAlchemy connection URL for database persistence. | `sqlite+aiosqlite:///data/marker_ui.db` |
| `MARKER_PRELOAD_MODELS` | Preload Marker models at worker startup when `true`; disable for lightweight CLI/MCP startup with `false`. | `true` |
| `MARKER_MCP_AUTH_TOKEN` | Bearer token required when MCP Streamable HTTP binds to any non-loopback host. Localhost stdio and loopback HTTP do not require it. | unset |

---

## Removed/Legacy Variables

- **`MARKER_ACCESS_TOKEN`**: Some early draft documentation mentioned this variable for API authentication. It is **not** implemented in the core codebase and has been removed to avoid confusion. If API-level authentication is required, it should be set up at the Nginx reverse proxy layer.

---

## Setting Variables

### Docker Compose
Modify the `environment` section of `docker-compose.yml`:
```yaml
environment:
  - MARKER_HOST=0.0.0.0
  - MARKER_PORT=8000
  - MARKER_MAX_UPLOAD_SIZE_MB=200
```

### Source (Local shell)
Create a `.env` file in the root of the project:
```env
MARKER_HOST=127.0.0.1
MARKER_PORT=8000
MARKER_MAX_UPLOAD_SIZE_MB=50
```
FastAPI reads these variables on startup via Python's `os.getenv` system.
