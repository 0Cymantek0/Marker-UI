I read the audit and checked the repository’s actual MCP, CLI, REST, routing, output writer, frontend queue/viewer, docs, and tests. The audit’s priorities are valid, but I would **not** send the agent one giant “fix all MCP/CLI issues” task. The work should be split into small contract-stabilization phases, because many issues are coupled: tool names, scopes, profiles, docs, self-test, and config generation all currently drift from each other. 

Below is the task breakdown I’d use.

---

# Marker-UI repair plan for AI coding agents

## Phase 0 — Do this first: establish safety rails

### Task 0.1 — Add a live contract snapshot test before refactoring

**Why this comes first:** before changing MCP/CLI surfaces, the agent needs tests that capture what currently exists and what the new target should be.

**Context:** `mcp_server.py` has its own `MCP_V1_TOOL_NAMES` list with many tools and aliases, while `agent_api.py` has a smaller `TOOL_NAMES` list. These are already divergent.

**Agent task prompt:**

```text
Create contract snapshot tests for Marker-UI’s current agent-facing surfaces before making behavior changes.

Focus files:
- backend/tests/test_cli_mcp.py
- backend/app/mcp_server.py
- backend/app/agent_api.py
- backend/app/mcp_resources.py

Add tests that record:
1. live MCP tool names from mcp.list_tools()
2. agent_api.capabilities()["tools"]
3. marker://capabilities resource tool list
4. CLI `marker mcp inspect --json` tool list
5. MCP resource/template URIs
6. prompt names

The tests should initially document current drift, but also include TODO/xfail or explicit target assertions that will be flipped in later tasks once the registry/profile work lands.

Do not refactor production code in this task except tiny testability helpers if unavoidable.
```

**Acceptance criteria:**

* Tests expose the mismatch between `MCP_V1_TOOL_NAMES` and `agent_api.TOOL_NAMES`.
* Tests can be reused later to verify the final minimal/full/admin profiles.
* No production behavior changes yet.

---

## Phase 1 — Fix hard runtime blockers

### Task 1.1 — Fix MCP manifest helper crash and centralize manifest reading

**Why:** this is the cleanest P0 and low-risk. `mcp_server.py` uses `json.loads` inside `_manifest_for_output_path`, but does not import `json`; `mcp_resources.py` has a duplicated helper that does import `json`.

**Agent task prompt:**

```text
Fix Marker-UI MCP output manifest runtime failure and remove duplicated manifest-reading logic.

Focus files:
- backend/app/mcp_server.py
- backend/app/mcp_resources.py
- new backend/app/services/output_manifest_reader.py
- backend/tests/test_cli_mcp.py

Implement a shared module `app.services.output_manifest_reader` with:
- manifest_for_output_path(path: Path) -> tuple[Path | None, dict[str, Any]]
- manifest_for_job_status(status: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]
- output_text_path_from_manifest(manifest: dict[str, Any]) -> str | None

Move duplicate helper logic from mcp_server.py and mcp_resources.py into this module. Ensure json is imported only in the shared module.

Add tests that create a real converted output + sibling `.marker.json`, then verify:
- marker_get_output_manifest works
- marker_list_output_assets works
- marker://outputs/{output_id}/manifest works
- marker://jobs/{job_id}/manifest works when job metadata points to a manifest
```

**Acceptance criteria:**

* No local `_manifest_for_output_path` duplicate remains in `mcp_server.py` or `mcp_resources.py`.
* Manifest tools do not raise `NameError`.
* Both MCP tools and MCP resources use the same manifest reader.
* Existing output manifest behavior stays compatible.

---

### Task 1.2 — Add direct `httpx` dependency and packaging smoke test

**Why:** `safe_url_fetcher.py` imports `httpx`, but `backend/requirements.txt` does not declare it directly.

**Agent task prompt:**

```text
Declare httpx as a direct backend dependency and add a packaging/import smoke test.

Focus files:
- backend/requirements.txt
- pyproject.toml if needed
- backend/tests/test_cli_mcp.py or a new packaging test file

Add:
httpx>=0.27,<1.0

Add a test or documented smoke command that installs/imports the URL fetcher cleanly:
python -c "import app.services.safe_url_fetcher; print('ok')"

Do not alter URL fetching behavior in this task.
```

**Acceptance criteria:**

* `httpx` is listed directly.
* Importing `app.services.safe_url_fetcher` succeeds in a clean backend environment.
* No unrelated dependency churn.

---

## Phase 2 — Stabilize the agent-facing contract

### Task 2.1 — Create a single agent surface registry

**Why:** this is the foundation. Right now MCP tool lists, capabilities, docs, and self-test are manually maintained in different places. MCP has 20+ tools and aliases, while `agent_api.capabilities()` reports fewer.

**Agent task prompt:**

```text
Create a single source of truth for Marker agent/MCP tools.

Focus files:
- new backend/app/agent_surface.py
- backend/app/agent_api.py
- backend/app/mcp_server.py
- backend/app/mcp_resources.py
- backend/tests/test_cli_mcp.py

Create an `AgentToolSpec` model/dataclass containing:
- name
- title
- description
- scopes
- profile: minimal | full | admin
- annotations: readOnlyHint, destructiveHint, idempotentHint, openWorldHint
- aliases/deprecated names where applicable

Move the MCP tool-name list out of mcp_server.py and the TOOL_NAMES list out of agent_api.py into this registry.

Generate:
- agent_api.capabilities()["tools"]
- MCP capabilities tool output
- marker://capabilities resource tool list
- marker mcp inspect output
- self-test expected tool list

Do not change the exposed default tool set yet unless needed for test wiring. This task is about centralizing the source of truth.
```

**Acceptance criteria:**

* There is only one canonical registry for agent/MCP tool metadata.
* `capabilities()["tools"]`, `marker://capabilities`, and live MCP list agree for the selected profile.
* Tests fail if a tool is added to MCP but not the registry.

---

### Task 2.2 — Add MCP tool profiles: minimal, full, admin

**Why:** the default MCP surface is too large for coding agents. The current server exposes many overlapping generic/local/url tools and settings write/delete tools in the same default surface.  The docs also lead with the full list of tools.

**Agent task prompt:**

```text
Add MCP tool profiles and make the default profile minimal.

Focus files:
- backend/app/agent_surface.py
- backend/app/mcp_server.py
- backend/app/cli.py
- backend/tests/test_cli_mcp.py
- docs/usage/mcp.md

Add:
- marker mcp start --tool-profile minimal|full|admin
- marker mcp inspect --tool-profile minimal|full|admin --json
- MARKER_MCP_TOOL_PROFILE env fallback

Default to `minimal`.

Minimal profile should expose only:
- marker_capabilities
- marker_plan
- marker_convert
- marker_submit
- marker_job_status
- marker_cancel_job
- marker_read_output
- marker_output_manifest

Full profile may include convenience/source-specific tools and deprecated aliases.

Admin profile includes:
- settings read/write/delete tools
- marker_delete_job
- other destructive/admin tools

Keep backward compatibility by allowing full/admin profile, but do not expose admin tools by default.
```

**Acceptance criteria:**

* Minimal profile returns ≤10 tools.
* Full profile still exposes legacy tools.
* Admin profile is required for settings write/delete and job deletion.
* Docs lead with minimal profile, not the legacy full list.
* Existing tests that expect legacy tools are updated to request `--tool-profile full`.

---

### Task 2.3 — Replace duplicate MCP local/url/generic tools with unified input models

**Why:** the agent currently has to choose among generic, local-file, and URL versions of plan/convert/submit. That creates tool-selection noise. The current MCP `marker_convert_file` accepts both `local_file_path` and `source_url`; separate `marker_convert_local_file` and `marker_convert_url` also exist.

**Agent task prompt:**

```text
Introduce v2 unified MCP tools for plan/convert/submit using discriminated source objects.

Focus files:
- backend/app/agent_contract.py
- backend/app/agent_surface.py
- backend/app/mcp_server.py
- backend/app/agent_api.py
- backend/tests/test_cli_mcp.py
- docs/usage/mcp.md

Add source models:
- { "kind": "local_path", "path": "/absolute/path/file.pdf" }
- { "kind": "url", "url": "https://example.com/file.pdf" }

Add v2 tools:
- marker_capabilities
- marker_plan
- marker_convert
- marker_submit
- marker_job_status
- marker_cancel_job
- marker_read_output
- marker_output_manifest

These v2 tools should call existing agent_api internals. Keep old tools as aliases in full/admin compatibility profiles only.
```

**Acceptance criteria:**

* Minimal profile exposes only v2 tools.
* Existing source-specific tools are still available in full/admin profile.
* v2 schemas are cleaner than the old huge function signatures.
* v2 docs include one canonical JSON request example.

---

## Phase 3 — Enforce MCP security properly

### Task 3.1 — Enforce scopes on every MCP tool and resource

**Why:** some MCP tools call `require_mcp_scopes`, but many read tools and all resources are not consistently guarded. `mcp_resources.py` exposes jobs, manifests, output, assets, docs/options, and settings without scope checks in the handlers.  The auth helper also returns early when no MCP access token exists, which is okay for local stdio but dangerous if resources are not wrapped in HTTP mode.

**Agent task prompt:**

```text
Apply uniform MCP scope enforcement to every MCP tool and resource.

Focus files:
- backend/app/agent_surface.py
- backend/app/mcp_server.py
- backend/app/mcp_resources.py
- backend/app/security/auth.py
- backend/app/security/scopes.py
- backend/tests/test_cli_mcp.py

Implement one wrapper/decorator path that enforces scopes from AgentToolSpec/ResourceSpec before handler execution.

Required mappings:
- capabilities/tools/docs/options -> capabilities:read
- jobs and job status/resources -> jobs:read
- submit/cancel/delete -> jobs:write where mutating
- output reads, manifests, assets -> outputs:read
- settings read -> settings:read
- settings write/delete -> settings:write

Add equivalent resource metadata/specs and enforce scopes inside resource handlers.

Add tests that simulate tokens with missing scopes and assert failure for:
- marker://settings without settings:read
- marker://jobs without jobs:read
- output manifest without outputs:read
- cancel/delete without jobs:write
```

**Acceptance criteria:**

* Every MCP tool/resource has an explicit scope set.
* No resource bypasses scope enforcement.
* Stdio/local no-token behavior remains usable.
* Non-loopback HTTP remains refused without auth token.

---

### Task 3.2 — Gate admin/settings write tools behind explicit environment opt-in

**Why:** even with scopes, model-controlled settings writes are dangerous. The current MCP default includes settings write/delete tools.

**Agent task prompt:**

```text
Disable MCP settings write/delete by default, even in admin profile, unless explicitly enabled.

Focus files:
- backend/app/agent_surface.py
- backend/app/mcp_server.py
- docs/usage/mcp.md
- backend/tests/test_cli_mcp.py

Add env:
MARKER_MCP_ENABLE_SETTINGS_WRITE=true

Behavior:
- Without env, marker_set_setting and marker_delete_setting are not registered.
- With env and admin profile, they are registered.
- settings read tools may stay in admin or full according to profile design, but not minimal.

Update docs to clearly say settings writes are model-controlled and must be enabled intentionally.
```

**Acceptance criteria:**

* Minimal profile never exposes settings write/delete.
* Admin profile exposes settings write/delete only with env opt-in.
* Self-test reports the active profile and whether settings writes are disabled.

---

## Phase 4 — Split cancel/delete everywhere

### Task 4.1 — Add real cancel API and agent function

**Status 2026-07-09:** Completed in the current repo. `POST /api/convert/{job_id}/cancel`
exists, agent/MCP/CLI cancel paths preserve the job row, and destructive delete
remains separate.

**Why:** `TaskManager.cancel_job()` already preserves the job row and marks status `cancelled`.  But REST `DELETE /api/convert/{job_id}` cancels and then deletes metadata/files.  MCP and CLI cancel currently call `delete_job(..., delete_files=False)`, which still deletes the DB row.

**Agent task prompt:**

```text
Separate job cancellation from job deletion across backend agent surfaces.

Focus files:
- backend/app/routes/convert.py
- backend/app/agent_api.py
- backend/app/mcp_server.py
- backend/app/cli.py
- backend/tests/test_cli_mcp.py
- add/adjust REST tests if present

Implement:
- POST /api/convert/{job_id}/cancel
- agent_api.cancel_job(job_id)
- MCP marker_cancel_job calls agent_api.cancel_job
- CLI jobs cancel calls agent_api.cancel_job
- DELETE /api/convert/{job_id} remains destructive deletion only

Expected semantics:
- cancel: stop work if possible, keep DB row, status becomes cancelled
- delete: remove DB row, optionally remove files
- delete is never used as the implementation of cancel
```

**Acceptance criteria:**

* Cancelled jobs remain in history.
* `get_job_status(job_id)` after cancel returns status `cancelled`.
* `marker_cancel_job` result status is `cancelled`, not `deleted`.
* `marker_delete_job` remains destructive and admin/profile gated.

---

### Task 4.2 — Fix frontend cancellation state and local removal behavior

**Why:** frontend `ConversionPhase` has no `cancelled`; cancelled backend states are mapped to `failed`.  Cancel and remove both call `deleteJob`.

**Agent task prompt:**

```text
Fix frontend job lifecycle semantics for cancelled jobs.

Focus files:
- frontend/src/lib/api.ts
- frontend/src/hooks/useConversionQueue.tsx
- frontend/src/pages/ConvertPage.tsx
- frontend tests if available

Implement:
- add `cancelJob(jobId)` calling POST /api/convert/{jobId}/cancel
- keep `deleteJob(jobId)` for destructive deletion only
- add `cancelled` to ConversionPhase
- map backend cancelled status to phase "cancelled", not "failed"
- cancel button calls cancelJob
- remove from UI should only remove locally unless there is an explicit delete action/confirmation
- clear polling intervals/SSE handles on remove/unmount
```

**Acceptance criteria:**

* Cancelled jobs render differently from failed jobs.
* Remove from UI does not silently delete backend metadata.
* Running job cancellation still stops backend work.
* No SSE/polling loop survives after job removal.

---

## Phase 5 — Create one format and option registry

### Task 5.1 — Create a single supported-format registry

**Why:** `.gif` is routed as image input and URL MIME maps to `.gif`, but REST upload allowlist does not include `.gif`.

**Agent task prompt:**

```text
Create one backend format registry and remove duplicated extension/MIME/router allowlists.

Focus files:
- new backend/app/conversion/formats.py
- backend/app/conversion/router.py
- backend/app/routes/convert.py
- backend/app/services/safe_url_fetcher.py
- backend/app/agent_api.py
- backend/app/routes/capabilities.py
- frontend/src/lib/api.ts
- frontend/src/pages/ConvertPage.tsx
- docs

Define each format once with:
- extensions
- MIME types
- engine
- upload_allowed
- url_allowed
- label
- category
- needs_marker_models
- needs_gpu

Generate from it:
- REST ALLOWED_EXTENSIONS
- safe_url_fetcher content-type mapping
- router extension map
- agent capabilities allowed_extensions/converters
- frontend capabilities data
- docs supported-formats table

Fix `.gif` consistency explicitly.
```

**Acceptance criteria:**

* No independent extension allowlist remains in REST/router/URL downloader.
* `.gif` support is either consistently enabled everywhere or deliberately disabled everywhere.
* Tests assert registry/router/REST/URL/frontend parity.

---

### Task 5.2 — Make conversion options schema-driven

**Why:** options are repeated in REST query params, agent contract, MCP signatures, CLI parser, and frontend TS. The repo already has `ConversionOptionsModel` and partial `OPTION_METADATA`, but many productivity options only exist in CLI/MCP/frontend/REST plumbing rather than the core model.

**Agent task prompt:**

```text
Make ConversionOptionsModel/metadata the source of truth for CLI/MCP/REST/frontend option definitions.

Focus files:
- backend/app/agent_contract.py
- backend/app/agent_api.py
- backend/app/cli.py
- backend/app/mcp_server.py
- backend/app/routes/convert.py
- frontend/src/lib/api.ts
- frontend/src/components/features/ConversionOptions.tsx
- docs/reference/json-schemas.md
- tests

Expand ConversionOptionsModel or structured extra option models to cover currently duplicated knobs:
- text_data_max_rows
- archive_* options
- image router options
- OCR routing options
- VLM batching options

Generate or validate:
- CLI flags
- MCP schemas
- REST request mapping
- frontend TypeScript type
- docs option table

Do not break existing --option/--options-json escape hatch.
```

**Acceptance criteria:**

* A parity test fails if a documented option exists in one surface but not another.
* CLI/MCP/REST/frontend all accept the same named options.
* Docs option table is generated or snapshot-tested.

---

### Task 5.3 — Make explicit engine overrides strict for agents/CLI

**Why:** invalid engine overrides are currently ignored by routing fallback: `_plan_engine_override` returns `None` if the engine is unknown or incompatible.  That is okay for forgiving GUI auto-mode, but bad for scripts and agents.

**Agent task prompt:**

```text
Make explicit engine_override strict for CLI/MCP/agent surfaces.

Focus files:
- backend/app/conversion/router.py
- backend/app/agent_api.py
- backend/app/routes/convert.py
- backend/tests
- docs/usage/cli.md
- docs/usage/mcp.md

Behavior:
- If engine_override is provided and unknown, return a typed USAGE_ERROR.
- If engine_override is known but incompatible with the extension, return a typed USAGE_ERROR listing compatible extensions.
- GUI can optionally keep forgiving behavior only if config explicitly says strict_engine_override=false.
- CLI/MCP/agent_api must default strict.

Add tests for:
- unknown engine
- incompatible engine
- compatible engine
- auto/no override still works
```

**Acceptance criteria:**

* Agents never silently get a different engine than requested.
* Error response includes requested engine, extension, and compatible extensions.
* GUI behavior is deliberate and documented.

---

## Phase 6 — Output reliability and frontend privacy

### Task 6.1 — Deduplicate asset filenames in output writer

**Why:** main output files avoid collisions, but asset writes can overwrite if two image/asset names sanitize to the same target. `_write_images` and `_write_assets` write directly to `asset_dir / name` or `asset_dir / relative`.

**Agent task prompt:**

```text
Prevent output sidecar asset filename collisions.

Focus files:
- backend/app/services/output_writer.py
- backend/tests/test_cli_mcp.py or new output writer tests

Implement:
- _next_available_asset_path(asset_dir, relative_path, used_paths, overwrite=False)
- Apply to both _write_images and _write_assets
- Keep manifest entry name, relative_path, path, sha256, and bytes aligned with the actual deduplicated file path

Add tests:
1. two image dict entries sanitize to same filename
2. two asset entries use the same nested relative path
3. image and asset collide with each other
4. manifest lists distinct paths and both files exist
```

**Acceptance criteria:**

* No asset sidecar is silently overwritten.
* Manifest accurately reflects deduplicated file names.
* Existing output text collision behavior remains unchanged.

---

### Task 6.2 — Sanitize Markdown images in frontend output viewer

**Status 2026-07-09:** Completed in the current repo. The Markdown preview
blocks unsafe image sources and keeps safe local asset rendering covered by
frontend tests.

**Why:** converted Markdown is untrusted content. `OutputViewer` passes image `src` directly into `<img src={src}>`, which can trigger external browser requests.

**Agent task prompt:**

```text
Make frontend Markdown rendering privacy-aware.

Focus files:
- frontend/src/components/features/OutputViewer.tsx
- frontend tests

Implement a sanitizer for Markdown image/link URLs:
- block http:// and https:// images by default
- block data: images by default
- block file: URLs
- allow safe relative paths only when they correspond to known manifest assets or safe local asset references
- render blocked images as a placeholder with visible URL text and optional "load external image" action

Preserve ImageUnderstandingBadge behavior for safe local assets.
```

**Acceptance criteria:**

* External Markdown images do not auto-load.
* Relative manifest assets still render.
* Tests cover `http`, `https`, `data`, `file`, absolute path, and relative image src.

---

## Phase 7 — URL downloader hardening

### Task 7.1 — Harden URL fetch against DNS rebinding and redirect policy gaps

**Why:** URL safety is strong but performs DNS resolution before `httpx` performs its own connection resolution, leaving a rebinding window. The current code checks URL safety before each request/redirect and blocks private/local IPs during `socket.getaddrinfo`.

**Agent task prompt:**

```text
Harden safe_url_fetcher against DNS rebinding and risky redirects.

Focus files:
- backend/app/services/safe_url_fetcher.py
- backend/tests

Implement one of:
A. production allowlist-first mode that can require MARKER_SOURCE_URL_ALLOWLIST for URL conversion, or
B. resolved-IP pinning / post-connect peer IP verification if feasible with httpx transport.

Also add redirect policy options:
- default: reject redirects to a different hostname unless explicitly allowed
- always re-check redirected target
- audit final URL and resolved IPs

Add tests for:
- private IP direct URL
- redirect to private IP
- redirect to different public host
- allowlisted host pass
- DNS rebinding simulation: public resolution during precheck, private during actual connection
- max size enforcement still works
```

**Acceptance criteria:**

* DNS rebinding test is blocked.
* Redirect-to-private test is blocked.
* Docs clearly explain production URL allowlist guidance.

---

## Phase 8 — CLI and MCP client compatibility

### Task 8.1 — Fix MCP client config generator

**Why:** docs show `cwd` for source checkout configs, but `_mcp_client_config()` emits only `command`, `args`, and env. It supports only codex/claude/gemini/opencode/antigravity.

**Agent task prompt:**

```text
Fix and expand `marker mcp init-config`.

Focus files:
- backend/app/cli.py
- docs/usage/mcp.md
- backend/tests/test_cli_mcp.py

Add flags:
- --client codex|claude|gemini|opencode|cursor|zed|cline|continue|goose|windsurf|antigravity
- --mode source|installed|http
- --cwd PATH
- --server-name marker
- --tool-profile minimal|full|admin
- --output PATH

Behavior:
- installed mode uses: marker mcp start --tool-profile minimal
- source mode uses python -m app.cli mcp start and requires/emits cwd
- http mode emits URL/auth fields appropriate to the client where supported
- generated config should parse as JSON or TOML depending on client

Add tests:
- every generated JSON parses
- Codex TOML parses
- source mode includes cwd
- installed mode uses marker command
- default profile is minimal
```

**Acceptance criteria:**

* Generated configs match docs.
* Cursor and Zed/ZCode are included.
* Snapshot tests prevent config drift.

---

### Task 8.2 — Fix CLI docs/parser mismatches and global flag semantics

**Why:** CLI docs show `settings get/delete --category`, but parser only accepts `--category` for list/set.   Global flags like `--quiet`, `--verbose`, `--no-input`, `--yes`, and `--dry-run` are parsed but not consistently enforced.

**Agent task prompt:**

```text
Make CLI docs and parser behavior match, and define global flag semantics.

Focus files:
- backend/app/cli.py
- docs/usage/cli.md
- backend/tests/test_cli_mcp.py

Fix settings:
- either add --category to settings get/delete and config get/delete, or remove it from docs.
Prefer adding --category consistently if duplicate keys by category are possible.

Define/enforce:
- --json: success JSON only on stdout, typed error JSON only on stderr
- --quiet: suppress non-error diagnostics
- --verbose: diagnostics to stderr only
- --debug: stack traces to stderr only
- --no-input: never prompt; fail if confirmation needed
- --yes: allow destructive operations without prompt
- --dry-run: supported write commands validate and report intended action

Add docs parity tests that run all documented safe commands with --help, --dry-run, or fixtures.
```

**Acceptance criteria:**

* Documented commands do not fail because of missing parser flags.
* Destructive commands respect `--yes`/`--no-input`.
* JSON mode never mixes logs/progress into stdout.

---

### Task 8.3 — Add `convert --request-json`, `--stdin-json`, and `--overwrite`

**Why:** batch has `--request-json`, but single conversion does not. Output writer supports `overwrite`, but CLI/MCP do not expose it cleanly.

**Agent task prompt:**

```text
Improve CLI scriptability for single conversions.

Focus files:
- backend/app/agent_contract.py
- backend/app/agent_api.py
- backend/app/cli.py
- backend/app/mcp_server.py if output overwrite should be exposed there too
- backend/tests/test_cli_mcp.py
- docs/usage/cli.md

Add:
- marker convert --request-json request.json --json
- marker convert --stdin-json --json
- marker convert --overwrite when output_path or default output exists

Wire overwrite into write_conversion_output through agent_api.

Request JSON should align with ConvertRequestModel.
```

**Acceptance criteria:**

* Agents can pass one JSON file/stdin object for conversion.
* `--overwrite` works only when explicitly supplied.
* Existing collision tests still pass.

---

## Phase 9 — Durable queue and job execution correctness

### Task 9.1 — Clarify durable queue ownership to avoid double scheduling

**Why:** REST and agent API enqueue durable metadata and then immediately call `task_manager.submit_job`.   That may be intended as recovery metadata, but the policy is not explicit.

**Agent task prompt:**

```text
Make durable queue semantics explicit and tested.

Focus files:
- backend/app/services/task_manager.py
- backend/app/agent_api.py
- backend/app/routes/convert.py
- backend/app/services/queue_backends.py
- tests

Choose and implement one clear policy:

Option A: in-memory primary + durable recovery metadata
- enqueue durable record as recovery metadata
- mark/claim it when submit_job starts
- recovery only resubmits stale/nonterminal jobs after heartbeat/expiry

Option B: durable queue primary
- REST/agent enqueue only
- a worker/resubmitter consumes queue
- submit_job is called by the worker only

Add tests:
- a configured durable queue executes job once
- restart recovery does not duplicate completed jobs
- stale processing jobs recover according to documented policy
```

**Acceptance criteria:**

* No duplicate execution under durable queue mode.
* Queue state transitions are documented.
* Recovery tests cover completed, queued, processing, failed, and cancelled jobs.

---

## Phase 10 — Documentation and release polish

### Task 10.1 — Rewrite README first screen for agent/local-first value

**Why:** README is detailed, but the top is dense. It does mention CLI/MCP, but not with a fast “agent-ready local converter” demo.

**Agent task prompt:**

```text
Rewrite the README top section to sell Marker-UI quickly and accurately.

Focus files:
- README.md
- docs/usage/mcp.md
- docs/usage/cli.md if cross-links need adjustment

New top should include:
- one-sentence value proposition
- supported input categories
- local-first privacy note
- output manifest idea
- GUI + CLI + MCP positioning
- 30-second demo:
  - marker self-test --json
  - marker convert ./paper.pdf --output-dir ./out --json
  - marker mcp start --tool-profile minimal
- short “What can agents do?” section
- screenshot/GIF placeholder if no asset exists

Keep deeper feature sections below.
```

**Acceptance criteria:**

* First screen explains why the project exists.
* Agent users immediately see CLI/MCP entry points.
* Claims match actual supported formats from the new registry.

---

### Task 10.2 — Generate MCP/CLI docs from registry or snapshot-test them

**Why:** MCP docs manually list tools/resources and already reflect the large old surface.  CLI docs already drift from parser behavior.

**Agent task prompt:**

```text
Prevent docs drift for MCP tools, resources, options, and CLI examples.

Focus files:
- backend/app/agent_surface.py
- backend/app/agent_contract.py
- docs/usage/mcp.md
- docs/usage/cli.md
- docs/reference/json-schemas.md
- tests or docs generator scripts

Implement either:
A. generated docs sections from registry/schema, or
B. snapshot tests that compare docs tables/examples to live parser/registry.

Must cover:
- minimal/full/admin MCP tools
- MCP resources
- option metadata
- CLI examples
- client config examples
```

**Acceptance criteria:**

* Tool tables cannot drift silently.
* Every documented CLI example is test-covered by help/dry-run/fixture execution.
* MCP docs show minimal profile first and admin tools later.

---

# Recommended execution order

Send tasks to the agent in this order:

Status note 2026-07-09: skip tasks already completed in the current repo:
Task 4.1, Task 4.2, Task 5.3, and Task 6.2. Re-verify Task 5.1 before
assigning because request validation and output typing are fixed, while broader
registry consolidation may still be useful.

1. **Task 0.1** — contract snapshot tests
2. **Task 1.1** — manifest crash + shared manifest reader
3. **Task 1.2** — direct `httpx` dependency
4. **Task 2.1** — single agent surface registry
5. **Task 2.2** — MCP profiles
6. **Task 2.3** — unified v2 MCP tools
7. **Task 3.1** — scope enforcement for all tools/resources
8. **Task 3.2** — settings write opt-in
9. **Task 4.1** — backend cancel/delete split
10. **Task 4.2** — frontend cancelled state
11. **Task 5.1** — format registry
12. **Task 5.2** — option schema registry
13. **Task 5.3** — strict engine overrides
14. **Task 6.1** — output asset collision fix
15. **Task 6.2** — Markdown image sanitization
16. **Task 7.1** — URL downloader hardening
17. **Task 8.1** — MCP config generator
18. **Task 8.2** — CLI docs/parser/global flags
19. **Task 8.3** — request JSON/stdin/overwrite
20. **Task 9.1** — durable queue semantics
21. **Task 10.1** — README rewrite
22. **Task 10.2** — generated/snapshot-tested docs

The highest-value stopping point is after **Task 4.2**. At that point, MCP runtime, capabilities drift, profile bloat, scopes, and cancel/delete semantics are fixed—the biggest things that would hurt coding-agent usage.
