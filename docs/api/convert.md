# Document Conversion API

Endpoint route prefix: `/api/convert`

Provides methods to upload documents, run local path conversions, poll status, delete jobs, and download results.

---

## 1. Upload Document for Conversion

`POST /api/convert/upload`

Submits a file for conversion or points the engine to a local absolute path.

### Request Body (Multipart Form)
- **`file`**: (Optional) The raw binary file to convert. Required if `local_filepath` is not provided.
- **`output_format`**: String. Either `markdown`, `html`, `json`, or `chunks` (Default: `markdown`).
  `markdown` is universal. `chunks` is available for native Markdown-derived
  outputs and Marker-backed outputs. `html` and `json` require a Marker-backed
  PDF/image/EPUB route today; native deterministic routes reject them instead
  of returning mislabeled Markdown.
- **`use_llm`**: Boolean. Enable LLM refinement (Default: `false`).
- **`converter`**: String. Either `PdfConverter` or `TableConverter` (Default: `PdfConverter`).
- **`local_filepath`**: (Optional) String. Absolute path to a file on the server's local storage.

### Response (`200 OK`)
```json
{
  "job_id": "8f2b1d60-705a-4e2e-a342-e19ef09bf3cd",
  "status": "pending",
  "filename": "academic-paper.pdf",
  "output_format": "markdown",
  "created_at": "2026-06-12T23:41:55"
}
```

### curl Example (Multipart File Upload)
```bash
curl -X POST "http://localhost:8000/api/convert/upload" \
  -F "file=@/path/to/my-doc.pdf" \
  -F "output_format=markdown" \
  -F "use_llm=true"
```

### curl Example (Local Path Reference)
```bash
curl -X POST "http://localhost:8000/api/convert/upload" \
  -F "local_filepath=/data/docs/important-report.pdf" \
  -F "output_format=json"
```

Use `/api/capabilities` before presenting format choices. Each
`input_formats[]` entry includes the `output_formats` that extension group can
actually render.

---

## 2. Check Job Status

`GET /api/convert/status/{job_id}`

Retrieves the current state of a conversion job.

### Response (`200 OK`)
```json
{
  "job_id": "8f2b1d60-705a-4e2e-a342-e19ef09bf3cd",
  "status": "completed",
  "progress": 100,
  "error": null,
  "filename": "academic-paper.pdf",
  "output_length": 14032,
  "completed_at": "2026-06-12T23:43:02"
}
```

### curl Example
```bash
curl "http://localhost:8000/api/convert/status/8f2b1d60-705a-4e2e-a342-e19ef09bf3cd"
```

---

## 3. Download Job Output

`GET /api/convert/download/{job_id}`

Downloads the converted outputs. If the job extracted images, returns a `.zip` file. Otherwise, returns the raw text file of the chosen format.

### curl Example
```bash
curl -o paper.zip "http://localhost:8000/api/convert/download/8f2b1d60-705a-4e2e-a342-e19ef09bf3cd"
```

---

## 4. List Job History

`GET /api/convert/history`

Retrieves a paginated list of all past conversions.

### Query Parameters
- `page`: Integer (Default: `1`)
- `page_size`: Integer (Default: `10`)

### Response (`200 OK`)
```json
{
  "jobs": [
    {
      "job_id": "8f2b1d60-705a-4e2e-a342-e19ef09bf3cd",
      "filename": "academic-paper.pdf",
      "status": "completed",
      "created_at": "2026-06-12T23:41:55"
    }
  ],
  "total_count": 12,
  "page": 1,
  "pages": 2
}
```

---

## 5. Delete Job

`DELETE /api/convert/{job_id}`

Deletes the SQLite record and deletes the corresponding conversion folder from `data/output/{job_id}`.

### curl Example
```bash
curl -X DELETE "http://localhost:8000/api/convert/8f2b1d60-705a-4e2e-a342-e19ef09bf3cd"
```
