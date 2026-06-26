# Agent JSON Schemas

Marker exposes stable agent-facing JSON contracts for CLI, MCP, and docs tooling.
The source of truth is `backend/app/agent_contract.py`.

## Export

From `backend`:

```powershell
python -m app.cli schema export --json
```

Write to a file:

```powershell
python -m app.cli schema export --output "C:\path\to\marker-schemas.json" --json
```

The top-level object has:

```json
{
  "schema_version": "marker.agent_contract.v1",
  "models": {},
  "option_metadata": []
}
```

## Model Set

| Model | Purpose |
|-------|---------|
| `ConversionOptionsModel` | Shared conversion options. |
| `PlanRequestModel` | Agent planning request. |
| `PlanResultModel` | Planning response. |
| `ConvertRequestModel` | One-shot conversion request. |
| `ConvertResultModel` | One-shot conversion response. |
| `SubmitJobRequestModel` | Async job submission request. |
| `SubmitJobResultModel` | Async job submission response. |
| `JobStatusModel` | Job status and metadata response. |
| `OutputManifestModel` | `.marker.json` output manifest. |
| `MarkerErrorModel` | Structured error object. |
| `BatchRequestModel` | Batch conversion manifest. |
| `BatchResultModel` | Batch conversion result. |
| `OptionMetadataModel` | CLI/MCP option metadata. |

## Schema Versions

| Schema | Version |
|--------|---------|
| Agent contract bundle | `marker.agent_contract.v1` |
| Structured error | `marker.error.v1` |
| Output manifest | `marker.output_manifest.v1` |
| Batch result | `marker.batch_result.v1` |
| Doctor result | `marker.doctor.v1` |
| Eval manifest | `marker.eval_manifest.v1` |
| Eval report | `marker.eval_report.v1` |

## Option Metadata

`option_metadata` maps shared option names to CLI flags and categories. Important
fields:

- `name`: shared backend option name.
- `cli_flag`: public CLI flag where available.
- `mcp_name`: MCP name when it differs from `name`.
- `type`: boolean, string, number, enum, or object.
- `default`: default value.
- `category`: output, routing, images, pdf, audio, runtime, or advanced.
- `description`: short user-facing description.

Advanced options can still be passed through `--option`, `--options-json`, or
MCP `extra_options_json` when a first-class flag is not present.

## Compatibility Rules

- Additive fields are allowed for flexible response models.
- Request models reject unknown top-level fields unless routed through
  `extra_options`.
- `schema_version` strings should change only for breaking contract changes.
- CLI `--json` failures must serialize as `MarkerErrorModel`.
- Output manifests must validate against `OutputManifestModel`.

## MCP Resources

Agents can read option metadata through:

```text
marker://docs/options
```

Version information is available at:

```text
marker://version
```
