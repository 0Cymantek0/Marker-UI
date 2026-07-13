# Security Policy

Marker UI is built to be a privacy-first, self-hosted web application. Because it runs locally and handles sensitive credentials (like LLM API keys) and local document files, we maintain strict security boundaries.

---

## Local Threat Model

Marker UI runs locally. The primary security boundaries are:
1. **API Key Storage**: LLM API keys (Gemini, Claude, OpenAI, etc.) are encrypted using **Fernet symmetric encryption** before persistence.
2. **Key Generation**: A unique encryption key is auto-generated on the first startup and saved at `data/.secret_key`.
3. **API Responses**: Key fields matching sensitive patterns (e.g. `_api_key`, `token`, `password`) are masked in all JSON API responses (e.g. `sk-proj-****abcd`), ensuring plaintext keys never flow to the frontend logs or state.
4. **Local Paths Sandbox**: When importing documents via local filesystem paths, the API ensures paths are checked and validated to prevent arbitrary directory traversal outside the server configuration bounds.

---

## Reporting a Vulnerability

> [!CAUTION]
> If you discover a security vulnerability in Marker UI, please do **not** open a public issue.

Instead, report it privately:
1. Email the maintainers at [cymantekfnosz@gmail.com] (or use the designated security email for your fork).
2. Include a detailed description of the vulnerability, steps to reproduce, and a proof of concept (PoC) if available.
3. We will acknowledge receipt of your report within 48 hours and work with you to patch it quickly.

---

## Best Practices for Deployment

To keep your deployment secure:
- **Binding Ports**: Keep local development bound to loopback whenever possible. If you bind REST or MCP to `0.0.0.0`, a LAN, or any shared network, require `MARKER_REST_AUTH_TOKEN`/`MARKER_MCP_AUTH_TOKEN` or stronger reverse-proxy authentication.
- **Path Roots**: Configure `MARKER_WORKSPACE_ROOTS` before enabling REST `local_filepath`, and configure `MARKER_OUTPUT_ROOT` before enabling REST `output_dir`. Do not set `MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS=true` outside trusted local development.
- **Authentication Model**: Marker UI supports static bearer-token auth with scopes for REST and MCP. Full OIDC/JWT validation is not implemented yet; configured OIDC values fail closed rather than accepting unverified JWTs. See `docs/enterprise/security.md`.
- **Backing up Secrets**: Back up the `data/.secret_key` and database `data/marker_ui.db` regularly. If the `.secret_key` is lost, your database settings containing LLM API keys will be unrecoverable.
- **Docker Privileges**: The default Docker image is built using a non-root user when possible, and volume paths inside the container are mapped directly to `/app/backend/data`. Keep folder permissions on the host restricted to the docker execution group.
