# Output Manifest Reference

Every successful conversion writes a `.marker.json` manifest beside the text
output or inside the output bundle directory. The manifest lets CLI, MCP, REST,
and agents verify what was produced without guessing paths or extensions.

## Paths

File output layout:

```text
document.md
document.marker.json
document_assets/
```

Directory layout when GUI-style jobs produce sidecar assets:

```text
document/
  document.md
  document.marker.json
  page_1.png
```

The exact text extension comes from converter output and media type, not from an
assumption about the requested format.

## Schema

Top-level shape:

```json
{
  "schema_version": "marker.output_manifest.v1",
  "created_at": "2026-01-01T00:00:00Z",
  "job_id": "optional-job-id",
  "source": {},
  "output": {},
  "conversion": {}
}
```

`source` records safe source metadata such as source name and source URL when
present. URL credentials and query secrets are not stored.

`output` fields:

| Field | Meaning |
|-------|---------|
| `final_path` | Final file or bundle directory path. |
| `text_path` | Main generated text path. |
| `manifest_path` | Manifest path. |
| `media_type` | Guessed media type for the text output. |
| `text_chars` | Character count of generated text. |
| `text_sha256` | SHA-256 hash of generated text. |
| `asset_count` | Number of asset entries. |
| `assets` | Asset entry list. |

Asset entries include:

- `name`: manifest-relative asset name.
- `path`: resolved asset path.
- `media_type`: asset media type.
- `size`: byte size when available.
- `sha256`: content hash when available.

`conversion` stores conversion config and metadata needed by downstream tools.
It can include routing decisions, mixed-engine segments, output format, and
backend metadata.

## Reading Manifests

CLI:

```powershell
python -m app.cli read-output "C:\path\to\out\document.md" --json
```

MCP:

```text
marker_get_output_manifest
marker://jobs/{job_id}/manifest
marker://outputs/{output_id}/manifest
```

`output_id` is a URL-encoded output path.

## Policy

When `MARKER_OUTPUT_ROOT` is set, output reads and manifest reads must stay
inside that root. Without `MARKER_OUTPUT_ROOT`, `marker_read_output` accepts
paths that are associated with a valid Marker output manifest.

## Agent Rules

- Inspect the manifest before claiming assets exist.
- Use `text_sha256` for cache keys and reproducibility checks.
- Page long text through output chunk tools instead of reading whole files into
  context.
- Do not infer output extension from requested `output_format`; use
  `output.media_type` and `output.text_path`.
