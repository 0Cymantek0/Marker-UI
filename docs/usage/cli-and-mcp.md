# CLI and MCP

Marker can run headless for coding agents and shell workflows. The CLI and MCP
server use the same backend conversion service as the GUI.

## Positioning

Marker gives visionless or audio-limited models document perception through
Markdown. Agents can convert PDFs, Office files, archives, images, audio, video,
text, and data files, then page through the result without loading a GUI.

## CLI

Run from the repository root after installing the package in editable mode, or run
module commands with `PYTHONPATH` pointing at `backend`:

```powershell
python -m pip install -e .
marker self-test --json
```

```powershell
$env:PYTHONPATH="C:\path\to\marker\backend"
python -m app.cli self-test --json
```

For uninstalled source checkouts, you can also run commands from the repository
`backend` directory:

```powershell
python -m app.cli capabilities --json
python -m app.cli plan "C:\path\to\document.pdf" --conversion-profile auto --json
python -m app.cli submit-job "C:\path\to\document.pdf" --output-dir "C:\path\to\out" --json
python -m app.cli convert "C:\path\to\document.pdf" --output-dir "C:\path\to\out" --max-chars 20000 --json
python -m app.cli read-output "C:\path\to\out\document.md" --offset 20000 --limit 20000 --json
python -m app.cli jobs --page 1 --page-size 20 --json
python -m app.cli job-status "job-id" --include-result-text --max-chars 20000 --json
python -m app.cli settings list --json
python -m app.cli settings set openai_api_key "dummy-api-key" --category llm --json
python -m app.cli self-test --json
```

Advanced GUI-compatible knobs use named flags for common agent workflows and
repeated `--option key=value` or one `--options-json` object for everything else:

```powershell
python -m app.cli convert "C:\path\to\meeting.mp3" --audio-output-mode enhanced --audio-word-timestamps --audio-low-confidence-threshold 0.35 --json
python -m app.cli convert "C:\path\to\document.tsv" --text-data-max-rows 1000 --json
python -m app.cli convert "C:\path\to\manuals.zip" --archive-max-files 50 --archive-max-depth 2 --no-archive-recursive --json
python -m app.cli convert "C:\path\to\scan.pdf" --image-handling-mode both --smart-router-level smart --ocr-min-lines 3 --dedup-max-distance 4 --json
python -m app.cli convert "C:\path\to\document.tsv" --options-json "{\"text_data_max_rows\": 1000}" --json
```

Equivalent MCP advanced options use `extra_options_json`:

```json
{
  "local_file_path": "C:\\path\\to\\document.tsv",
  "output_dir": "C:\\path\\to\\out",
  "extra_options_json": "{\"text_data_max_rows\": 1000}"
}
```

Cloud and VLM paths remain explicit. Use `--image-handling-mode understanding`
and `--allow-cloud-vlm` only when sending image crops to the configured cloud
provider is acceptable.

## MCP Server

Start a local stdio server:

```powershell
python -m app.cli mcp
```

Start streamable HTTP on localhost:

```powershell
python -m app.cli mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

Streamable HTTP is unauthenticated only on loopback hosts (`127.0.0.1`, `::1`,
or `localhost`). Binding to `0.0.0.0`, a LAN address, or any other non-loopback
host requires a bearer token:

```powershell
$env:MARKER_MCP_AUTH_TOKEN="change-this-token"
python -m app.cli mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

Clients must send `Authorization: Bearer change-this-token`. This protects
settings and delete tools when the MCP server is reachable beyond the local
machine. Prefer a reverse proxy with TLS for remote deployments.

Tools:

- `marker_list_capabilities`: supported engines, extensions, modes, and tool names.
- `marker_plan_conversion`: probes/plans conversion without writing output.
- `marker_submit_job`: starts a GUI-compatible async job and returns a job id.
- `marker_convert_file`: converts a local file or safe public URL and writes output.
- `marker_read_output`: pages through long generated text.
- `marker_list_jobs`: paginated conversion history without large result text.
- `marker_get_job_status`: one job's status, metadata, paths, and optional bounded result text.
- `marker_delete_job`: cancel/delete one job and optionally remove its files.
- `marker_list_settings`: grouped settings with sensitive values masked.
- `marker_get_setting`: one masked setting.
- `marker_set_setting`: set one setting using GUI-compatible encryption rules.
- `marker_delete_setting`: delete one setting key.
- `marker_self_test`: verifies registered tools and a real TSV conversion smoke path.

`marker_convert_file` exposes the main GUI knobs directly: output format,
converter class, engine override, profile, LLM provider/model, image handling,
cloud VLM permission, OCR, pagination, image extraction, page range, language,
audio mode/model/vocabulary/context/confidence/timestamps, multiprocessing,
OCR stripping, inline math redo, debug, text/data row limits, archive traversal
limits, and the image-understanding router, dedup, downscale, batch, OCR engine,
crop size, batch size, and retry controls. For future or low-level options, pass
`extra_options_json` as a JSON object.

The server uses MCP `stdio` for local agent clients and Streamable HTTP at `/mcp`
for HTTP clients. Tool annotations mark read-only, destructive, idempotent, and
closed-world behavior so clients can plan safely. Tool responses are JSON and
structured where supported, with bounded previews to avoid flooding model context.

## Agent Config Examples

Codex project config `.codex/config.toml` using editable install:

```toml
[mcp_servers.marker]
command = "marker"
args = ["mcp"]
startup_timeout_sec = 20
tool_timeout_sec = 600
enabled = true
```

Codex project config `.codex/config.toml` using source checkout:

```toml
[mcp_servers.marker]
command = "python"
args = ["-m", "app.cli", "mcp"]
cwd = "C:\\path\\to\\marker\\backend"
startup_timeout_sec = 20
tool_timeout_sec = 600
enabled = true
```

Claude Code:

```powershell
claude mcp add --transport stdio marker -- python -m app.cli mcp
```

Run that command from `C:\path\to\marker\backend`, or configure the same command
and working directory in `.mcp.json`.

Gemini CLI `settings.json`:

```json
{
  "mcpServers": {
    "marker": {
      "command": "python",
      "args": ["-m", "app.cli", "mcp"],
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
      "command": ["python", "-m", "app.cli", "mcp"],
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
      "args": ["-m", "app.cli", "mcp"],
      "cwd": "C:\\path\\to\\marker\\backend"
    }
  }
}
```

## Agent Use Pattern

1. Call `marker_list_capabilities`.
2. Call `marker_plan_conversion` for PDFs or unknown files.
3. For long work, call `marker_submit_job`, then poll `marker_get_job_status`.
4. For one-shot work, call `marker_convert_file` with `output_dir` and bounded `max_chars`.
5. If `truncated=true`, call `marker_read_output` with `next_offset`.
6. Keep cloud paths off unless the user explicitly allows them.
7. Use `marker_list_jobs` / `marker_get_job_status` for GUI job history and long-running job inspection.
8. Use settings tools for provider/GPU/runtime knobs; secret values are masked on read and encrypted on write.

## Verification

Use:

```powershell
python -m app.cli self-test --json
```

For MCP clients, call `marker_self_test`. It reports expected tools,
registered tools, and whether a real TSV conversion produced the expected
Markdown table. The test suite also starts the MCP server through stdio,
inspects the tool schema, calls `marker_self_test`, converts a real CSV, and
pages the saved output through `marker_read_output`.

The read-only MCP usability evaluation lives at
`docs/usage/marker-mcp-evaluation.xml`.
