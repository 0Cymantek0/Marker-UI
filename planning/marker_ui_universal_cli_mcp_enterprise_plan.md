# Marker UI Universal CLI + MCP + Enterprise Final Pass Plan

Generated: 2026-06-25
Repository: `0Cymantek0/Marker-UI`
Target reader: autonomous coding agent plus product/architecture reviewer
Primary objective: turn Marker UI into a universal, scriptable, agent-operable, enterprise-ready document conversion system exposed consistently through GUI, REST, CLI, and MCP.

---

## 0. How to use this document

Use this markdown file as the execution spec for the next final-pass implementation cycle.

The agent implementing this plan should:

1. Create a feature branch, for example `feat/universal-cli-mcp-final-pass`.
2. Keep existing GUI, REST, CLI, and MCP behavior backward-compatible unless a task explicitly says to deprecate something.
3. Treat `backend/app/agent_api.py` as the current stable seam shared by CLI and MCP, but refactor contracts out of it so schemas, docs, CLI, REST, and MCP can share one source of truth.
4. Add tests before or alongside every behavior change.
5. Run the existing backend tests, then targeted new CLI/MCP/security/batch tests.
6. Update docs in the same pull request as implementation changes.
7. Avoid introducing cloud calls, destructive actions, external URL fetches, or settings writes without explicit user/operator opt-in.

This plan is written as an implementation-grade backlog. Each item includes intent, expected files, acceptance criteria, and notes for autonomous execution.

---

## 1. Repository inspection scope and method

I inspected the repository through the GitHub connector and targeted the files that define the current architecture, CLI/MCP implementation, conversion pipeline, routing, worker management, settings, security boundaries, docs, and test strategy. A direct shell clone was attempted but the runtime could not resolve `github.com`, so the GitHub connector was used as the repository source of truth.

Primary repository areas inspected:

- `README.md`
- `pyproject.toml`
- `backend/requirements.txt`
- `docs/usage/cli-and-mcp.md`
- `docs/development/architecture.md`
- `docs/development/backend.md`
- `docs/development/task-manager.md`
- `docs/development/database.md`
- `docs/development/testing.md`
- `docs/configuration/environment-variables.md`
- `backend/app/cli.py`
- `backend/app/mcp_server.py`
- `backend/app/agent_api.py`
- `backend/app/main.py`
- `backend/app/routes/convert.py`
- `backend/app/models/schemas.py`
- `backend/app/services/conversion_service.py`
- `backend/app/services/task_manager.py`
- `backend/app/services/marker_service.py`
- `backend/app/conversion/router.py`
- `backend/app/conversion/probe.py`
- `backend/app/conversion/registry.py`
- `backend/app/conversion/result.py`
- `backend/app/conversion/stream_info.py`
- `backend/app/processors/image_understanding.py`

External standards and current industry references checked:

- MCP specification 2025-06-18: architecture, tools, resources, prompts, transports, authorization, roots, elicitation.
- MCP security best practices and OWASP MCP Security Cheat Sheet.
- OWASP Server-Side Request Forgery Prevention Cheat Sheet.
- Command Line Interface Guidelines at clig.dev.
- OpenTelemetry Python instrumentation guidance.
- NIST Secure Software Development Framework.

---

## 2. Executive summary

Marker UI already has a strong core. The current product is not just a GUI wrapper around Marker. It is already moving toward a universal conversion orchestrator with:

- A shared conversion seam for GUI, CLI, and MCP.
- A format router and converter registry.
- Fast deterministic paths for many non-PDF formats.
- Deep Marker/Surya path for PDFs/scans/images.
- Image-understanding VLM pipeline with routing, deduplication, batching, and local/cloud gates.
- Thread/process execution backends with multi-GPU support.
- SQLite job/settings persistence.
- MCP and CLI headless entry points.
- Tests and documentation that are already more mature than most early products.

The next milestone should not be random feature expansion. The next milestone should be product hardening around the agent entry points:

1. Freeze a stable agent contract.
2. Make CLI behavior script-grade and enterprise-grade.
3. Make MCP behavior standards-aligned and policy-safe.
4. Make output artifacts durable, manifest-driven, and reproducible.
5. Add workspace sandboxing, audit, telemetry, auth, and durable job semantics.
6. Add evaluation harnesses so router and conversion quality improve safely.

The core strategic move is to treat Marker UI as a local-first, agent-native document perception engine. The GUI is one interface. The CLI is the automation interface. MCP is the agent interface. REST is the service interface. All four should share the same typed contract and output model.

---

## 3. Product north star

### 3.1 Product statement

Marker UI should become the universal document-to-agent-context engine:

> Given any supported file, URL, archive, media asset, or enterprise batch, Marker UI converts it into reliable Markdown, structured JSON, HTML, assets, metadata, provenance, and quality signals that humans, scripts, RAG pipelines, and coding agents can consume safely.

### 3.2 Non-negotiable product qualities

The final product should be:

- Local-first by default.
- Cloud-explicit, never cloud-implicit.
- Scriptable through a stable CLI.
- Agent-operable through a standards-aligned MCP server.
- Enterprise-controllable through policies, roots, auth, audit, and quotas.
- Observable through logs, metrics, traces, job events, and manifests.
- Reproducible through schema versioning, manifests, config snapshots, and output hashes.
- Extensible through converter plugins, routing policies, and provider backends.
- Durable enough for batch workloads, not only one-off GUI uploads.

### 3.3 Primary personas

1. Individual power user
   - Wants local conversion, drag/drop, and CLI convenience.
   - Wants no cloud data leakage.
   - Wants output that is clean enough for RAG and note-taking.

2. Coding agent user
   - Wants Codex, Claude Code, Gemini CLI, OpenCode, or another agent to read PDFs, spreadsheets, slides, archives, audio, and images.
   - Wants bounded outputs and page/chunk reading so context is not flooded.
   - Wants safe tool behavior with explicit cloud and destructive-operation consent.

3. Enterprise automation team
   - Wants batch conversion at scale using scripts.
   - Wants policy controls, audit logs, deterministic output manifests, and retry/resume.
   - Wants a headless service mode, not only GUI.

4. Platform/IT/security team
   - Wants auth, RBAC, TLS, logs, metrics, config precedence, secret handling, SSRF protections, and deployment artifacts.
   - Wants clear boundaries for local paths, workspaces, and external network access.

---

## 4. Current architecture map

### 4.1 Current high-level system

```text
Browser / React UI
    |
    | HTTP / SSE
    v
FastAPI REST routes
    |
    | create job / read settings / stream status
    v
TaskManager
    |                         SQLite
    |                         - conversion_jobs
    |                         - settings
    v
ConversionService
    |
    +--> ConversionRouter
    |       - extension routing
    |       - PDF probe routing
    |       - execution backend hints
    |
    +--> ConverterRegistry
            - marker_pdf
            - liteparse_pdf
            - office_docx
            - office_pptx
            - spreadsheet
            - text_data
            - archive
            - audio
            - video
            - html
            - xml_rss
            - notebook
            - outlook_msg
```

### 4.2 Current headless architecture

```text
CLI command                    MCP tool call
    |                              |
    |                              v
    |                         FastMCP server
    |                              |
    +------------+-----------------+
                 |
                 v
          backend/app/agent_api.py
                 |
                 v
          ConversionService + TaskManager + SQLite + output files
```

### 4.3 Core design that should be preserved

The most important current design choice is that CLI and MCP reuse the same `agent_api.py` seam, and that seam reuses the same `ConversionService` and Marker option builder as the GUI. This is the correct direction. Do not fork conversion behavior for CLI/MCP.

Instead, strengthen the shared seam by moving typed contracts, output schemas, error types, option metadata, and manifest writing into shared modules.

---

## 5. What is already strong

### 5.1 Shared GUI/CLI/MCP conversion core

`backend/app/agent_api.py` explicitly states that it reuses the same `ConversionService` and option builder as the GUI route. This is the key product advantage. The same conversion behavior can be exposed to humans, scripts, and agents.

Keep this pattern.

### 5.2 Universal conversion router

`ConversionRouter` is stateless and cheap. It routes by extension and PDF probe metadata without importing heavy models. This is a good architecture boundary.

Current coverage includes:

- PDF/image/EPUB through Marker path.
- Audio through local transcript path.
- Video through local timeline path.
- Office documents and slides.
- Outlook MSG.
- Spreadsheets.
- CSV/TSV/JSON/plain text.
- XML/RSS/Atom.
- HTML.
- Jupyter notebooks.
- ZIP archives.

### 5.3 Converter registry abstraction

The `BaseConverter` and `ConverterRegistry` abstraction is the right foundation for plugins. It already separates planning from execution and supports priority.

### 5.4 PDF fast/deep routing

The PDF probe uses `pypdf` signals to choose LiteParse or Marker. The thresholds are explicit. The router provides warnings and fallback chains. This gives agents something explainable.

### 5.5 Runtime fallback behavior

`ConversionService.convert_file()` already falls back to `marker_pdf` when non-marker converters fail, and it detects suspiciously short LiteParse output. This is important for enterprise trust because the system attempts useful recovery instead of hard failing.

### 5.6 Multi-GPU process backend design

The `TaskManager` process backend is well designed for local workstation scaling:

- One worker process per GPU.
- GPU-pinned workers.
- Parent owns database writes to avoid SQLite multi-process contention.
- Worker events stream progress/log/result/error to the parent.
- CPU-thread pool for deterministic jobs when primary backend is process-based.

This is the seam for future multi-node execution.

### 5.7 Image understanding pipeline

The image understanding processor has a strong architecture:

- Router before VLM.
- Decorative skip path.
- Local OCR path.
- Cloud VLM gate.
- Dedup by image hash.
- Crop downscaling.
- Batch VLM calls.
- Metadata sidecar.
- Markdown/HTML handling to preserve Mermaid/LaTeX.

### 5.8 Current MCP server is already functional

The MCP server already supports:

- stdio transport.
- streamable HTTP transport.
- loopback-only unauthenticated HTTP.
- bearer token requirement on non-loopback host.
- structured output models.
- tool annotations.
- bounded previews.
- self-test.

This is a strong MVP.

### 5.9 Current CLI is already useful

The CLI already supports:

- `capabilities`
- `plan`
- `convert`
- `submit-job`
- `read-output`
- `jobs`
- `job-status`
- `delete-job`
- `settings`
- `self-test`
- `mcp`
- JSON output.
- Advanced options through named flags, `--option`, and `--options-json`.

This is a solid base for scripting.

---

## 6. Critical gaps and defects found

These are the highest-priority items because they can cause incorrect schemas, incorrect output metadata, unsafe agent behavior, or enterprise friction.

### 6.1 MCP delete-job output schema mismatch

Current behavior:

- `agent_api.delete_job()` returns `files_removed` as a list of removed paths.
- `mcp_server.DeleteJobOutput` declares `files_removed: bool`.

Why it matters:

- MCP clients may validate structured outputs.
- Incorrect schema undermines agent reliability.
- This is a small, high-confidence fix.

Action:

- Change `files_removed` in `DeleteJobOutput` to `list[str]`.
- Add MCP schema test that calls delete-job on a fixture job and validates output.

Expected files:

- `backend/app/mcp_server.py`
- `backend/tests/test_cli_mcp.py`

Acceptance:

- `marker_delete_job` output validates against its schema.
- Existing delete behavior remains unchanged.

### 6.2 Mixed PDF segment metadata is not fully persisted

Current behavior:

- `ConversionService._convert_mixed_pdf_segments()` returns metadata with `mixed_engine_segments`.
- `TaskManager._finalize_job()` persists `image_understanding`, `engine`, and `probe_result`, but not `mixed_engine_segments`.

Why it matters:

- Job history/status loses a major piece of routing provenance.
- Agents cannot explain which pages used which engine after completion.

Action:

- Persist `mixed_engine_segments` in `result_metadata_json`.
- Ensure REST status/history and MCP job status return it.

Expected files:

- `backend/app/services/task_manager.py`
- `backend/app/routes/convert.py`
- `backend/app/agent_api.py`
- tests for mixed PDF metadata persistence.

Acceptance:

- Completed mixed PDF job status includes `conversion_metadata.mixed_engine_segments`.
- Agent API job status includes the same metadata.

### 6.3 Upload size environment variable is documented but not wired

Current behavior:

- Docs list `MARKER_MAX_UPLOAD_SIZE_MB`.
- `backend/app/routes/convert.py` hardcodes `MAX_UPLOAD_SIZE = 100 * 1024 * 1024`.

Why it matters:

- Enterprise admins expect documented settings to work.
- Large batch/server deployments need configurable limits.

Action:

- Move upload size into `app.core.config`.
- Use a single value in REST and `source_url` download code.
- Add tests for env override.

Expected files:

- `backend/app/core/config.py`
- `backend/app/routes/convert.py`
- `docs/configuration/environment-variables.md`
- tests.

Acceptance:

- Setting `MARKER_MAX_UPLOAD_SIZE_MB=200` changes enforced limit.
- Docs and code match.

### 6.4 Output path collisions in async job finalizer

Current behavior:

- `agent_api._save_result()` uses `_next_available_output_path()` and refuses explicit overwrite.
- `TaskManager._finalize_job()` writes to predictable paths like `<stem>.md` or `<stem>/` and can collide.

Why it matters:

- Batch workflows can overwrite prior outputs.
- Concurrent jobs with same filename can corrupt outputs.
- Enterprise scripts need deterministic but safe outputs.

Action:

- Create shared output writer module.
- Use atomic temp writes and collision-safe naming for GUI, CLI, MCP, and async jobs.
- Add an explicit `overwrite` option only when requested.

Expected new module:

- `backend/app/services/output_writer.py`

Acceptance:

- Two jobs with same original filename in same output dir produce distinct outputs by default.
- `--output-path` refuses overwrite unless explicit `--overwrite` is provided.
- Manifest contains final resolved path.

### 6.5 `UniversalConversionResult.assets` is dropped

Current behavior:

- `UniversalConversionResult` has `assets`, but `to_legacy_envelope()` drops it.
- Downstream `_finalize_job()` only sees text/images/metadata.

Why it matters:

- Spreadsheet, archive, media, and future converters may produce non-image sidecars.
- Enterprise pipelines need full artifact manifests.

Action:

- Add `assets` to the legacy envelope or remove the legacy envelope from final writer path.
- Persist assets through `TaskManager`, `agent_api._save_result()`, and download packaging.

Expected files:

- `backend/app/conversion/result.py`
- `backend/app/services/task_manager.py`
- `backend/app/agent_api.py`
- `backend/app/routes/convert.py`

Acceptance:

- A converter can return `Asset(...)` and it is written to disk and listed in manifest.
- Tests cover bytes assets and image assets.

### 6.6 Mixed PDF routing may be unsafe if it only uses sampled pages

Current behavior:

- `probe_pdf()` samples first three pages plus last page by default.
- `plan_pdf_routing_segments()` groups only `probe.page_results`.
- `ConversionService` can execute mixed PDF routing from those segment results.

Risk:

- If only sampled pages are segmented, unsampled pages may be omitted or misrepresented.
- Mixed routing is powerful but must not run unless there is a full page map.

Action:

- Add `probe_pdf(..., full_page_map=True)` for mixed routing or large-doc calibrated page map.
- Only enable actual mixed routing when `probe_result.page_results` covers every page or the user explicitly requests experimental behavior.
- For normal plan output, keep sampled segment preview labelled as sampled.

Acceptance:

- Mixed routing execution never drops unsampled pages.
- Unit test creates a multi-page PDF and verifies all pages appear exactly once in mixed routing.
- Plan response distinguishes `sampled_mixed_engine_segments` from `mixed_engine_segments` if needed.

### 6.7 MCP `openWorldHint` is inaccurate for URL conversion tools

Current behavior:

- `marker_convert_file` can fetch `source_url`.
- Tool annotation currently uses `openWorldHint=False`.

Why it matters:

- MCP annotations guide host/client safety decisions.
- A tool that interacts with arbitrary URLs is open-world.

Action:

- Split local and URL tools:
  - `marker_convert_local_file`: `openWorldHint=False`
  - `marker_convert_url`: `openWorldHint=True`
  - same split for plan/submit if URL support exists.

Alternative:

- Keep one tool but set `openWorldHint=True`. Splitting is better.

Acceptance:

- URL-capable tools have `openWorldHint=True`.
- Local-only tools keep `openWorldHint=False`.
- Docs teach agents to use local tools by default.

### 6.8 `marker_submit_job` does not have full parity with `marker_convert_file`

Current behavior:

- `marker_convert_file` exposes image-understanding router/dedup/batch/OCR knobs.
- `marker_submit_job` exposes fewer image-understanding knobs.

Why it matters:

- For long enterprise jobs, async job path should be the preferred path.
- Async path must not be less configurable than one-shot conversion.

Action:

- Generate both tools from the same option schema.
- Keep shorter aliases if desired, but full options must be reachable.

Acceptance:

- Any option accepted by `convert` can be accepted by `submit-job`, unless explicitly marked one-shot-only.

### 6.9 Global Anthropic base URL mutation can leak across jobs

Current behavior:

- `build_marker_options()` sets `os.environ["ANTHROPIC_BASE_URL"]` for `custom_anthropic`.

Why it matters:

- Concurrent jobs using different providers/base URLs can bleed settings across each other.
- Enterprise multi-tenant behavior must avoid global mutable state.

Action:

- Replace global env mutation with provider-specific client/service construction if supported.
- If unavoidable, isolate custom Anthropic jobs in a worker process with environment scoped before startup and never shared.
- Add concurrency test with two custom Anthropic providers.

Acceptance:

- Concurrent provider jobs cannot affect each other's base URL.

### 6.10 CLI error/exit behavior is not yet enterprise-grade

Current behavior:

- Most exceptions print `Error: ...` to stderr and exit 1.
- Usage errors exit 2 through argparse.
- JSON mode still emits non-JSON errors.

Why it matters:

- Scripts need machine-readable failures.
- Batch systems need stable exit codes.
- Agents need structured error payloads.

Action:

- Implement typed `MarkerError` taxonomy and map to CLI exit codes.
- In `--json` mode, all errors should be JSON to stdout or stderr consistently; recommendation: primary result to stdout, diagnostics/errors to stderr, with `--json` errors as one JSON object on stderr.
- Add `--debug` stack traces gated by flag.

Acceptance:

- `marker convert missing.pdf --json` returns stable JSON error and exit code `3`.
- No stack trace unless `--debug`.

### 6.11 MCP settings tools are too powerful for default agent usage

Current behavior:

- MCP includes tools to list/get/set/delete settings.
- Sensitive reads are masked, but settings writes/deletes can change runtime behavior.

Why it matters:

- Agents may accidentally or maliciously alter provider keys, GPU settings, cloud gates, or runtime knobs.
- Enterprise hosts need least privilege.

Action:

- Add MCP scopes and local policy flags.
- Make settings write/delete disabled by default for remote HTTP unless explicitly enabled.
- Require human confirmation/elicitation for settings write/delete in supporting clients.
- Add audit events for all settings changes.

Acceptance:

- Default local stdio can read masked settings but cannot write secrets unless a config flag allows it.
- Remote streamable HTTP requires a token with `settings:write` scope.

### 6.12 URL SSRF protections are good but should be hardened

Current behavior:

- Code validates scheme, credentials, DNS result, and blocks private/local ranges.
- Redirects are validated per hop.
- Download size is bounded.

Remaining enterprise hardening:

- DNS rebinding and time-of-check/time-of-use risk.
- Need optional enterprise egress allowlist/denylist.
- Need blocked cloud metadata IPs tested explicitly.
- Need content sniffing and decompression-bomb protection for archives.

Action:

- Add a `SafeURLFetcher` module with DNS pinning or controlled resolver.
- Disable redirects by default or make redirect policy explicit.
- Add host/domain allowlist mode.
- Add tests for localhost, RFC1918, link-local, metadata IPs, IPv6, redirects to private IPs, credentials in URL, large response, unsupported content-type.

Acceptance:

- SSRF test suite passes.
- URL conversion tools clearly identify external network use in manifest.

### 6.13 API authentication is not first-class

Current behavior:

- Docs state API authentication is not implemented and should be set up at Nginx/reverse proxy layer.
- MCP HTTP has bearer token guard for non-loopback.

Why it matters:

- Enterprise deployments need product-supported auth, not only external docs.

Action:

- Keep reverse proxy auth as supported deployment mode, but add optional built-in API auth middleware for REST/MCP service mode.
- Support static token for local/simple deployments and OIDC/JWT validation for enterprise.
- Add RBAC scopes aligned with MCP tool scopes.

Acceptance:

- `/api/*` can be protected with `MARKER_API_AUTH_MODE=token|oidc|none`.
- Health endpoint can stay public; readiness/version can be policy-controlled.

### 6.14 Queue and job state are not durable enough for enterprise batch

Current behavior:

- Task queue/progress is in-memory.
- On restart, stale pending/processing jobs are marked failed.

Why it matters:

- Enterprise batch workflows need resume, retries, idempotency, priorities, and cancellation across restarts.

Action:

- Keep in-memory mode as local default.
- Add pluggable durable job backend for enterprise: SQLite-durable first, then Redis/Postgres optional.
- Add job leases, retry policy, idempotency keys, priority, and resume semantics.

Acceptance:

- A submitted job has a persistent status transition log.
- Restart does not silently lose queued jobs; policy determines fail/retry/resume.

### 6.15 Observability is incomplete

Current behavior:

- `/api/health` returns `status: ok`.
- Logs and SSE exist.

Needed:

- `/healthz` liveness.
- `/readyz` readiness including DB writable, output dir writable, model state, worker state.
- `/metrics` Prometheus/OpenTelemetry metrics.
- `/version` build info.
- Structured logs with job_id, request_id, source type, engine, duration, bytes, output chars, cloud usage, cost.
- Audit event table.

Acceptance:

- Every conversion has traceable job lifecycle events.
- CLI/MCP can fetch health/diagnostics.

---

## 7. Target architecture for the final product

### 7.1 Target control-plane/data-plane split

```text
                    +----------------------+
                    |  Human / Agent / CI  |
                    +-----------+----------+
                                |
             +------------------+------------------+
             |                  |                  |
             v                  v                  v
        React GUI             CLI v1             MCP v1
             |                  |                  |
             +------------------+------------------+
                                |
                                v
                     Agent Contract Layer
             Pydantic schemas / JSON Schema / errors
             options metadata / output manifest schema
                                |
                                v
                      Conversion Orchestrator
             router / registry / policies / provenance
                                |
          +---------------------+----------------------+
          |                                            |
          v                                            v
 deterministic converters                         marker workers
 office/data/html/archive/audio/video             PDF/OCR/image/VLM
          |                                            |
          +---------------------+----------------------+
                                |
                                v
                    Output Writer + Manifest
                    text / assets / hashes / quality
                                |
                                v
                 Job Store + Audit + Telemetry + APIs
```

### 7.2 Target shared modules

Add or refactor toward these modules:

```text
backend/app/agent_contract.py
    Typed request/response schemas, JSON schema export, option metadata.

backend/app/errors.py
    MarkerError base class, error codes, exit-code mapping, safe serialization.

backend/app/services/output_writer.py
    Atomic output writing, path policy, manifest creation, asset persistence.

backend/app/services/policy.py
    Workspace roots, cloud/network policy, destructive-action policy.

backend/app/services/safe_url_fetcher.py
    SSRF-hardened URL fetcher and content validation.

backend/app/services/audit.py
    Audit event model and sink.

backend/app/services/telemetry.py
    Structured logs, metrics, traces, job spans.

backend/app/cli_main.py or backend/app/cli/
    Richer CLI command tree, generated from contract metadata where possible.

backend/app/mcp_server.py
    Thin MCP wrapper over agent contract, resources, prompts, and tools.
```

### 7.3 Contract-first principle

Every user/agent-facing operation should be defined once in the contract layer:

- Request model.
- Response model.
- Error codes.
- Option descriptions.
- Defaults.
- Validation.
- Security category.
- Tool annotations.
- CLI flag mapping.
- JSON schema export.

Then generate or wire:

- CLI parser flags.
- MCP tool schemas.
- REST schemas.
- Docs tables.
- Tests/snapshots.

This prevents the current drift between CLI, MCP, and REST.

---

## 8. Universal output manifest

### 8.1 Why this is required

Enterprise users and agents need more than a text path. They need to know:

- What was converted.
- Which engine was used.
- Which fallbacks occurred.
- What files were written.
- Which assets exist.
- Whether cloud calls happened.
- Whether external URLs were fetched.
- Output sizes and hashes.
- Warnings and quality signals.
- How to page through output safely.

### 8.2 Manifest file name

For every conversion, write:

```text
<output-stem>.marker.json
```

If the output is a directory:

```text
<output-dir>/manifest.marker.json
```

### 8.3 Manifest schema v1

```json
{
  "schema_version": "marker.output_manifest.v1",
  "marker_version": "0.1.0",
  "job_id": "11111111-1111-4111-8111-111111111111",
  "created_at": "2026-06-25T00:00:00Z",
  "input": {
    "source_type": "local_path",
    "original_name": "document.pdf",
    "path": "/workspace/document.pdf",
    "source_url": null,
    "sha256": "...",
    "size_bytes": 123456,
    "extension": ".pdf",
    "mime_type": "application/pdf"
  },
  "request": {
    "output_format": "markdown",
    "conversion_profile": "auto",
    "image_handling_mode": "extraction",
    "allow_cloud_vlm": false,
    "page_range": null,
    "options_redacted": {}
  },
  "engine": {
    "selected": "marker_pdf",
    "label": "Marker PDF",
    "confidence": 1.0,
    "execution_backend": "marker_worker",
    "fallback_chain": [],
    "fallbacks_taken": []
  },
  "routing": {
    "probe_result": null,
    "mixed_engine_segments": []
  },
  "output": {
    "format": "markdown",
    "text_path": "/output/document.md",
    "media_type": "text/markdown",
    "text_chars": 10000,
    "sha256": "...",
    "assets_dir": "/output/document_assets",
    "assets": [
      {
        "path": "/output/document_assets/image1.png",
        "relative_path": "document_assets/image1.png",
        "media_type": "image/png",
        "size_bytes": 1234,
        "sha256": "...",
        "kind": "image"
      }
    ]
  },
  "quality": {
    "warnings": [],
    "page_count": null,
    "coverage_score": null,
    "table_count": null,
    "image_understanding_count": 0
  },
  "security": {
    "external_network_used": false,
    "cloud_vlm_used": false,
    "cloud_provider": null,
    "workspace_root": "/workspace",
    "policy_profile": "local_default"
  },
  "timing": {
    "duration_ms": 0,
    "started_at": "2026-06-25T00:00:00Z",
    "completed_at": "2026-06-25T00:00:00Z"
  }
}
```

### 8.4 Required manifest behavior

- Always write a manifest for successful conversions.
- For failed conversions, write a failure manifest when output location is known.
- The manifest must redact secrets.
- The manifest must include whether any cloud or external network was used.
- The manifest must include output hashes for reproducibility.
- CLI and MCP responses should return manifest path.
- MCP should expose the manifest as a resource.

---

## 9. Error taxonomy and CLI exit codes

### 9.1 Error schema

```json
{
  "ok": false,
  "schema_version": "marker.error.v1",
  "error": {
    "code": "INPUT_NOT_FOUND",
    "message": "Input file not found: /path/to/file.pdf",
    "hint": "Check the path or pass --source-url for remote files.",
    "details": {
      "path": "/path/to/file.pdf"
    },
    "retryable": false
  }
}
```

### 9.2 Recommended error codes

```text
USAGE_ERROR
INPUT_NOT_FOUND
INPUT_NOT_ALLOWED
UNSUPPORTED_FORMAT
OUTPUT_EXISTS
OUTPUT_WRITE_FAILED
CONFIG_ERROR
AUTH_REQUIRED
AUTH_FORBIDDEN
NETWORK_BLOCKED
NETWORK_FETCH_FAILED
URL_UNSAFE
CONVERSION_FAILED
MODEL_NOT_READY
MODEL_DOWNLOAD_FAILED
CLOUD_NOT_ALLOWED
TIMEOUT
CANCELLED
PARTIAL_FAILURE
INTERNAL_ERROR
```

### 9.3 Recommended exit codes

```text
0  success
1  internal or uncategorized failure
2  usage error / invalid arguments
3  input not found
4  unsupported format or input not allowed
5  config error
6  auth required or forbidden
7  network blocked or fetch failed
8  conversion failed
9  timeout or cancelled
10 partial batch failure
11 output exists or output write failed
```

### 9.4 CLI rules

- Primary output goes to stdout.
- Progress, warnings, and diagnostics go to stderr.
- In `--json` mode, successful results are JSON on stdout.
- In `--json` mode, errors are JSON on stderr and exit nonzero.
- No stack traces unless `--debug`.
- `--quiet` suppresses non-error stderr.
- `--verbose` adds diagnostic messages.
- `--trace` or `--debug` includes stack traces and internal details.

---

## 10. CLI v1 product specification

### 10.1 Goals

The CLI exists for two reasons:

1. Scripting and enterprise automation.
2. MCP and agent integration bootstrap.

Therefore it must be reliable in shells, CI/CD, cron jobs, batch processing, and agent sandboxes.

### 10.2 Target command tree

Keep existing commands, but expand toward this v1 tree:

```text
marker --version
marker doctor [--json]
marker capabilities [--json]
marker schema export [--format json|markdown] [--output PATH]

marker plan INPUT [--json] [--profile auto|fast|high_accuracy]
marker convert INPUT [--output-dir DIR | --output-path PATH] [--format markdown|json|html|chunks] [--json]
marker batch MANIFEST [--concurrency N] [--resume] [--progress text|ndjson] [--json]

marker output read PATH_OR_JOB_ID [--offset N] [--limit N] [--json]
marker output manifest PATH_OR_JOB_ID [--json]

marker jobs list [--page N] [--page-size N] [--json]
marker jobs status JOB_ID [--include-result-text] [--json]
marker jobs watch JOB_ID [--progress text|ndjson]
marker jobs cancel JOB_ID [--json]
marker jobs delete JOB_ID [--keep-files] [--yes] [--json]

marker settings list [--category NAME] [--json]
marker settings get KEY [--json]
marker settings set KEY VALUE [--category NAME] [--json]
marker settings delete KEY [--yes] [--json]

marker config path
marker config get KEY [--json]
marker config set KEY VALUE
marker config validate [--json]

marker server start [--host HOST] [--port PORT]
marker server status [--json]
marker server stop

marker mcp start [--transport stdio|streamable-http] [--host HOST] [--port PORT]
marker mcp init-config --client codex|claude|gemini|opencode|antigravity
marker mcp inspect [--json]
marker mcp self-test [--json]

marker eval run CORPUS_DIR [--profile NAME] [--json]
```

### 10.3 Backward compatibility

Existing commands should remain valid:

- `marker self-test`
- `marker capabilities`
- `marker plan`
- `marker convert`
- `marker submit-job`
- `marker read-output`
- `marker jobs`
- `marker job-status`
- `marker delete-job`
- `marker settings ...`
- `marker mcp`

Add new grouped commands as aliases or next-generation paths. Emit deprecation warnings only after docs are updated.

### 10.4 Input modes

Support these input modes:

```text
marker convert /path/file.pdf
marker convert ./file.pdf
marker convert --source-url https://example.com/file.pdf
marker convert - < file.txt
cat file.txt | marker convert - --filename notes.txt
```

Rules:

- `-` means stdin for streamable text/data inputs only at first.
- Binary stdin can be supported later, but must require `--filename` for extension.
- Source URL fetch should be explicit through `--source-url` or a URL-looking positional input with confirmation/policy.
- Local paths should respect workspace roots when policy is enabled.

### 10.5 Output modes

Support:

```text
marker convert input.pdf --output-dir out/
marker convert input.pdf --output-path out/input.md
marker convert input.pdf --stdout
marker convert input.pdf --manifest
marker convert input.pdf --json
```

Rules:

- `--stdout` writes converted text only to stdout and assets to a temp or specified dir.
- Default for binary/asset-producing conversions should write files, not stdout.
- `--output-path` must not overwrite unless `--overwrite`.
- `--output-dir` should create collision-safe output names by default.
- Always return manifest path in JSON output.

### 10.6 Batch mode

Batch mode is essential for enterprise scripting.

#### JSONL manifest input

```jsonl
{"input":"/docs/a.pdf","output_dir":"/out","output_format":"markdown","conversion_profile":"auto"}
{"input":"/docs/b.docx","output_dir":"/out","output_format":"markdown"}
{"source_url":"https://example.com/c.pdf","output_dir":"/out","allow_external_network":true}
```

#### Batch CLI behavior

```text
marker batch jobs.jsonl --concurrency 4 --resume --progress ndjson --json
```

Requirements:

- Per-row result file or NDJSON result stream.
- `--resume` skips rows with existing successful manifest unless `--force`.
- `--continue-on-error` default true for batch; final exit code 10 if partial failures.
- Stable row IDs: explicit `id` or hash of input/options.
- Progress in NDJSON for CI/agents.
- Summary JSON at end.

#### Batch result schema

```json
{
  "schema_version": "marker.batch_result.v1",
  "ok": false,
  "total": 100,
  "succeeded": 97,
  "failed": 3,
  "skipped": 0,
  "results_path": "/out/batch-results.jsonl",
  "failed_path": "/out/batch-failed.jsonl"
}
```

### 10.7 Remote server mode

The CLI should be able to talk to a running Marker server instead of always importing backend code locally.

Config:

```text
MARKER_SERVER_URL=http://127.0.0.1:8000
MARKER_API_TOKEN=...
```

Flags:

```text
marker convert file.pdf --server-url http://server:8000 --token-env MARKER_API_TOKEN
marker jobs list --server-url http://server:8000 --json
```

Behavior:

- Local direct mode remains default.
- Remote mode uses REST API.
- Remote mode handles uploads, URLs, local server paths explicitly.
- Never assume a local client path exists on the remote server unless `--server-local-path` is explicitly used.

### 10.8 Config precedence

Use this precedence:

1. CLI flags.
2. Environment variables.
3. Project config file.
4. User config file.
5. System config file.
6. Built-in defaults.

Suggested config locations:

```text
./marker.toml
~/.config/marker-ui/config.toml
/etc/marker-ui/config.toml
```

Do not store secrets in plain config by default. Use encrypted settings, OS keyring, or secret manager integrations.

### 10.9 CLI polish checklist

Implement:

- `--help` with examples.
- `--version` with package version, backend version, marker-pdf version, Python version.
- Shell completions for bash/zsh/fish/PowerShell.
- `--no-input` for CI.
- `--yes` only for destructive actions.
- `--dry-run` for delete/settings/batch operations.
- `--quiet`, `--verbose`, `--debug`.
- TTY detection for progress bars.
- No color when not TTY or when `NO_COLOR` is set.
- Machine-readable JSON schemas for all `--json` outputs.
- No secrets in command arguments recommendation; prefer env/stdin/keyring.

---

## 11. MCP v1 product specification

### 11.1 MCP design intent

The MCP server should make Marker safely usable by agents as a document perception tool. It should not be just a CLI wrapper. It should provide:

- Tools for actions.
- Resources for outputs/job state/docs.
- Prompts for repeatable workflows.
- Roots/workspace support.
- Strong schemas.
- Least-privilege authorization.
- Safe defaults.

### 11.2 Split tools by safety boundary

Replace broad tools with smaller tools where safety semantics differ.

#### Capability and diagnostics tools

```text
marker_get_capabilities
marker_self_test
marker_get_health
marker_get_version
```

#### Planning tools

```text
marker_plan_local_file
marker_plan_url
```

- Local file plan: `openWorldHint=false`.
- URL plan: `openWorldHint=true`.

#### Conversion tools

```text
marker_convert_local_file
marker_convert_url
marker_submit_local_job
marker_submit_url_job
```

- URL tools must be `openWorldHint=true`.
- Cloud VLM must default false.
- Tools should return `structuredContent` plus resource links where supported.

#### Output tools

```text
marker_read_output
marker_get_output_manifest
marker_list_output_assets
marker_read_output_chunk
```

#### Job tools

```text
marker_list_jobs
marker_get_job_status
marker_cancel_job
marker_delete_job
```

- Delete is destructive.
- Cancel may be destructive enough to require confirmation in some hosts.

#### Settings tools

```text
marker_list_settings
marker_get_setting
marker_set_setting
marker_delete_setting
```

- Settings write/delete should be disabled unless policy allows.
- Secrets should not be retrievable, only set/delete/list masked.

### 11.3 MCP resources

Add resources so agents can browse state without repeatedly calling tools.

```text
marker://capabilities
marker://health
marker://version
marker://jobs
marker://jobs/{job_id}
marker://jobs/{job_id}/manifest
marker://jobs/{job_id}/output
marker://jobs/{job_id}/assets
marker://outputs/{output_id}/manifest
marker://docs/agent-guide
marker://docs/options
marker://settings
```

Resource behavior:

- Large resources must support pagination or ranged reads.
- Output text should be exposed as chunks, not one unbounded blob.
- Resources should include MIME types.
- Secrets must be masked.

### 11.4 MCP prompts

Add prompts for common agent workflows. These should be user-controlled templates, not hidden policy.

```text
convert_for_rag
extract_tables_from_document
summarize_converted_document_with_citations
convert_and_compare_two_documents
batch_convert_folder
inspect_conversion_quality
convert_audio_to_meeting_notes
extract_figures_and_diagrams
```

Example prompt: `convert_for_rag`

Inputs:

```json
{
  "input_path": "/workspace/report.pdf",
  "output_dir": "/workspace/out",
  "quality": "auto",
  "allow_cloud_vlm": false
}
```

Prompt behavior:

1. Call capabilities.
2. Plan conversion.
3. Convert or submit job depending on size/profile.
4. Read output in chunks.
5. Return manifest, warnings, and suggested next steps.

### 11.5 MCP tool annotations

Use annotations accurately:

```text
Capabilities/read/status/read-output/settings-read:
  readOnlyHint=true
  destructiveHint=false
  idempotentHint=true
  openWorldHint=false

Local convert/submit:
  readOnlyHint=false
  destructiveHint=false
  idempotentHint=false
  openWorldHint=false

URL convert/submit:
  readOnlyHint=false
  destructiveHint=false
  idempotentHint=false
  openWorldHint=true

Delete job/delete setting:
  readOnlyHint=false
  destructiveHint=true
  idempotentHint=false or true depending behavior
  openWorldHint=false

Set setting:
  readOnlyHint=false
  destructiveHint=false
  idempotentHint=true
  openWorldHint=false
```

Remember: MCP annotations are hints, not security controls. Enforce policies server-side.

### 11.6 MCP authorization model

Current static token behavior is fine for local/dev MVP. Enterprise mode needs scoped auth.

Suggested scopes:

```text
read:capabilities
read:health
convert:local
convert:url
jobs:read
jobs:write
outputs:read
settings:read
settings:write
admin:delete
audit:read
```

Policy:

- stdio transport: use environment/config credentials; no OAuth flow.
- streamable HTTP loopback: may allow no auth in local developer mode.
- streamable HTTP non-loopback: require auth.
- production HTTP: require TLS or trusted reverse proxy.
- validate token audience/resource.
- do not accept tokens intended for upstream APIs.
- do not pass through client tokens to external services.

### 11.7 MCP roots and workspace sandboxing

Support MCP roots and local policy roots.

Rules:

- If client provides roots, local file tools must stay inside roots unless server policy allows broader access.
- Server-side `MARKER_WORKSPACE_ROOTS` should restrict local paths even if the client has broad filesystem access.
- `marker_read_output` must not read arbitrary local files. It should read only registered output paths or paths under allowed output roots.
- Deletion tools must only delete files owned by Marker job records unless admin policy permits broader behavior.

Suggested environment variables:

```text
MARKER_WORKSPACE_ROOTS=/workspace,/data/input
MARKER_OUTPUT_ROOT=/data/output
MARKER_ALLOW_ARBITRARY_LOCAL_PATHS=false
MARKER_ALLOW_URL_FETCH=false
MARKER_ALLOW_CLOUD_VLM=false
```

### 11.8 MCP elicitation and consent

Use elicitation where clients support it, especially for:

- Cloud VLM enablement.
- External URL fetches.
- Settings writes/deletes.
- Deleting output files.
- Reading outside roots, if ever allowed.

Never use elicitation to ask for raw API keys. Secrets should be set through explicit settings command, CLI, UI, env, or secret manager.

---

## 12. Enterprise hardening plan

### 12.1 Authentication and authorization

Add optional auth middleware for REST and MCP HTTP.

Modes:

```text
none       local development only
static     bearer token from env/secret file
oidc       JWT validation against issuer/JWKS
proxy      trusted reverse proxy headers
```

Required features:

- Scope-based authorization.
- Per-route/tool scope requirements.
- Audit logging of denied requests.
- Separate admin scope for deletion/settings writes.
- Clear docs for Nginx, Caddy, Traefik, and Kubernetes ingress.

### 12.2 Audit logging

Add audit event table or log sink.

Events:

```text
job.submitted
job.started
job.completed
job.failed
job.cancelled
job.deleted
output.read
output.deleted
settings.read
settings.set
settings.deleted
url.fetch.started
url.fetch.blocked
url.fetch.completed
cloud_vlm.requested
cloud_vlm.blocked
cloud_vlm.used
auth.denied
policy.denied
```

Audit fields:

```json
{
  "event_id": "...",
  "timestamp": "...",
  "actor": "user/service/client id",
  "transport": "gui|rest|cli|mcp-stdio|mcp-http",
  "request_id": "...",
  "job_id": "...",
  "source_type": "local|url|upload",
  "action": "job.submitted",
  "allowed": true,
  "reason": null,
  "metadata_redacted": {}
}
```

### 12.3 Durable queue

Add pluggable queue backend.

Local default:

```text
in_memory
```

Enterprise options:

```text
sqlite_durable
redis
postgres
```

Durable job requirements:

- persisted queue state
- retries
- retry backoff
- idempotency key
- priority
- lease/heartbeat
- worker crash recovery
- cancellation across restart
- queue pause/resume
- max concurrency per engine/provider/GPU

### 12.4 Database options

Keep SQLite as default, but add Postgres support for enterprise.

Requirements:

- Alembic migrations must support SQLite and Postgres.
- Use UUID columns where possible.
- Add indexes for status, created_at, actor, source hash, output hash.
- Avoid storing large `result_text` in DB for enterprise mode; store path/blob reference and preview.

### 12.5 Observability

Add structured logs:

```json
{
  "level": "info",
  "message": "conversion completed",
  "request_id": "...",
  "job_id": "...",
  "engine": "marker_pdf",
  "duration_ms": 12345,
  "input_bytes": 123456,
  "output_chars": 9999,
  "cloud_vlm_used": false,
  "status": "completed"
}
```

Add metrics:

```text
marker_jobs_submitted_total
marker_jobs_completed_total
marker_jobs_failed_total
marker_jobs_cancelled_total
marker_conversion_duration_seconds
marker_conversion_input_bytes
marker_conversion_output_chars
marker_queue_depth
marker_worker_active_jobs
marker_gpu_worker_count
marker_vlm_requests_total
marker_vlm_cost_usd_total
marker_url_fetch_blocked_total
marker_policy_denied_total
```

Add traces:

```text
conversion.request
conversion.plan
conversion.probe_pdf
conversion.route
conversion.convert
conversion.image_understanding
conversion.output_write
conversion.finalize
```

Add endpoints:

```text
/api/healthz
/api/readyz
/api/metrics
/api/version
/api/diagnostics
```

### 12.6 Deployment polish

Add deployment artifacts:

- CPU Docker image.
- CUDA Docker image.
- Docker Compose profiles: `cpu`, `gpu`, `mcp`, `server`.
- Helm chart.
- systemd service file.
- Nginx/Caddy examples with TLS and auth.
- Offline model cache instructions.
- Air-gapped install guide.
- Backup/restore guide for DB, settings, output, and model cache.

### 12.7 Secret management

Current Fernet encryption is good for local mode. Enterprise should add pluggable secret providers:

```text
local_encrypted_db
os_keyring
file_env
hashicorp_vault
aws_secrets_manager
azure_key_vault
gcp_secret_manager
kubernetes_secret
```

Requirements:

- Never expose raw secrets through MCP/REST/CLI reads.
- Secrets in manifests must be redacted.
- Prefer setting secrets by env, UI, secret manager, or stdin, not CLI flags.
- Audit secret writes without logging values.

---

## 13. Conversion quality and evaluation plan

### 13.1 Why evaluation matters

The product depends on routing decisions. Routing must be measured, not guessed. Fast paths are valuable only if quality stays high. Enterprise users will trust the system if every conversion has quality signals and every routing change is benchmarked.

### 13.2 Evaluation corpus

Create `eval/corpus/` with categories:

```text
pdf_digital_text_clean
pdf_scanned
pdf_sandwich_ocr
pdf_tables_heavy
pdf_multicolumn
pdf_scientific_equations
pdf_financial_reports
pdf_slides_exported_to_pdf
pdf_mixed_clean_and_scanned
docx_simple
docx_tables_images
pptx_charts
xlsx_multi_sheet
csv_large
json_nested
html_article
zip_nested
audio_meeting
audio_low_quality
video_presentation
images_charts_diagrams
```

### 13.3 Metrics

Track:

```text
text coverage
page coverage
table preservation
heading structure
reading order
image extraction count
image understanding accuracy
equation preservation
link preservation
metadata/provenance completeness
conversion duration
GPU memory usage
cloud cost
fallback rate
failure rate
```

### 13.4 Quality report

Every eval run should output:

```text
eval-report.json
eval-report.md
eval-results.jsonl
artifacts/
```

### 13.5 Router calibration

For PDF routing:

- Validate LiteParse vs Marker output quality on clean PDFs.
- Validate Marker fallback on scanned/sandwich/complex PDFs.
- Calibrate thresholds from current hardcoded values.
- For mixed routing, require full-page routing map.
- Track false-fast and false-deep rates.

### 13.6 Conversion quality gates in CI

Add lightweight CI gates:

- deterministic fixtures only
- no cloud calls
- no heavy model load by default
- mocked Marker where needed
- separate nightly/manual heavy eval for real models/VLM

---

## 14. Security plan

### 14.1 Policy profiles

Add named policy profiles:

```text
local_default
agent_safe
enterprise_locked_down
enterprise_with_url_fetch
enterprise_with_cloud_vlm
```

Example `agent_safe`:

```toml
[policy]
allow_arbitrary_local_paths = false
allow_url_fetch = false
allow_cloud_vlm = false
allow_settings_write = false
allow_delete_files = false
workspace_roots = ["/workspace"]
output_root = "/workspace/.marker-output"
```

### 14.2 Local path security

- Restrict reads to workspace roots when configured.
- Resolve symlinks before policy checks.
- Prevent path traversal.
- Do not let `read_output` read arbitrary files.
- Store job-owned output paths and only read those by default.

### 14.3 URL fetch security

Add `SafeURLFetcher`:

- Only http/https.
- No credentials in URL.
- Optional domain allowlist.
- Block private/local/link-local/multicast/reserved/metadata IPs.
- Validate every redirect target.
- DNS pinning or controlled resolver.
- Timeout and size caps.
- Content type and extension validation.
- Download to temp path, then atomic rename.
- Log/audit every URL fetch.

Test cases:

```text
http://127.0.0.1/file.pdf
http://localhost/file.pdf
http://169.254.169.254/latest/meta-data
http://10.0.0.1/file.pdf
http://172.16.0.1/file.pdf
http://192.168.1.1/file.pdf
http://[::1]/file.pdf
redirect from public to private
URL with username/password
unsupported content type
response larger than limit
slowloris timeout
```

### 14.4 Cloud/VLM safety

- Cloud VLM default false everywhere.
- Output manifest states if cloud VLM was used.
- MCP tools require explicit `allow_cloud_vlm=true` plus policy allow.
- GUI/CLI/MCP docs should repeat that images may leave the machine only when explicitly allowed.
- Audit event when cloud is requested, blocked, or used.

### 14.5 MCP-specific threats

Mitigate:

- Tool poisoning: stable schemas, docs, and explicit tool descriptions.
- Tool shadowing: unique names and clear server identity.
- Confused deputy: scoped auth, no token passthrough, audience validation.
- Data exfiltration: roots, output access restrictions, URL/cloud gates.
- Rug pull: versioned tool schemas and schema diff tests.
- Over-scoping: settings and delete tools disabled by default in remote mode.

---

## 15. Implementation backlog for autonomous agent

The following task IDs are intended to be executed in order. P0 items should be completed before major CLI/MCP expansion.

### UCM-001: Add contract module

Priority: P0
Status: [Done]

Intent:

Create one typed contract layer for CLI, MCP, REST, and docs.

Expected files:

```text
backend/app/agent_contract.py
backend/tests/test_agent_contract.py
```

Implement:

- `ConversionOptionsModel` [Done]
- `PlanRequestModel` [Done]
- `PlanResultModel` [Done]
- `ConvertRequestModel` [Done]
- `ConvertResultModel` [Done]
- `SubmitJobRequestModel` [Done]
- `JobStatusModel` [Done]
- `OutputManifestModel` [Done]
- `MarkerErrorModel` [Done]
- `BatchRequestModel` [Done]
- `BatchResultModel` [Done]
- option metadata registry [Done]
- JSON schema export function [Done]

Acceptance:

- JSON schemas can be exported without importing Marker heavy models. [Done]
- CLI/MCP can import models without loading neural weights. [Done]
- Tests snapshot the exported schema. [Done]

### UCM-002: Add MarkerError taxonomy

Priority: P0

Status: [Done]

Expected files:

```text
backend/app/errors.py
backend/app/cli.py
backend/app/agent_api.py
backend/app/mcp_server.py
backend/tests/test_errors.py
```

Implement:

- `MarkerError` base class.
- Concrete errors listed in section 9.
- Safe serialization.
- CLI exit code mapping.
- MCP conversion of known errors into structured tool errors.

Acceptance:

- Missing input produces `INPUT_NOT_FOUND` and CLI exit 3.
- Unsupported suffix produces `UNSUPPORTED_FORMAT` and CLI exit 4.
- URL blocked produces `URL_UNSAFE` or `NETWORK_BLOCKED` and CLI exit 7.

### UCM-003: Shared output writer and manifest

Priority: P0
Status: [Done]

Expected files:

```text
backend/app/services/output_writer.py
backend/app/agent_api.py
backend/app/services/task_manager.py
backend/app/routes/convert.py
backend/tests/test_output_writer.py
```

Implement:

- Atomic text writes. [Done]
- Asset writing. [Done]
- Collision-safe path selection. [Done]
- Optional overwrite. [Done]
- Manifest creation. [Done]
- Output hash calculation. [Done]
- Redaction of sensitive config. [Done]
- Shared sync CLI/MCP and async job finalization writer. [Done]

Acceptance:

- Sync CLI conversion and async job conversion use the same writer. [Done]
- Two jobs with same file do not overwrite by default. [Done]
- Manifest is created for every successful conversion. [Done]

### UCM-004: Fix known schema/metadata/config defects

Priority: P0

Status: [Done]

Subtasks:

1. `DeleteJobOutput.files_removed` list type. [Done]
2. Persist `mixed_engine_segments`. [Done]
3. Wire `MARKER_MAX_UPLOAD_SIZE_MB` to code. [Done]
4. Fix `UniversalConversionResult.assets` persistence. [Done]
5. Remove or isolate global `ANTHROPIC_BASE_URL` mutation. [Done]
6. Serialize shared Marker/Surya predictor use in the single-process backend while preserving parallel CPU converter execution and honest queue status for waiting marker jobs. [Done]

Acceptance:

- Targeted tests pass for all six fixes.
- Existing docs/examples still work.
- Shared model conversions cannot overlap in the default thread backend, CPU-only conversions still route to the CPU pool, and queued marker jobs report a wait message instead of pretending they have started.

### UCM-005: Harden URL fetching

Priority: P0/P1
Status: [Done]

Expected files:

```text
backend/app/services/safe_url_fetcher.py
backend/app/routes/convert.py
backend/app/agent_api.py
backend/tests/test_safe_url_fetcher.py
```

Implement:

- Central safe fetcher. [Done]
- DNS/redirect/private IP protection. [Done]
- Configurable allowlist. [Done]
- Size/time/content limits. [Done]
- Audit hooks. [Done]

Acceptance:

- SSRF test cases pass. [Done]
- Existing `source_url` behavior remains supported for safe URLs. [Done]

### UCM-006: Workspace roots and output access policy

Priority: P1
Status: [Done]

Expected files:

```text
backend/app/services/policy.py
backend/app/agent_api.py
backend/app/mcp_server.py
backend/app/routes/convert.py
backend/tests/test_policy_roots.py
```

Implement:

- `MARKER_WORKSPACE_ROOTS`. [Done]
- `MARKER_OUTPUT_ROOT`. [Done]
- symlink-resolved path checks. [Done]
- output read restricted to job-owned outputs by default. [Done]
- MCP roots integration where feasible. [Done]

Done: shared filesystem policy now enforces server-configured workspace roots for REST/agent local file inputs, enforces MCP client-provided roots around local-file MCP tools, enforces output root for agent output reads and explicit output dirs/paths, defaults agent sync output under `MARKER_OUTPUT_ROOT` when configured, and blocks unregistered output reads unless a valid Marker output manifest owns the path.

Acceptance:

- `marker_read_output` cannot read `/etc/passwd` or arbitrary files. [Done]
- Local conversion outside roots is denied when policy enabled. [Done]

### UCM-007: CLI v1 command expansion

Priority: P1
Status: [Done]

Expected files:

```text
backend/app/cli.py
backend/app/cli/  (optional package split)
backend/tests/test_cli_subprocess.py
backend/tests/test_cli_json_contract.py
```

Implement:

- `doctor` [Done]
- `schema export` [Done]
- grouped `jobs` commands [Done]
- grouped `output` commands [Done]
- `batch` [Done]
- `config` [Done]
- `server` client mode skeleton [Done]
- `mcp init-config` [Done]
- `--quiet`, `--verbose`, `--debug`, `--no-input`, `--yes`, `--dry-run` [Done]
- `--version` [Done]
- stable JSON errors [Done]

Done: existing flat commands remain compatible while grouped jobs/output/config/schema/server/mcp commands are available; batch converts sequentially, records per-item typed errors, honors `--resume` for explicit output paths, and returns exit 10 on partial failure. Parse-time and runtime JSON errors use stable Marker error payloads.

Final audit note (2026-06-26): added top-level `--version` backed by
`MARKER_VERSION` and `MARKER_COMMIT_SHA`, with subprocess coverage and CLI docs.

Acceptance:

- Existing commands still pass. [Done]
- New commands have help text and JSON tests. [Done]
- Batch mode handles partial failure with exit 10. [Done]

### UCM-008: MCP v1 split tools, resources, and prompts

Priority: P1
Status: [Done]

Expected files:

```text
backend/app/mcp_server.py
backend/app/mcp_resources.py
backend/app/mcp_prompts.py
backend/tests/test_mcp_server.py
backend/tests/test_mcp_annotations.py
```

Implement:

- Split local and URL tools. [Done]
- Add resources listed in section 11.3. [Done]
- Add prompts listed in section 11.4. [Done]
- Correct annotations. [Done]
- Use shared contract schemas. [Done]
- Return manifest/resource links. [Done]

Done: MCP v1 now exposes split local/URL plan, convert, and submit tools; URL-capable tools carry `openWorldHint=true`, destructive job/settings tools carry `destructiveHint=true`, static and templated resources cover capabilities/health/version/jobs/manifests/output/assets/docs/options/settings, and all planned prompts are registered. MCP self-test validates tools, resources, and prompts.

Acceptance:

- MCP self-test validates expected tools/resources/prompts. [Done]
- URL tools have `openWorldHint=true`. [Done]
- Delete tools have `destructiveHint=true`. [Done]

### UCM-009: MCP/REST authorization and scopes

Priority: P1/P2
Status: [Done]

Expected files:

```text
backend/app/security/auth.py
backend/app/security/scopes.py
backend/app/mcp_server.py
backend/app/main.py
backend/tests/test_auth_scopes.py
```

Implement:

- Static token scopes. [Done]
- Optional OIDC/JWT validation skeleton. [Done]
- REST middleware. [Done]
- MCP scope checks. [Done]
- Settings/delete tools gated. [Done]

Done: REST auth is disabled unless static REST tokens are configured, then `/api/*` requires Bearer auth except `/api/health`; REST settings writes/deletes require `settings:write`. MCP Streamable HTTP keeps non-loopback auth refusal and now returns scoped static tokens; MCP output reads require `outputs:read`, settings reads require `settings:read`, settings writes/deletes require `settings:write`, and job delete/cancel requires `jobs:write`. OIDC/JWT settings are explicit but safely return not-implemented instead of accepting unverified JWTs.

Acceptance:

- Non-loopback MCP HTTP without auth is denied. [Done]
- Token without `settings:write` cannot set settings. [Done]
- Token with `outputs:read` can read output. [Done]

### UCM-010: Audit events

Priority: P1/P2
Status: [Done]

Expected files:

```text
backend/app/models/audit.py
backend/app/services/audit.py
backend/alembic/versions/...
backend/tests/test_audit.py
```

Implement:

- Audit event model. [Done]
- Audit sink function. [Done]
- Events for jobs, settings, URL fetch, cloud VLM, policy denied, auth denied. [Done]
- Redaction. [Done]

Done:

- Added `audit_events` table/model plus Alembic migration. [Done]
- Added redacting audit sink that strips sensitive keys and URL credentials/query/fragment before persistence. [Done]
- REST settings writes/deletes and CLI/MCP agent settings writes/deletes emit metadata-only audit records. [Done]
- REST and agent job submit paths emit job audit records; cloud VLM opt-in emits requested audit record. [Done]
- REST URL fetch start/success/block/fail events persist, including denied events before error rollback. [Done]
- Auth denied and path policy denied events are audited best-effort. [Done]
- Tests cover secret-free settings audit, source URL blocked audit, and job-submitted audit. [Done]

Acceptance:

- Settings write emits audit event without secret value. [Done]
- URL blocked emits audit event. [Done]

### UCM-011: Observability

Priority: P2
Status: [Done]

Expected files:

```text
backend/app/services/telemetry.py
backend/app/routes/diagnostics.py
backend/app/main.py
backend/tests/test_health_ready_metrics.py
```

Implement:

- `/api/healthz` [Done]
- `/api/readyz` [Done]
- `/api/version` [Done]
- optional `/api/metrics` [Done]
- structured logging helpers [Done]
- request_id/job_id propagation [Done]

Done:

- Added diagnostics router with liveness, readiness, version, and metrics endpoints. [Done]
- Added readiness checks for database connectivity and output directory availability/writability. [Done]
- Added metrics endpoint gated by `MARKER_ENABLE_METRICS`, with job counters by status. [Done]
- Added request context middleware that propagates `X-Request-ID`, records request duration/status, and includes job_id when present. [Done]
- Health/readiness/version probes remain accessible when REST auth is configured. [Done]
- Tests cover health/version/request ID, DB readiness failure, output-dir readiness failure, metrics enabled counters, and metrics disabled default. [Done]

Acceptance:

- Readiness fails if DB/output dir unavailable. [Done]
- Metrics endpoint exposes job counters when enabled. [Done]

### UCM-012: Durable queue abstraction

Priority: P2
Status: [Done]

Expected files:

```text
backend/app/services/queue_backends.py
backend/app/services/task_manager.py
backend/app/models/job_event.py
backend/tests/test_durable_queue.py
```

Implement:

- Queue backend interface. [Done]
- Keep current in-memory backend. [Done]
- Add SQLite-durable mode first. [Done]
- Job event log. [Done]
- Retry/idempotency fields. [Done]
- Done so far: in-memory thread execution now has separate one-wide marker and parallel CPU pools, queued marker jobs expose honest waiting status, smooth-progress helper tasks are tracked/cancelled, cancellation cleanup removes futures from the owning backend, and durable SQLite recovery/event-log/retry/idempotency/lease primitives are implemented. [Done]

Done:

- Added `job_events` append-only model and migration. [Done]
- Added durable queue metadata fields on `conversion_jobs`: backend, queued/started timestamps, lease owner/expiry, retry count, max retries, and idempotency key. [Done]
- Added queue backend interface, in-memory null backend, and SQLite durable backend. [Done]
- SQLite backend can enqueue jobs in the caller transaction, recover pending jobs, recover expired processing leases, mark started with leases, and mark terminal states. [Done]
- TaskManager accepts an optional durable queue backend, exposes same-session durable enqueue, and exposes durable recovery for an external resubmitter. [Done]
- REST and agent submit paths enqueue durable metadata when the real TaskManager has a durable backend configured. [Done]
- `MARKER_QUEUE_BACKEND=sqlite` enables the SQLite durable backend for app startup. [Done]
- Tests cover restart-style queued recovery, expired lease recovery, event log sequence, same-transaction TaskManager enqueue, env selection, and unchanged in-memory behavior through existing task/upload/CLI suites. [Done]

Acceptance:

- In-memory behavior unchanged by default. [Done]
- SQLite-durable mode can recover queued job state after restart simulation. [Done]

### UCM-013: Full-page mixed PDF routing safety

Priority: P1
Status: [Done]

Expected files:

```text
backend/app/conversion/probe.py
backend/app/services/conversion_service.py
backend/tests/test_mixed_pdf_routing.py
```

Implement:

- Full page probe option for mixed execution. [Done]
- Sampled plan clearly labelled if not full. [Done]
- Execution requires full page coverage. [Done]

Done:

- Added `probe_pdf(..., full_page_probe=True)` to inspect every page instead of sampled first/last pages. [Done]
- Added probe coverage helpers to detect full-page coverage and missing probed pages. [Done]
- Mixed PDF planning now requires probe page results to cover every page exactly once before returning `mixed_pdf`. [Done]
- Sampled mixed probes are labelled in plan warnings/reasons and fall back to whole-file routing instead of mixed execution. [Done]
- Mixed PDF execution validates full page coverage and segment coverage before converting segments. [Done]
- REST plan/upload and agent plan/convert/submit paths can request full-page probing; explicit mixed routing forces full-page probing. [Done]
- Dedicated tests verify sampled mixed probes do not execute mixed routing, full-page mixed outputs/metadata cover all pages, and route planning passes `full_page_probe=True` for explicit mixed routing. [Done]

Acceptance:

- Mixed routing never drops pages. [Done]
- Tests verify all pages are present in output. [Done]

### UCM-014: Evaluation harness

Priority: P2
Status: [Done]

Expected files:

```text
backend/app/eval/
backend/scripts/run_eval.py
backend/tests/test_eval_smoke.py
docs/development/evaluation.md
```

Implement:

- Corpus manifest format. [Done]
- Metrics interface. [Done]
- Golden output comparison. [Done]
- Router benchmark report. [Done]
- CLI `marker eval run`. [Done]

Done:

- Added `backend/app/eval/` package with manifest schema `marker.eval_manifest.v1`. [Done]
- Eval samples support inline text/table data or manifest-relative golden/candidate files. [Done]
- Runner reuses existing benchmark scoring metrics, writes JSON schema `marker.eval_report.v1`, and writes Markdown summary. [Done]
- Report includes sample scores, summary pass/fail, regressions, and router expected-vs-actual engine checks. [Done]
- Added `backend/scripts/run_eval.py` for direct script usage. [Done]
- Added CLI `marker eval run --manifest ... --output-dir ...` with JSON output support. [Done]
- Added smoke tests for manifest loading, report generation, and CLI eval execution without loading heavy models. [Done]
- Added docs at `docs/development/evaluation.md`. [Done]

Acceptance:

- Smoke eval runs without heavy models on deterministic fixtures. [Done]
- Report JSON/MD generated. [Done]

### UCM-015: Documentation and release polish

Priority: P1/P2
Status: [Done]

Progress note (2026-06-26):

- Added focused CLI, MCP, enterprise security, enterprise deployment, JSON schema,
  error-code, and output-manifest docs.
- Converted `docs/usage/cli-and-mcp.md` into a concise quickstart that links to
  the deeper references.
- Updated README documentation index and changelog release notes.
- Added automated docs link/index validation under `backend/tests/test_docs_links.py`.
- Verification: `python -m pytest tests/test_docs_links.py -q` passed;
  focused CLI/MCP/eval/docs suite passed; `python -m app.cli schema export
  --json` smoke passed; `python -m app.cli self-test --json` smoke passed;
  `git diff --check` passed for touched docs/tests/plan files.

Expected files:

```text
docs/usage/cli.md
docs/usage/mcp.md
docs/enterprise/security.md
docs/enterprise/deployment.md
docs/reference/json-schemas.md
docs/reference/errors.md
docs/reference/output-manifest.md
CHANGELOG.md
```

Implement:

- CLI guide.
- MCP guide with client configs.
- Security policy docs.
- Output manifest docs.
- Error code docs.
- Upgrade notes.

Acceptance:

- README docs index links to all new docs.
- Existing `docs/usage/cli-and-mcp.md` either redirects or remains as concise quickstart.

---

## 16. Suggested autonomous agent execution order

Use this order to reduce risk:

```text
1. Add tests for currently identified defects.
2. Fix UCM-004 small defects.
3. Add error taxonomy (UCM-002).
4. Add output writer and manifest (UCM-003).
5. Add contract module and schema export (UCM-001).
6. Refactor CLI/MCP to use contract where practical.
7. Harden URL fetching and workspace reads (UCM-005, UCM-006).
8. Expand CLI v1 (UCM-007).
9. Expand MCP tools/resources/prompts (UCM-008).
10. Add auth/scopes/audit/observability (UCM-009 to UCM-011).
11. Add durable queue/eval harness (UCM-012 to UCM-014).
12. Complete docs/release polish (UCM-015).
```

Do not start with the durable queue or auth system before the contract/output/errors are stable. Otherwise too many surfaces will change at once.

---

## 17. Acceptance checklist for final pass

The final implementation should be considered complete only when these are true:

### 17.1 Backward compatibility

- Current README quickstart still works.
- Current `docs/usage/cli-and-mcp.md` examples still work or have compatible aliases.
- Existing tests continue to pass.

### 17.2 CLI readiness

- Every command has `--help`.
- Every command that supports `--json` returns schema-stable JSON.
- Errors are structured in `--json` mode.
- Batch mode supports resume and partial failure reporting.
- CLI supports noninteractive CI mode.
- CLI never leaks secrets in logs or JSON.

### 17.3 MCP readiness

- MCP self-test validates tools, resources, prompts, and schemas.
- Tool annotations match real side effects.
- URL tools are open-world.
- Destructive tools are marked destructive and policy-gated.
- Resources expose outputs/manifests safely.
- Prompts guide agents toward safe workflows.
- Remote HTTP requires auth outside loopback.

### 17.4 Enterprise readiness

- Workspace roots can restrict local path access.
- URL fetches pass SSRF test suite.
- Cloud VLM is opt-in and audited.
- Output manifests are always produced.
- Job events/audit logs exist.
- Health/readiness/version endpoints exist.
- Deployment docs cover local, Docker, GPU, and reverse proxy auth.

### 17.5 Conversion quality readiness

- Router behavior is tested on fixtures.
- Mixed PDF routing cannot drop pages.
- LiteParse fallback paths are tested.
- Deterministic converters produce manifests with assets.
- Evaluation harness exists, even if small at first.

---

## 18. Concrete code-level notes

### 18.1 `backend/app/cli.py`

Current role:

- Argparse-based entry point for all headless operations.
- Dispatches directly to `agent_api` functions.

Recommended changes:

- Keep `main()` but split into command modules if file grows.
- Add structured error handling around command dispatch.
- Add command aliases and grouped subcommands.
- Add JSON error handling.
- Add `--version`, `doctor`, `schema export`, `batch`, `output`, `config`, `server`.
- Use shared contract metadata for flag generation where practical.

### 18.2 `backend/app/agent_api.py`

Current role:

- Stable seam for CLI and MCP.
- Reuses conversion service and GUI option builder.

Recommended changes:

- Keep as orchestration facade.
- Move schemas/options/errors to `agent_contract.py` and `errors.py`.
- Replace ad-hoc dictionaries with typed models at boundaries.
- Use shared output writer.
- Policy-check local path, output path, URL, cloud VLM, settings actions.

### 18.3 `backend/app/mcp_server.py`

Current role:

- FastMCP server with tools and output models.

Recommended changes:

- Thin wrapper over contract/agent_api.
- Split local and URL tools.
- Add resources/prompts.
- Correct `DeleteJobOutput` mismatch.
- Correct annotations.
- Add scopes/policy gates.
- Add structured error mapping.

### 18.4 `backend/app/routes/convert.py`

Current role:

- REST upload/source URL/local path/job/status/download/history/delete/SSE.

Recommended changes:

- Use shared config builder from agent contract or agent_api.
- Use shared safe URL fetcher.
- Use shared output writer.
- Use env-configured upload limit.
- Add policy checks.
- Reduce drift between REST, CLI, MCP options.

### 18.5 `backend/app/services/task_manager.py`

Current role:

- Background execution, progress, SSE, finalization.

Recommended changes:

- Use shared output writer.
- Persist all metadata including `mixed_engine_segments`.
- Add durable queue abstraction behind current behavior.
- Add job event/audit hooks.
- Add cancellation semantics for queued/running jobs.
- Make finalization idempotent and atomic.

### 18.6 `backend/app/services/conversion_service.py`

Current role:

- Universal conversion orchestrator.

Recommended changes:

- Keep router/registry split.
- Make mixed PDF routing safe with full page coverage.
- Emit richer quality/fallback metadata.
- Preserve assets.
- Add plugin discovery later.

### 18.7 `backend/app/conversion/router.py`

Current role:

- Stateless extension/PDF router.

Recommended changes:

- Move thresholds to config/policy while keeping safe defaults.
- Add route policy profiles.
- Add router telemetry and eval hooks.
- Expose explanations in manifest.

### 18.8 `backend/app/services/marker_service.py`

Current role:

- Marker model loading, option building, LLM provider mapping, OOM retry.

Recommended changes:

- Remove process-global provider mutations where possible.
- Validate provider/model configurations before conversion.
- Redact logs.
- Add provider capability metadata.
- Add per-provider concurrency/cost telemetry.

### 18.9 `backend/app/processors/image_understanding.py`

Current role:

- Image routing, dedup, OCR/VLM handling, batch extraction, metadata.

Recommended changes:

- Add quality/cost summary into output manifest.
- Add policy hooks for cloud use.
- Add eval fixtures for image types.
- Add failure-mode metadata for skipped/failed VLM images.

---

## 19. Example final CLI JSON outputs

### 19.1 `marker plan --json`

```json
{
  "ok": true,
  "schema_version": "marker.plan_result.v1",
  "filename": "document.pdf",
  "size": 123456,
  "preliminary": false,
  "plan": {
    "engine": "marker_pdf",
    "label": "Marker PDF",
    "confidence": 1.0,
    "reasons": ["scan likelihood is above LiteParse threshold"],
    "needs_marker_models": true,
    "needs_gpu": true,
    "execution_backend": "marker_worker",
    "fallback_chain": [],
    "warnings": []
  },
  "probe_result": {},
  "mixed_engine_segments": null
}
```

### 19.2 `marker convert --json`

```json
{
  "ok": true,
  "schema_version": "marker.convert_result.v1",
  "source": {
    "name": "document.pdf",
    "path": "/workspace/document.pdf",
    "source_url": null
  },
  "output": {
    "text_path": "/workspace/out/document.md",
    "manifest_path": "/workspace/out/document.marker.json",
    "asset_paths": [],
    "media_type": "text/markdown"
  },
  "text_preview": "# Document\n\n...",
  "text_chars": 50000,
  "truncated": true,
  "next_step": "marker output read /workspace/out/document.md --offset 20000 --limit 20000 --json"
}
```

### 19.3 `marker batch --json`

```json
{
  "ok": false,
  "schema_version": "marker.batch_result.v1",
  "total": 10,
  "succeeded": 9,
  "failed": 1,
  "skipped": 0,
  "results_path": "/workspace/out/batch-results.jsonl",
  "failed_path": "/workspace/out/batch-failed.jsonl",
  "exit_code": 10
}
```

---

## 20. Example MCP resource responses

### 20.1 `marker://jobs/{job_id}`

```json
{
  "schema_version": "marker.job_status.v1",
  "job_id": "...",
  "status": "completed",
  "progress": 100,
  "filename": "document.pdf",
  "output_format": "markdown",
  "result_path": "/output/document.md",
  "manifest_uri": "marker://jobs/.../manifest",
  "output_uri": "marker://jobs/.../output",
  "conversion_metadata": {
    "engine": {},
    "probe_result": {},
    "mixed_engine_segments": []
  }
}
```

### 20.2 `marker://docs/agent-guide`

Should contain a concise workflow guide:

1. Call `marker_get_capabilities`.
2. Prefer local file tools over URL tools.
3. Call plan for PDFs or unknown/large files.
4. Use submit-job for long work.
5. Poll status or watch events.
6. Read output in chunks.
7. Read manifest for provenance.
8. Keep `allow_cloud_vlm=false` unless the user explicitly allows cloud vision.
9. Do not call settings/delete tools unless asked.

---

## 21. Testing plan

### 21.1 Unit tests

Add tests for:

- contract schema export
- errors and exit codes
- output writer collision behavior
- manifest creation
- safe URL fetcher
- policy roots
- conversion router thresholds
- mixed PDF page coverage
- asset persistence
- settings masking

### 21.2 CLI tests

Use subprocess tests where practical:

```text
marker --version
marker capabilities --json
marker plan sample.tsv --json
marker convert sample.tsv --output-dir tmp --json
marker output read tmp/sample.md --json
marker batch batch.jsonl --json
marker self-test --json
```

Validate:

- exit code
- stdout JSON schema
- stderr behavior
- no stack traces without debug
- output files and manifest

### 21.3 MCP tests

Test through MCP protocol, not only Python function calls:

- list tools/resources/prompts
- validate annotations
- call self-test
- convert deterministic CSV/TSV
- read output resource/chunk
- verify URL tool annotation
- verify settings write denied by policy
- verify delete-job output schema

### 21.4 Security tests

- SSRF blocked hosts and redirects.
- workspace root enforcement.
- arbitrary `read_output` denied.
- destructive operations require policy/confirmation in configured mode.
- secret values masked in settings, logs, manifests, and errors.

### 21.5 Integration tests

- REST upload to status to download.
- CLI direct conversion and async job conversion produce same manifest shape.
- MCP conversion produces same manifest shape.
- Multi-job same filename collision-safe output.
- Stale job handling on restart simulation.

### 21.6 Heavy/manual tests

Keep separate from normal CI:

- real Marker model load
- real Surya OCR
- real VLM provider smoke tests
- multi-GPU process backend
- large files
- nested ZIPs
- audio/video conversions

---

## 22. Documentation plan

Create or update:

```text
docs/usage/cli.md
    Full CLI guide, command reference, examples, batch mode, exit codes.

docs/usage/mcp.md
    MCP tools/resources/prompts, client setup, safe agent workflow.

docs/reference/json-schemas.md
    Generated schema reference.

docs/reference/output-manifest.md
    Manifest schema and examples.

docs/reference/errors.md
    Error codes and exit codes.

docs/enterprise/security.md
    Auth, policies, roots, SSRF, cloud gates, audit, secrets.

docs/enterprise/deployment.md
    Docker, GPU, reverse proxy, TLS, tokens/OIDC, storage, backups.

docs/development/evaluation.md
    Eval corpus, metrics, how to run.
```

Update README docs index to point to the new docs.

---

## 23. Release and packaging plan

### 23.1 Python package

Current `pyproject.toml` already exposes:

```toml
[project.scripts]
marker = "app.cli:main"
```

Improve packaging:

- Add project URLs.
- Add license metadata.
- Add classifiers.
- Include schema files/docs if generated.
- Add optional extras:

```text
marker-ui[cli]
marker-ui[mcp]
marker-ui[server]
marker-ui[gpu]
marker-ui[enterprise]
marker-ui[dev]
```

### 23.2 Binary/installer options

Consider later:

- pipx install.
- uv tool install.
- Homebrew formula.
- Windows installer or winget.
- Docker images.

### 23.3 Versioning

Use semantic versioning for user-facing CLI/MCP contracts.

Suggested initial contract versions:

```text
marker.cli.v1
marker.mcp.v1
marker.output_manifest.v1
marker.error.v1
```

### 23.4 Release checklist

- Tests pass.
- Docs updated.
- Changelog updated.
- JSON schema snapshots updated intentionally.
- MCP self-test passes.
- CLI self-test passes.
- Docker smoke passes.
- Security tests pass.

---

## 24. Standards alignment notes

### 24.1 MCP 2025-06-18

Relevant standard expectations:

- MCP uses JSON-RPC 2.0 message semantics.
- Servers can expose tools, resources, and prompts.
- Tools should have input and output schemas where possible.
- Tool annotations include read-only, destructive, idempotent, and open-world hints.
- Streamable HTTP and stdio are standard transports.
- stdio logs should not pollute stdout; stdout is for MCP messages.
- HTTP MCP servers should validate origin, bind localhost for local use, and use auth when remote.
- Authorization is based on OAuth 2.1 concepts for HTTP deployments.
- Roots define filesystem boundaries that servers should respect.
- Elicitation can ask for user consent or structured non-sensitive data.

Key references:

- https://modelcontextprotocol.io/specification/2025-06-18
- https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- https://modelcontextprotocol.io/specification/2025-06-18/server/resources
- https://modelcontextprotocol.io/specification/2025-06-18/server/prompts
- https://modelcontextprotocol.io/specification/2025-06-18/client/roots
- https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation

### 24.2 MCP security

Relevant expectations:

- Least privilege for tools and tokens.
- Tool schema integrity.
- No token passthrough.
- Human confirmation for sensitive operations.
- Strong sandboxing for file access.
- Audit logs.
- SSRF protections for URL-capable tools.

References:

- https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices
- https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html

### 24.3 CLI conventions

Relevant expectations:

- Human-readable by default, machine-readable with `--json`.
- stdout for output, stderr for diagnostics.
- zero exit code on success, nonzero on failure.
- support quiet/verbose/debug.
- prompts should not be required in scripts.
- use `--yes` for dangerous confirmations.
- support stdin/stdout conventions where useful.
- config precedence should be flags, env, project, user, system.
- avoid secrets in flags.

Reference:

- https://clig.dev/

### 24.4 SSRF and secure software practices

References:

- https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- https://csrc.nist.gov/pubs/sp/800/218/final

### 24.5 Observability

Reference:

- https://opentelemetry.io/docs/languages/python/instrumentation/

---

## 25. Final recommendation

The repo is already on the right path. The next implementation wave should focus on one theme:

> Make the agent/automation boundary stable, safe, observable, and enterprise-grade.

Do not overfit the next pass to one agent client. Build the core once:

- one contract layer
- one output writer
- one manifest schema
- one error taxonomy
- one policy layer
- one safe URL fetcher
- one audit/telemetry path

Then expose it cleanly through:

- GUI
- REST
- CLI
- MCP tools
- MCP resources
- MCP prompts

If this plan is implemented, Marker UI will move from a powerful local conversion UI into a serious universal document intelligence substrate for individual users, coding agents, and enterprise automation.
