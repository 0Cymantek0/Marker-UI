# Enterprise Security

Marker is local-first by default, but agent and HTTP surfaces need explicit
controls when deployed beyond a single trusted desktop. This page documents the
implemented controls and what they do not do.

## Defaults

- REST API auth is disabled until a REST token is configured.
- MCP stdio and loopback HTTP are allowed without a token.
- MCP Streamable HTTP on non-loopback hosts requires a bearer token.
- Cloud VLM calls are denied unless the request explicitly sets
  `allow_cloud_vlm=true` and uses an image-understanding mode.
- Source URL fetches allow public HTTP(S) by default, but block private,
  loopback, link-local, multicast, and unsafe redirect targets.
- Local path restrictions are disabled until workspace roots are configured.

## Static Token Auth

REST:

```powershell
$env:MARKER_REST_AUTH_TOKEN="change-this-rest-token"
$env:MARKER_REST_AUTH_SCOPES="capabilities:read jobs:read jobs:write outputs:read settings:read"
```

MCP:

```powershell
$env:MARKER_MCP_AUTH_TOKEN="change-this-mcp-token"
$env:MARKER_MCP_AUTH_SCOPES="marker:mcp capabilities:read jobs:read jobs:write outputs:read settings:read"
```

Shared map for multiple tokens:

```powershell
$env:MARKER_AUTH_TOKENS="token-a=capabilities:read jobs:read;token-b=*"
```

Scopes:

| Scope | Grants |
|-------|--------|
| `marker:mcp` | MCP bearer accepted for MCP surface. |
| `capabilities:read` | Capability and version reads. |
| `jobs:read` | Job history/status reads. |
| `jobs:write` | Job submit, cancel, and delete. |
| `outputs:read` | Output chunk, manifest, and asset reads. |
| `settings:read` | Masked settings reads. |
| `settings:write` | Settings writes and deletes. |
| `*` | All scopes. |

When REST bearer auth is enabled, route groups enforce matching scopes:
capabilities/version/model reads use `capabilities:read`; job planning/status
uses `jobs:read` or `capabilities:read`; upload, cancel, retry, regenerate, and
delete use `jobs:write`; downloads and job-scoped asset preview URLs use `outputs:read`; settings reads use
`settings:read`; settings writes and model-management mutations use
`settings:write`.

`settings:write` scope is not enough to expose MCP settings write/delete tools.
Set `MARKER_MCP_ENABLE_SETTINGS_WRITE=true` and use `--tool-profile admin` only
for trusted agents that should be able to modify stored configuration.

Health endpoints `/api/health`, `/api/healthz`, `/api/readyz`, and
`/api/version` remain unauthenticated so orchestration can probe the service.

OIDC environment variables (`MARKER_OIDC_ISSUER`, `MARKER_OIDC_AUDIENCE`,
`MARKER_OIDC_JWKS_URL`) are reserved. This build refuses configured OIDC tokens
rather than accepting unverified JWTs.

## Path Policy

Restrict local inputs:

```powershell
$env:MARKER_WORKSPACE_ROOTS="C:\path\to\documents;D:\team-share\docs"
```

Restrict outputs:

```powershell
$env:MARKER_OUTPUT_ROOT="C:\path\to\marker-output"
```

On Linux/macOS, separate roots with `:`. On Windows, separate roots with `;`.
When these variables are unset, Marker preserves legacy local behavior. When
set, CLI, MCP, and REST agent paths must stay inside allowed roots.

`marker_read_output` can read paths with a valid `.marker.json` manifest. When
`MARKER_OUTPUT_ROOT` is set, reads must also stay inside that root.

## URL Fetching

URL conversion accepts only HTTP(S). The safe fetcher blocks:

- private, loopback, link-local, multicast, unspecified, and reserved IPs;
- DNS results resolving to blocked IP ranges;
- redirects to blocked or disallowed hosts;
- redirects to a different host by default;
- hosts outside `MARKER_SOURCE_URL_ALLOWLIST` when an allowlist is configured.

For shared or production deployments, set `MARKER_SOURCE_URL_REQUIRE_ALLOWLIST=true`
and configure `MARKER_SOURCE_URL_ALLOWLIST` so arbitrary public hosts cannot be
used as a fetch target.

Allowlist example:

```powershell
$env:MARKER_SOURCE_URL_ALLOWLIST="docs.example.com,*.trusted.example"
$env:MARKER_SOURCE_URL_REQUIRE_ALLOWLIST="true"
```

Entries match exact hosts and subdomains. Wildcard entries of the form
`*.example.com` match subdomains.

## Secret Handling

Provider keys written through settings use the same encrypted settings path as
the GUI. Reads are masked. Audit payloads recursively redact sensitive key names
and URL credentials/query strings.

Do not pass real secrets in command examples, committed docs, shell history, or
batch manifests. Use environment injection or a secret manager in shared CI.

## Audit Events

Audit events are persisted for security-relevant actions, including:

- denied REST auth;
- settings writes/deletes;
- URL fetch started, succeeded, blocked, or failed;
- job submission;
- policy-denied local/output paths;
- cloud VLM requested.

Payloads are redacted before persistence. Audit logs support incident review but
are not a full SIEM replacement.

## Cloud VLM Control

Cloud image understanding requires explicit request-level opt-in:

- CLI: `--image-handling-mode understanding --allow-cloud-vlm`
- MCP/REST: `image_handling_mode` set to `understanding` or `both` and
  `allow_cloud_vlm=true`

Without this opt-in, cloud VLM paths are denied even when provider keys exist.

## Reverse Proxy Guidance

For remote deployments:

- terminate TLS at the proxy;
- require bearer auth at Marker or stronger auth at the proxy;
- forward `Authorization` to `/api/*` and `/mcp` when using Marker auth;
- keep backend bind addresses private when possible;
- set `MARKER_WORKSPACE_ROOTS` and `MARKER_OUTPUT_ROOT`;
- set `MARKER_SOURCE_URL_ALLOWLIST` for controlled environments.

See [Enterprise Deployment](deployment.md).
