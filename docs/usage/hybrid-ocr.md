# Hybrid OCR

Hybrid OCR is local-only document refinement. Surya/Marker builds the baseline
document, then configured local GLM-OCR and PaddleOCR-VL specialists refine
selected tables, formulas, and degraded scan regions.

Setup downloads model snapshots only; it does not configure cloud services:

```powershell
marker hybrid-ocr setup --engine all
marker hybrid-ocr status
```

Runtime workers must be local. Configure either localhost endpoints:

```powershell
$env:MARKER_GLM_OCR_ENDPOINT = "http://127.0.0.1:8765/ocr"
$env:MARKER_PADDLE_OCR_VL_ENDPOINT = "http://127.0.0.1:8766/ocr"
```

or local commands that read `--request request.json` and write
`--response response.json`:

```powershell
$env:MARKER_GLM_OCR_COMMAND = "python C:\path\to\glm_worker.py"
$env:MARKER_PADDLE_OCR_VL_COMMAND = "python C:\path\to\paddle_worker.py"
```

There is also an experimental in-process Transformers path for the downloaded
snapshots:

```powershell
$env:MARKER_HYBRID_OCR_ENABLE_NATIVE_TRANSFORMERS = "true"
```

Use it only after `marker hybrid-ocr status` and a smoke conversion prove your
installed `torch`/`transformers` stack can load the models. Otherwise use
separate worker environments from `backend/requirements-hybrid-glm.txt` and
`backend/requirements-hybrid-paddle.txt`.

Conversion never downloads models implicitly. If models or workers are missing,
Hybrid OCR keeps the Surya baseline and records warnings in metadata. Use:

```powershell
marker convert C:\path\to\document.pdf --ocr-engine hybrid_ocr --json
```
