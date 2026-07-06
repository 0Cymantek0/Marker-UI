# MCP Guide

Marker exposes a Model Context Protocol server for agents that need document
conversion, job history, output paging, and masked settings without using the
browser. MCP tools are thin wrappers over the same `app.agent_api` surface used
by the CLI.

## Transports

Local stdio:

```powershell
python -m app.cli mcp start --tool-profile minimal
```

Loopback Streamable HTTP:

```powershell
python -m app.cli mcp start --transport streamable-http --host 127.0.0.1 --port 8000 --tool-profile minimal
```

Non-loopback Streamable HTTP requires a bearer token:

```powershell
$env:MARKER_MCP_AUTH_TOKEN="change-this-token"
python -m app.cli mcp start --transport streamable-http --host 0.0.0.0 --port 8000 --tool-profile minimal
```

HTTP clients must send:

```text
Authorization: Bearer change-this-token
```

Use TLS at a reverse proxy for remote HTTP. Do not expose the backend directly
on an untrusted network.

## Client Configs

Generate snippets with:

```powershell
python -m app.cli mcp init-config --client codex --mode source --cwd "C:\path\to\marker\backend" --tool-profile minimal --output config.toml
python -m app.cli mcp init-config --client cursor --mode installed --tool-profile minimal --output mcp.json
python -m app.cli mcp init-config --client gemini --mode http --url "http://127.0.0.1:8000/mcp" --output settings.json
```

Supported clients: `codex`, `claude`, `gemini`, `opencode`, `cursor`, `zed`,
`cline`, `continue`, `goose`, `windsurf`, and `antigravity`. Source mode emits
`cwd`; installed mode uses the `marker` console command; HTTP mode emits a URL
and optional bearer header when `--auth-token` is provided.

Codex `.codex/config.toml` using source checkout:

```toml
[mcp_servers.marker]
command = "python"
args = ["-m", "app.cli", "mcp", "start", "--tool-profile", "minimal"]
cwd = "C:\\path\\to\\marker\\backend"
startup_timeout_sec = 20
tool_timeout_sec = 600
enabled = true
```

Codex with installed package:

```toml
[mcp_servers.marker]
command = "marker"
args = ["mcp", "start", "--tool-profile", "minimal"]
startup_timeout_sec = 20
tool_timeout_sec = 600
enabled = true
```

Claude Code:

```powershell
claude mcp add --transport stdio marker -- python -m app.cli mcp start --tool-profile minimal
```

Run that command from `C:\path\to\marker\backend`, or put equivalent command and
working directory in `.mcp.json`.

Gemini CLI `settings.json`:

```json
{
  "mcpServers": {
    "marker": {
      "command": "python",
      "args": ["-m", "app.cli", "mcp", "start", "--tool-profile", "minimal"],
      "cwd": "C:\\path\\to\\marker\\backend",
      "timeout": 600000,
      "trust": false
    }
  }
}
```

OpenCode `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "marker": {
      "type": "local",
      "command": ["python", "-m", "app.cli", "mcp", "start", "--tool-profile", "minimal"],
      "cwd": "C:\\path\\to\\marker\\backend",
      "enabled": true
    }
  }
}
```

Antigravity MCP config:

```json
{
  "mcpServers": {
    "marker": {
      "command": "python",
      "args": ["-m", "app.cli", "mcp", "start", "--tool-profile", "minimal"],
      "cwd": "C:\\path\\to\\marker\\backend"
    }
  }
}
```

## Tool Profiles

Default profile is `minimal`, also available through
`MARKER_MCP_TOOL_PROFILE=minimal`. It exposes the small safe surface needed by
most coding agents:

- `marker_capabilities`
- `marker_plan`
- `marker_convert`
- `marker_submit`
- `marker_job_status`
- `marker_cancel_job`
- `marker_read_output`
- `marker_output_manifest`

Use `--tool-profile full` for legacy/source-specific convenience tools such as
`marker_convert_file`, `marker_convert_url`, and `marker_submit_local_job`.
Use `--tool-profile admin` only when the agent needs destructive/admin tools
such as `marker_delete_job`.

Settings write/delete tools are disabled even in `admin` unless
`MARKER_MCP_ENABLE_SETTINGS_WRITE=true` is set. Enable this only for trusted
agents because model-controlled settings writes can change provider keys,
base URLs, and other sensitive runtime behavior.

Canonical v2 tools use one source object:

```json
{ "kind": "local_path", "path": "C:\\path\\to\\document.pdf" }
```

```json
{ "kind": "url", "url": "https://docs.example.com/report.pdf" }
```

## Tools

| Tool | Purpose |
|------|---------|
| `marker_capabilities` | Canonical v2 capability tool. |
| `marker_plan` | Canonical v2 planning with a `{kind, path|url}` source object. |
| `marker_convert` | Canonical v2 conversion with a `{kind, path|url}` source object. |
| `marker_submit` | Canonical v2 async submission with a `{kind, path|url}` source object. |
| `marker_job_status` | Canonical v2 job status. |
| `marker_output_manifest` | Canonical v2 output manifest reader. |
| `marker_list_capabilities` / `marker_get_capabilities` | Supported formats, engines, tools, resources, prompts, and options. |
| `marker_get_health` | Lightweight MCP health check. |
| `marker_get_version` | Version and contract schema version. |
| `marker_plan_conversion` | Backward-compatible generic planning. |
| `marker_plan_local_file` | Plan a local file after workspace policy checks. |
| `marker_plan_url` | Plan a safe public URL after SSRF checks. |
| `marker_convert_file` | Backward-compatible generic conversion. |
| `marker_convert_local_file` | Convert a local file and write manifest-backed output. |
| `marker_convert_url` | Fetch and convert a safe public URL. |
| `marker_submit_job` | Backward-compatible generic async submission. |
| `marker_submit_local_job` | Submit a local-file job. |
| `marker_submit_url_job` | Submit a URL job. |
| `marker_read_output` / `marker_read_output_chunk` | Page through long generated text. |
| `marker_get_output_manifest` | Read a `.marker.json` output manifest. |
| `marker_list_output_assets` | List manifest asset entries. |
| `marker_list_jobs` | Page through job history. |
| `marker_get_job_status` | Inspect one job. |
| `marker_cancel_job` | Request job cancellation. |
| `marker_delete_job` | Delete job metadata and optionally files. |
| `marker_list_settings` | Read masked settings by category. |
| `marker_get_setting` | Read one masked setting. |
| `marker_set_setting` | Write one encrypted setting. Requires `admin` and `MARKER_MCP_ENABLE_SETTINGS_WRITE=true`. |
| `marker_delete_setting` | Delete one setting. Requires `admin` and `MARKER_MCP_ENABLE_SETTINGS_WRITE=true`. |
| `marker_self_test` | Validate tools, resources, prompts, schemas, and a TSV conversion smoke path. |

Tool annotations mark read-only, destructive, idempotent, and closed-world
behavior for clients that use MCP planning metadata. Destructive tools are still
policy-gated server side.

## Resources

| Resource | Purpose |
|----------|---------|
| `marker://capabilities` | Capabilities plus tool/resource/prompt names. |
| `marker://health` | Lightweight health. |
| `marker://version` | Version and contract schema. |
| `marker://jobs` | First page of job history. |
| `marker://jobs/{job_id}` | One job status. |
| `marker://jobs/{job_id}/manifest` | Completed job manifest. |
| `marker://jobs/{job_id}/output` | First output text chunk. |
| `marker://jobs/{job_id}/assets` | Manifest asset entries. |
| `marker://outputs/{output_id}/manifest` | Manifest for a URL-encoded output path. |
| `marker://docs/agent-guide` | Safe agent workflow guide. |
| `marker://docs/options` | Agent option metadata. |
| `marker://settings` | Masked settings. |

## Agent Workflow

1. Read `marker://capabilities` or call `marker_capabilities`.
2. Plan with `marker_plan` for PDFs and unknown inputs.
3. Convert small work with `marker_convert`.
4. Submit long work with `marker_submit`, then poll `marker_job_status`.
5. Read long output with `marker_read_output_chunk`.
6. Inspect `.marker.json` manifests before summarizing asset-heavy output.
7. Keep `allow_cloud_vlm=false` unless the user explicitly approves cloud image understanding.

## Security

MCP uses static bearer tokens for HTTP auth. The implemented scope names are:

- `marker:mcp`
- `capabilities:read`
- `jobs:read`
- `jobs:write`
- `outputs:read`
- `settings:read`
- `settings:write`

Configure token scopes:

```powershell
$env:MARKER_MCP_AUTH_TOKEN="change-this-token"
$env:MARKER_MCP_AUTH_SCOPES="marker:mcp capabilities:read jobs:read jobs:write outputs:read"
```

Settings write/delete tools require `settings:write`. Output tools require
`outputs:read`. Job mutation requires `jobs:write`.

## Verification

Run local CLI self-test:

```powershell
python -m app.cli self-test --json
```

From an MCP client, call `marker_self_test`. It verifies the expected tools,
resources, prompts, schemas, and a real TSV conversion smoke path.

The read-only MCP usability evaluation lives at
[`marker-mcp-evaluation.xml`](marker-mcp-evaluation.xml).
