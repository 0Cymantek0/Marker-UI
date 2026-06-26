# Evaluation Harness

Marker eval manifests score deterministic candidate outputs against golden text
and optional table structures. The harness does not load Marker models; it is
intended for smoke checks, regression gates, and router decision reports.

## Manifest

```json
{
  "schema_version": "marker.eval_manifest.v1",
  "name": "smoke",
  "samples": [
    {
      "sample_id": "invoice-001",
      "golden_path": "golden/invoice.md",
      "candidate_path": "outputs/invoice.md",
      "routing": {
        "expected_engine": "text_data",
        "actual_engine": "text_data"
      }
    }
  ]
}
```

Use `golden_text` and `candidate_text` for inline fixtures, or
`golden_path` and `candidate_path` for files relative to the manifest.

## Run

```powershell
python -m app.cli eval run --manifest C:\path\to\eval_manifest.json --output-dir C:\path\to\reports --json
```

or:

```powershell
python backend/scripts/run_eval.py --manifest C:\path\to\eval_manifest.json --output-dir C:\path\to\reports
```

The runner writes `eval_report.json` and `eval_report.md` by default. The JSON
report uses schema `marker.eval_report.v1` and includes sample scores, summary
metrics, and router checks.
