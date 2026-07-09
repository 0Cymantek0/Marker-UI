# CLI Guide

Marker's CLI is the headless entry point for local shell workflows, CI checks,
and coding agents that do not use MCP. It runs through `app.agent_api`, so plan,
convert, output, settings, policy, and manifest behavior match the GUI and MCP
paths.

## Running From Source

From the repository `backend` directory:

```powershell
python -m app.cli --help
python -m app.cli self-test --json
```

From the repository root, set `PYTHONPATH`:

```powershell
$env:PYTHONPATH="C:\path\to\marker\backend"
python -m app.cli self-test --json
```

If installed as a package, use:

```powershell
marker self-test --json
```

## Global Flags

Global flags go before the command:

- `--debug`: print stack traces for unexpected errors.
- `--version`: print version and commit metadata, then exit.
- `--quiet`: suppress non-error diagnostics.
- `--verbose`: emit extra diagnostics.
- `--no-input`: refuse interactive prompts.
- `--yes`: assume yes for supported confirmations.
- `--dry-run`: validate supported actions without writing.

Use `--json` on agent-facing commands so scripts receive stable objects and
typed `marker.error.v1` failures on stderr.

## Command Map

| Command | Purpose |
|---------|---------|
| `capabilities` | List supported formats, engines, options, and MCP names. |
| `plan` | Probe and plan conversion without writing output. |
| `convert` | Convert one local file or safe public URL. |
| `submit-job` | Submit one async GUI-compatible job. |
| `read-output` | Read a bounded text slice from an output file. |
| `jobs` | List conversion history. |
| `job-status` | Show status and metadata for one job. |
| `delete-job` | Cancel/delete one job, optionally deleting files. |
| `output` | Read converted output helpers. |
| `batch` | Convert many inputs sequentially with resume support. |
| `doctor` | Check runtime readiness. |
| `schema export` | Export agent JSON schemas and option metadata. |
| `eval run` | Run deterministic evaluation manifests. |
| `settings` / `config` | List, get, set, or delete encrypted settings. |
| `self-test` | Run CLI/MCP readiness checks. |
| `server` | Local server helper actions. |
| `mcp` | Start the MCP server. |

Every command supports `--help`; verify exact flags in the running checkout:

```powershell
python -m app.cli convert --help
python -m app.cli batch --help
python -m app.cli eval run --help
```

## Plan and Convert

Local path:

```powershell
python -m app.cli plan "C:\path\to\document.pdf" --conversion-profile auto --json
python -m app.cli convert "C:\path\to\document.pdf" --output-dir "C:\path\to\out" --json
python -m app.cli convert "C:\path\to\document.pdf" --output-path "C:\path\to\out\document.md" --overwrite --json
```

Safe public URL:

```powershell
python -m app.cli convert --source-url "https://docs.example.com/report.pdf" --output-dir "C:\path\to\out" --json
```

Use `MARKER_SOURCE_URL_ALLOWLIST` when deployments should restrict URL hosts.
Set `MARKER_SOURCE_URL_REQUIRE_ALLOWLIST=true` for shared deployments where URL
conversion must never fetch arbitrary public hosts. SSRF guards still block
local, private, loopback, cross-host redirects, and unsafe redirect targets.

Single-file convert also accepts the shared request contract from a file or
stdin. This is the safest route for automation with many options:

```json
{
  "local_file_path": "C:\\path\\to\\document.pdf",
  "output_path": "C:\\path\\to\\out\\document.md",
  "overwrite": false,
  "max_chars": 20000,
  "options": {
    "output_format": "markdown"
  }
}
```

Run it:

```powershell
python -m app.cli convert --request-json "C:\path\to\convert-request.json" --json
Get-Content "C:\path\to\convert-request.json" | python -m app.cli convert --stdin-json --json
```

## Advanced Options

Common GUI-compatible options have named flags:

```powershell
python -m app.cli convert "C:\path\to\meeting.mp3" --audio-output-mode enhanced --audio-word-timestamps --json
python -m app.cli convert "C:\path\to\meeting.mp3" --audio-provider local_faster_whisper --audio-diarization --audio-speaker-alias speaker_0=Alice --json
python -m app.cli convert "C:\path\to\meeting.mp3" --audio-text-enhancement --audio-text-enhancement-strength 2 --audio-structural-enhancement --audio-structural-enhancement-mode meeting_notes --audio-contradiction-detection --json
python -m app.cli convert "C:\path\to\meeting.mp3" --no-audio-confidence-heatmap --audio-quality-diagnostics --audio-fusion-mode audio_first --json
python -m app.cli convert "C:\path\to\data.tsv" --text-data-max-rows 1000 --json
python -m app.cli convert "C:\path\to\manuals.zip" --archive-max-files 50 --archive-max-total-uncompressed-bytes 20971520 --archive-max-compression-ratio 100 --archive-max-depth 2 --json
python -m app.cli convert "C:\path\to\scan.pdf" --image-handling-mode both --smart-router-level smart --ocr-min-lines 3 --json
python -m app.cli convert "C:\path\to\notes.md" --output-format chunks --chunking-strategy unstructured_by_title --chunk-max-tokens 512 --json
```

Audio stays local-first. `--audio-provider local_faster_whisper` is the default.
Cloud STT provider ids require `--audio-allow-cloud-stt`, and providers whose
adapters are not shipped yet fail before the job is queued. Unknown provider ids
are rejected instead of silently falling back to local. For reusable
vocabulary, use repeated `--audio-vocabulary-pack-id` flags. Provider
comparison is reserved: `--audio-benchmark-compare` and
`--audio-compare-provider` are rejected until a benchmark runner and at least
two shipped STT adapters exist.
Use `--audio-confidence-heatmap` / `--no-audio-confidence-heatmap`,
`--audio-quality-diagnostics` / `--no-audio-quality-diagnostics`, and
`--audio-fusion-mode` for advanced audio audit and context-fusion controls.

Use repeated `--option key=value` or one `--options-json` object for lower-level
backend options:

```powershell
python -m app.cli convert "C:\path\to\data.tsv" --options-json "{\"text_data_max_rows\": 1000}" --json
```

Cloud VLM calls are closed by default. Both an image-understanding mode and
`--allow-cloud-vlm` are required before configured cloud providers receive image
crops:

```powershell
python -m app.cli convert "C:\path\to\scan.pdf" --image-handling-mode understanding --allow-cloud-vlm --json
```

## Batch Mode

Batch mode accepts a JSON manifest with local paths or source URLs:

```json
{
  "items": [
    {
      "local_file_path": "C:\\path\\to\\one.pdf",
      "output_dir": "C:\\path\\to\\out",
      "overwrite": false
    },
    {
      "source_url": "https://docs.example.com/two.pdf",
      "output_dir": "C:\\path\\to\\out"
    }
  ],
  "continue_on_error": true,
  "resume": true
}
```

Run it:

```powershell
python -m app.cli batch --request-json "C:\path\to\batch.json" --json
```

The result reports totals, skipped items, failures, and paths to machine-readable
result files. `resume=true` skips entries whose output is already complete.

## Outputs and Manifests

Each conversion writes a text output and a sibling `.marker.json` manifest. The
manifest records source metadata, output paths, media type, text hash, asset
entries, and conversion config. Read long outputs in chunks:

```powershell
python -m app.cli read-output "C:\path\to\out\document.md" --offset 0 --limit 20000 --json
```

See [Output Manifest Reference](../reference/output-manifest.md).

## Settings

Settings commands use GUI-compatible encryption and masking:

```powershell
python -m app.cli settings list --json
python -m app.cli settings get openai_api_key --category llm --json
python -m app.cli settings set openai_api_key "env-or-secret-value" --category llm --json
python -m app.cli settings delete openai_api_key --category llm --json
```

Secrets are masked on read. Do not put real tokens in command history on shared
machines; prefer environment injection or an interactive secret manager.

## Evaluation

Run deterministic evaluation manifests:

```powershell
python -m app.cli eval run --manifest "C:\path\to\eval.json" --output-dir "C:\path\to\reports" --json
```

See [Deterministic Evaluation Harness](../development/evaluation.md).

## Security Controls

Use environment policy for agent deployments:

```powershell
$env:MARKER_WORKSPACE_ROOTS="C:\path\to\documents"
$env:MARKER_OUTPUT_ROOT="C:\path\to\outputs"
$env:MARKER_SOURCE_URL_ALLOWLIST="docs.example.com,*.trusted.example"
```

When `MARKER_WORKSPACE_ROOTS` is set, local input paths must stay inside those
roots. When `MARKER_OUTPUT_ROOT` is set, output writes and reads must stay inside
that root. See [Enterprise Security](../enterprise/security.md).
