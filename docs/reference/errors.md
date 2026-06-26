# Agent Error Codes

Marker maps CLI, MCP, and agent-facing REST failures to stable structured errors.
The source of truth is `backend/app/errors.py`.

## JSON Shape

CLI `--json` errors are written to stderr:

```json
{
  "ok": false,
  "schema_version": "marker.error.v1",
  "error": {
    "code": "INPUT_NOT_FOUND",
    "message": "Input not found",
    "hint": "Check the path or pass --source-url for remote files.",
    "details": {},
    "retryable": false
  }
}
```

## Exit Codes

| Exit code | Meaning |
|-----------|---------|
| `0` | Success. |
| `1` | Internal error. |
| `2` | Usage error. |
| `3` | Input not found. |
| `4` | Unsupported or disallowed input. |
| `5` | Configuration error. |
| `6` | Auth or cloud permission error. |
| `7` | Network or URL safety error. |
| `8` | Conversion failed. |
| `9` | Timeout or cancellation. |
| `10` | Partial failure. |
| `11` | Output exists or output write failed. |

## Codes

| Code | Exit | Retryable | Meaning |
|------|------|-----------|---------|
| `USAGE_ERROR` | `2` | No | Invalid command, argument, JSON, or option. |
| `INPUT_NOT_FOUND` | `3` | No | Local input path does not exist. |
| `INPUT_NOT_ALLOWED` | `4` | No | Input denied by policy or permissions. |
| `UNSUPPORTED_FORMAT` | `4` | No | File type is unsupported for requested path. |
| `OUTPUT_EXISTS` | `11` | No | Output path already exists and overwrite is false. |
| `OUTPUT_WRITE_FAILED` | `11` | No | Output or manifest could not be written. |
| `CONFIG_ERROR` | `5` | No | Runtime, provider, or server config is invalid. |
| `AUTH_REQUIRED` | `6` | No | Missing required auth. |
| `AUTH_FORBIDDEN` | `6` | No | Auth present but required scope is missing. |
| `NETWORK_BLOCKED` | `7` | No | URL blocked by allowlist or SSRF policy. |
| `NETWORK_FETCH_FAILED` | `7` | Yes | Remote fetch failed after policy checks. |
| `URL_UNSAFE` | `7` | No | URL scheme, host, or redirect target is unsafe. |
| `CONVERSION_FAILED` | `8` | No | Converter failed after accepting input. |
| `MODEL_NOT_READY` | `8` | Yes | Required model is not available yet. |
| `MODEL_DOWNLOAD_FAILED` | `8` | Yes | Required model download failed. |
| `CLOUD_NOT_ALLOWED` | `6` | No | Cloud VLM path requested without explicit permission. |
| `TIMEOUT` | `9` | Yes | Operation timed out. |
| `CANCELLED` | `9` | No | Operation was cancelled. |
| `PARTIAL_FAILURE` | `10` | No | Batch or multi-step operation had failures. |
| `INTERNAL_ERROR` | `1` | No | Unexpected failure. |

## Handling Guidance

- Retry only when `retryable=true`.
- Treat `OUTPUT_EXISTS` as a user choice: pass `--overwrite` or choose another
  path.
- Treat `NETWORK_BLOCKED` and `URL_UNSAFE` as security decisions, not transient
  download failures.
- Treat `CLOUD_NOT_ALLOWED` as requiring explicit user approval before retry.
- Do not parse human `message` strings for automation; use `code`.
