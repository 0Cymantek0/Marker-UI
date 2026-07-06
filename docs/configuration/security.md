# Security Architecture

Marker UI handles sensitive local information, including documents and API keys for cloud LLM providers. This document specifies the security boundaries and mechanisms used to protect this data.

---

## 1. Credentials Encryption (Fernet)

API keys for LLM services are encrypted before they are stored in the database.
- **Algorithm**: AES-128 in CBC mode using a SHA256 HMAC for authentication (standard Fernet cryptography via python's `cryptography` library).
- **Key Store**: The 32-byte key is stored in `data/.secret_key` with strict host system permissions (`0600` in Linux/macOS environments).
- **Graceful Failbacks**: If unencrypted keys are found (e.g. from legacy installations), they are returned as-is rather than crashing, but will be encrypted upon the next settings save.

---

## 2. API Response Masking

To prevent secret keys from appearing in browser logs, diagnostic outputs, or frontend state:
- Every settings request that returns credentials runs through the masking filter in `app.utils.secrets`.
- Any setting key containing substrings like `api_key`, `token`, `password`, or `secret` is masked.
- If the value is 8 characters or less, it is replaced entirely by `****`.
- If the value is longer than 8 characters, the first 4 characters and the last 4 characters are preserved, with the middle characters replaced by asterisks (e.g. `sk-p********abcd`).

---

## 3. Local Path Validation

The **Local Absolute Paths** feature allows users to convert documents by specifying their location on the server's filesystem.
- To prevent arbitrary directory traversal attacks or loading system-protected files, paths must be explicitly validated.
- **SSRF Restrictions**: The application restricts outbound LLM requests to verified provider endpoints, blocking requests to internal metadata endpoints.

---

## 4. MCP Transport Threat Model

The Marker CLI/MCP server (`backend/app/mcp_server.py`) exposes tools that can
convert local files, submit jobs, and read, set, or delete persisted settings.
Because settings can include provider credentials and jobs include converted
documents, the server must not be reachable by untrusted clients.

### 4.1 Transports

| Transport | Default bind | Intended exposure | Risk if exposed |
|-----------|--------------|-------------------|-----------------|
| `stdio` | n/a (parent process pipes) | Local coding agent only | Low; inherits agent process trust. |
| `streamable-http` loopback (`127.0.0.1`, `::1`, `localhost`) | `127.0.0.1` | Single host, same machine | Low; only local processes can call. |
| `streamable-http` non-loopback (`0.0.0.0`, LAN, WAN) | refused unless token set | Remote/multi-client | High; anyone reachable can invoke settings and delete tools. |

### 4.2 Mitigations enforced in `run()`

1. **Loopback-only default.** `streamable-http` defaults to `127.0.0.1`.
2. **Non-loopback requires a bearer token.** Binding to `0.0.0.0`, a LAN
   address, or any other non-loopback host raises `ValueError` unless
   `MARKER_MCP_AUTH_TOKEN` (or `--auth-token`) is configured.
3. **Constant-time token comparison.** `StaticTokenVerifier.verify_token` uses
   `secrets.compare_digest` so token checks resist timing attacks.
4. **Scope gating.** When a token is set, `AuthSettings` requires the
   `marker:mcp` scope so clients must present a valid bearer token for every
   request, including `marker_list_settings`, `marker_get_setting`,
   `marker_set_setting`, `marker_delete_setting`, `marker_delete_job`, and
   `marker_purge_job_files`.

### 4.3 Residual risks and operator responsibilities

- **Transport encryption.** MCP HTTP is plaintext. Terminate TLS at a reverse
  proxy (e.g. Caddy, nginx) for any remote deployment so bearer tokens and
  converted content are not exposed on the wire.
- **Token strength.** Generate a high-entropy token; a weak or shared token
  still lets any holder call destructive tools.
- **Network exposure.** Even with a token, prefer binding behind a firewall or
  VPN. The token is a required gate, not a substitute for network controls.
- **Process trust.** `stdio` and loopback HTTP assume the calling agent or
  local process is trusted. Do not bridge either to a public endpoint without
  the token plus TLS path above.

### 4.4 Verification

`backend/tests/test_cli_mcp.py::test_mcp_streamable_http_refuses_non_loopback_without_auth_token`
asserts that `run(transport="streamable-http", host="0.0.0.0")` raises without a
token, and
`test_mcp_streamable_http_configures_bearer_auth_for_non_loopback` asserts that a
token installs `AuthSettings` plus a token verifier before the server starts.
