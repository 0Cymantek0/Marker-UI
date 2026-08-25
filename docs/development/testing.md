# Testing Strategy

Marker UI uses **pytest** for testing the FastAPI backend components. Quality is verified across database schemas, encryption algorithms, API routers, and concurrency limits.

---

## Running Tests

Ensure you have activated your virtual environment from the project root, then execute:
```bash
python -m pytest backend/tests -v
cd frontend
npm test
```

When `pytest-xdist` is installed, a broad backend run scales workers to CPU
and memory capacity. Focused file/node, `-k`, `-m`, last-failed, stepwise, and
collection-only commands stay serial to avoid worker startup dominating the
test itself. Override deliberately with `MARKER_TEST_WORKERS=<count>`; use
`MARKER_TEST_WORKERS=serial` (or `0`/`1`) to force serial execution. Worker
requests remain capped by the suite's process and memory safety limits.

---

## Test Suites Overview

Current collection covers 2,215 backend pytest tests and 161 frontend Vitest tests:
- **`test_crypto.py` / `test_secrets.py`**: Validates credential encryption at rest and key masking in JSON responses.
- **`test_upload.py` / `test_convert.py`**: Verifies allowed file types, upload size limits, and job execution SSE streams.
- **`test_settings.py` / `test_providers.py`**: Asserts configuration CRUD and LLM provider registration.
- **`test_task_manager.py` / `test_gpu_workers.py` / `test_job_transport.py`**: Validates thread pools, GPU routing, process workers, and IPC events.
- **`test_vlm_service.py` / `test_image_cost.py` / `test_image_router.py`**: Tests image routing, cost estimation, and VLM JSON extraction.
- **`test_kernel_*.py`**: Covers the Truth Kernel — commit spine, payload durability, materialized generations, retention/GC, fenced publication, fair scheduling/liveness/events, runtime integration, source truth/anchors/reading order, patches, claims/proofs, verification risk, and publication sets with lexical (FTS5) generations.

---

## Known Testing Gaps

1. **End-to-End Frontend Tests**: React components are covered using Vitest, but no automated Cypress or Playwright visual flows are required in the default suite.
2. **Real LLM / VLM API Integration**: Production VLM services are verified using mock APIs (`FakeVLM`) rather than calling live cloud endpoints.
3. **Local OCR Model Loading**: Standard testing avoids loading heavy Surya neural weights on CPU hosts, mocking OCR result structures instead.
4. **Optional Unstructured Chunking**: `unstructured_by_title` tests run only when the optional `unstructured` package is installed.
