# Testing Strategy

Marker UI uses **pytest** for testing the FastAPI backend components. Quality is verified across database schemas, encryption algorithms, API routers, and concurrency limits.

---

## Running Tests

Ensure you have activated your virtual environment inside the `backend/` directory, then execute:
```bash
python -m pytest tests/ -v
```

---

## Test Suites Overview

The backend contains a highly comprehensive suite of over 540 automated tests:
- **`test_crypto.py` / `test_secrets.py`**: Validates credential encryption at rest and key masking in JSON responses.
- **`test_upload.py` / `test_convert.py`**: Verifies allowed file types, upload size limits, and job execution SSE streams.
- **`test_settings.py` / `test_providers.py`**: Asserts configuration CRUD and LLM provider registration.
- **`test_task_manager.py` / `test_gpu_workers.py` / `test_job_transport.py`**: Validates thread pools, GPU routing, process workers, and IPC events.
- **`test_vlm_service.py` / `test_image_cost.py` / `test_image_router.py`**: Tests image routing, cost estimation, and VLM JSON extraction.

---

## Known Testing Gaps

1. **End-to-End Frontend Tests**: While React components are covered using Vitest, no automated Cypress or Playwright E2E visual flows are run.
2. **Real LLM / VLM API Integration**: Production VLM services are verified using mock APIs (`FakeVLM`) rather than calling live cloud endpoints.
3. **Local OCR Model Loading**: Standard testing avoids loading heavy Surya neural weights on CPU hosts, mocking OCR result structures instead.
