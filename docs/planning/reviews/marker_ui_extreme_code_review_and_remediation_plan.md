# Marker UI Extreme Deep Code Review and Remediation Plan

> Status note (2026-07-09): This is a historical static-review input from
> 2026-07-03. Several P0/P1 findings have since been fixed or partially fixed
> in the local repo. Treat detailed findings as audit history and consult
> `planning/reviews/README.md` for the current status ledger before using this
> document as implementation truth.

**Repository:** `0Cymantek0/Marker-UI`
**Branch requested:** `master`
**Commit observed through GitHub connector:** `92a883afb993f34855cf295bd865fffba956627a`
**Review date:** 2026-07-03
**Reviewer stance:** senior architecture/code review, product-hardening plan, and implementation roadmap.

---

## 0. Scope, method, and confidence

This review was performed by inspecting the repository through the GitHub connector, with focused reads of conversion routing, converters, task management, REST routes, CLI/MCP surfaces, audio provider code, frontend output rendering, output writing, URL safety, policy, database bootstrap, and selected configuration/test files.

I did **not** run the full application, because repository access was available through the GitHub connector rather than a locally cloned working tree with all neural/model/runtime dependencies. Therefore:

- Findings marked **confirmed** are direct code-path defects or contract mismatches visible from source.
- Findings marked **high-confidence likely** are defects whose code path is clear, but where exact runtime symptoms should be validated with the proposed tests.
- Findings marked **needs dynamic validation** are risk areas that need real fixtures, GPU/CPU runs, browser tests, and provider mocks before final severity is locked.

The review intentionally goes beyond the issues already reported by the coding agent. The strongest pattern is not â€œone missing feature,â€ but **architecture drift**: multiple independent registries, multiple contracts, aspirational UI controls, and native converters that produce Markdown while the UI/API advertise structured multi-format behavior.

---

## 1. Executive verdict

Marker UI has a strong product direction and several genuinely useful pieces: a local-first Marker path, a conversion router, basic native deterministic converters, async jobs, a CLI/MCP surface, output manifests, privacy gates for cloud VLM/STT, and some real tests. However, the current implementation is not yet a production-grade universal document conversion/RAG system. It is closer to a promising platform with a polished UI over several half-built subsystems.

### What works best today

1. **PDF/image/EPUB through Marker**: this is the most mature path, especially when models are installed and the document is within memory limits.
2. **Simple Markdown output for native formats**: simple DOCX/PPTX/XLSX/CSV/HTML/XML/JSON/text/notebook files can produce usable Markdown.
3. **Local audio transcription**: local faster-whisper is the only shipped audio provider and can produce transcripts plus deterministic extractive notes.
4. **Headless CLI/MCP basics**: conversion, job listing, output reading, and manifest/assets inspection exist.
5. **Output manifest**: this is a good foundation for agent workflows and asset provenance.

### What is half-built or misleading today

1. **JSON/HTML/chunks are advertised for native converters but mostly not implemented.** Native converters return Markdown, yet REST/UI/download paths can label that Markdown as JSON or chunks.
2. **Chunking is not semantic/RAG chunking except where Markerâ€™s own chunk renderer is used.** `marker_read_output_chunk` is just offset paging.
3. **Audio advanced features are UI-first, not pipeline-first.** Provider comparison, contradiction detection, fusion, enhancement/correction strength, cloud provider adapters, and diarization are mostly controls and metadata rather than implemented behavior.
4. **Format support is duplicated across REST, router, frontend, MCP/CLI, and URL fetcher.** Drift already exists.
5. **Job lifecycle is inconsistent.** REST delete cancels and deletes in one operation; MCP has separate names but still uses deletion semantics underneath; running thread cancellation is not reliable.
6. **Output preview leaks remote Markdown image URLs.** The React Markdown renderer emits `<img src=...>` directly, which can reveal user IP/session context to attacker-controlled image hosts embedded in converted documents.
7. **Native converters are too custom and too shallow for a top-quality project.** DOCX/PPTX/spreadsheet parsing is hand-rolled enough to break on complex real-world files, while existing tools such as Docling, MarkItDown, Pandoc, and Unstructured can cover large parts of this space more reliably.

### Strategic recommendation

Do not try to patch every feature by adding more ad hoc conditionals. The highest-quality path is:

1. Build **one canonical capability/format/option registry**.
2. Introduce a **conversion artifact model**: text + structured elements + assets + metadata + available renderers.
3. Integrate existing tools where they are strong:
   - **Marker** remains the high-accuracy PDF/image path.
   - **Docling** becomes the primary structured IR/chunking path for native documents and optional RAG-ready output.
   - **MarkItDown** becomes a lightweight Markdown fallback for broad deterministic conversion.
   - **Pandoc** is used selectively for markup/office conversion where its AST model is appropriate, while respecting its documented lossiness.
   - **Unstructured chunking** is a fallback element-based chunker where Docling output is unavailable.
4. Replace aspirational controls with an **implementation-state matrix**: `implemented`, `beta`, `deferred`, `unsupported`.
5. Ship fewer features at once, but each feature must have fixture coverage, contract tests, browser tests where relevant, and explicit acceptance criteria.

---

## 2. Research basis used for the plan

The remediation plan favors existing high-quality tools over custom rewrites where possible.

### Document conversion and chunking

- **Docling** supports a unified document representation and exports to Markdown, HTML, JSON, text, DocLang, Doctags, and WebVTT. It supports inputs including PDF, DOCX/XLSX/PPTX, ODF, EPUB, Markdown, AsciiDoc, LaTeX, HTML/XHTML, CSV, images, audio, video, WebVTT, and schema-specific XML formats. Source: Docling supported formats, `https://docling-project.github.io/docling/usage/supported_formats/`.
- **Docling chunking** provides `BaseChunker`, `HybridChunker`, line-based token chunking, and hierarchical chunking. `HybridChunker` applies tokenizer-aware split/merge refinements on top of document hierarchy and can repeat table headers for table chunks. Source: Docling chunking docs, `https://docling-project.github.io/docling/concepts/chunking/`.
- **Unstructured chunking** chunks document elements, preserving semantic units after partitioning. Its `by_title` strategy preserves section boundaries and can respect page boundaries. Source: Unstructured chunking docs, `https://docs.unstructured.io/open-source/core-functionality/chunking`.
- **MarkItDown** is a Microsoft open-source tool focused on producing Markdown for LLM/text-analysis consumption. It supports PDF, PowerPoint, Word, Excel, images, audio, HTML, CSV/JSON/XML, ZIP, YouTube URLs, EPUB, and more, but its own README says the output is not meant for high-fidelity human conversions. Source: MarkItDown README, `https://github.com/microsoft/markitdown`.
- **Pandoc** is a mature universal document converter with a real AST and many readers/writers, but its manual explicitly warns that conversions from formats more expressive than Pandoc Markdown can be lossy and that complex tables may not fit its document model. Source: Pandoc manual, `https://pandoc.org/MANUAL.html`.
- Recent document-RAG evaluation literature is aligned with this plan: structure-aware and hierarchical chunking generally beat naive fixed-offset splitting; one 2026 evaluation found Docling with hierarchical splitting and image descriptions performed best among tested PDF-to-Markdown conversion configurations, and that metadata enrichment/hierarchy-aware chunking mattered more than converter choice alone. Source: â€œFrom PDF to RAG-Ready: Evaluating Document Conversion Frameworks for Domain-Specific Question Answering,â€ arXiv 2604.04948.

### MCP and agent surface

- The MCP specification defines resources, prompts, tools, client roots, elicitation, progress, cancellation, logging, and security principles. It explicitly calls out user consent/control, data privacy, tool safety, authorization flows, and appropriate access controls. Source: MCP specification 2025-06-18, `https://modelcontextprotocol.io/specification/2025-06-18`.

### Audio/STT providers

- OpenAIâ€™s current speech-to-text API supports `transcriptions` and `translations`, with models including `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, and `gpt-4o-transcribe-diarize`; diarized output requires `diarized_json` and `chunking_strategy` for audio longer than 30 seconds. Source: OpenAI Speech to Text guide, `https://developers.openai.com/api/docs/guides/speech-to-text`.
- Deepgram diarization supports speaker assignment, versioned diarization models, and returns speaker/speaker confidence values for pre-recorded audio. Source: Deepgram diarization docs, `https://developers.deepgram.com/docs/diarization`.
- AssemblyAI supports `speaker_labels`, `speakers_expected`, and min/max speaker options. Source: AssemblyAI speaker labeling docs, `https://www.assemblyai.com/docs/pre-recorded-audio/label-speakers`.
- Azure Speech fast transcription supports diarization and speaker identifiers, with diarization options and constraints. Source: Microsoft Learn fast transcription guide, `https://learn.microsoft.com/en-us/azure/ai-services/speech-service/fast-transcription-create`.

### Testing and quality

- Ragas provides metrics such as context precision/recall, response relevancy, faithfulness, answer accuracy, context relevance, and response groundedness, useful for testing RAG/document-preparation quality. Source: Ragas docs, `https://docs.ragas.io/en/stable/`.
- OpenTelemetry Python provides standard instrumentation for traces/metrics/logs. Source: `https://opentelemetry.io/docs/languages/python/instrumentation/`.
- Playwright provides trace viewer, screenshots/video-on-failure, and browser-level verification for UI/privacy behavior. Source: `https://playwright.dev/docs/trace-viewer`.

---

## 3. Subsystem maturity map

| Subsystem | Current maturity | Works when | Fails / degrades when | Required direction |
|---|---:|---|---|---|
| Marker PDF/image path | Medium-high | Marker models installed; supported PDF/image/EPUB; memory sufficient | GPU/VRAM limits; bad page ranges; mixed routing sampled; custom renderer/processor side effects | Keep Marker as primary PDF path, harden routing, memory, tests |
| Native DOCX/PPTX/XLS/XLSX/HTML/XML/JSON/text | Low-medium | Simple files; Markdown output only | Complex layout, comments, formula semantics, embedded assets, requested JSON/HTML/chunks | Use Docling/MarkItDown/Pandoc appropriately; define format capabilities |
| Output formats | Low | Marker path multi-format | Native path requested non-Markdown; downloads mislabeled | Single renderer registry + enforce available formats |
| Semantic/RAG chunking | Low | Marker stock `chunks` renderer for Marker path | CLI/MCP chunk tool; native converters; semantic metadata | Introduce `ChunkingService`; Docling HybridChunker/Unstructured fallback |
| Audio local STT | Medium | faster-whisper installed; local transcript/notes | diarization, cloud STT, enhancement, benchmark, fusion | Provider adapters + implementation-state matrix |
| Video | Low | ffmpeg installed, simple audio/frame sampling | no real visual semantics, scene detection, timestamps, OCR optional | Reframe as experimental or integrate a proper media pipeline |
| ZIP archives | Low-medium | small deterministic text/native children | child assets lost; nested/big archives; no structured child output | Child artifact bundling + zip bomb safeguards |
| MCP/CLI | Medium | basic conversion/job/read settings | schema drift; chunking not semantic; lifecycle/scopes partial | Generate from same contract registry; implement resources/roots/cancel correctly |
| Frontend output preview | Medium | trusted Markdown, local assets | untrusted remote images | URL sanitizer + CSP + explicit asset resolver |
| Persistence/migrations | Low-medium | fresh SQLite DB | schema evolution, production DBs | Alembic as real migration path |
| Testing | Medium for unit, low for product | simple unit conditions | cross-surface drift, browser privacy, real fixtures, provider adapters | Golden corpus + contract + E2E + eval harness |

---

## 4. Confirmed and high-confidence findings

### MUI-001 â€” Non-PDF output formats are falsely advertised and can produce mislabeled files

**Severity:** P0
**Confidence:** confirmed from source

#### What is happening

The REST upload endpoint accepts `output_format` and `output_formats` with `markdown`, `json`, `html`, and `chunks`. TaskManager also treats those four values as supported globally. However, `ConversionService.supports_multiple_formats()` explicitly says only marker-backed engines can render several formats; native engines produce one Markdown output. Native converters such as `TextDataConverter`, `HtmlConverter`, `SpreadsheetConverter`, `OfficeDocxConverter`, `OfficePptxConverter`, `AudioConverter`, `VideoConverter`, `ArchiveConverter`, and `NotebookConverter` return `extension="md"` and Markdown text.

The dangerous part is the finalization helper: when a native converter returns one Markdown envelope but the jobâ€™s primary `output_format` is `json`, `_formats_payload_for_finalize()` still stores the Markdown text under the key `json`. The download route then maps `json` to `.json` and `application/json` even if the content is a Markdown table or fenced code block.

#### Evidence in code

- `backend/app/routes/convert.py` accepts `output_format: markdown, json, html, chunks` and `output_formats`.
- `backend/app/services/task_manager.py` `_resolve_requested_formats()` supports the same four values globally.
- `backend/app/services/conversion_service.py` says native engines are single Markdown output and `supports_multiple_formats()` returns false for them.
- `backend/app/conversion/converters/text_data.py` returns Markdown for CSV/TSV/JSON/JSONL/text.
- `backend/app/services/task_manager.py` `_formats_payload_for_finalize()` stores `payload[primary_format] = primary_result.text` if the primary format is missing.
- `backend/app/routes/convert.py` `download_result()` maps `json -> .json` and `application/json`.

#### How to reproduce

1. Upload `sample.csv` with `output_format=json` or `output_formats=json`.
2. Wait for completion.
3. Download `format=json`.
4. Expected if feature is real: valid JSON.
5. Actual likely behavior: Markdown table content is returned with `.json` extension and JSON media type.

This same class of bug applies to `chunks`, `html`, and native Office/text/archive/audio/video paths.

#### Why it matters

This is a user trust issue and an integration-breaking issue. An agent or downstream system that expects JSON will parse invalid content. It also makes the UI look like it supports more than the backend can guarantee.

#### Best solution

Create a single `OutputFormatRegistry` and attach available renderers to each engine. Do not infer formats from global strings.

Minimum contract:

```python
@dataclass(frozen=True)
class OutputFormatSpec:
    id: str                 # markdown, html, json, chunks, text
    extension: str          # md, html, json, jsonl, txt
    media_type: str
    renderer_kind: Literal["native", "marker", "docling", "derived"]
    requires_structured_ir: bool = False

@dataclass(frozen=True)
class EngineSpec:
    id: str
    supported_extensions: set[str]
    output_formats: set[str]
    default_output_format: str = "markdown"
```

Immediate rule:

- If the resolved engine cannot produce the requested format, return a clear 400 before starting the job, or downgrade only if the user explicitly allows fallback.
- `available_formats` should come from the resolved engine, not guessed from `formats_json`.
- For native converters, either expose only `markdown` and `raw`, or define a real JSON envelope: `{text, elements, metadata, assets}` with `application/json`.

Higher-quality next step:

- Use Docling for structured JSON and chunks where a `DoclingDocument` is available.
- Keep MarkItDown as a Markdown fallback, not as the source of truth for JSON/chunks.

#### Acceptance criteria

- A CSV requested as `json` either fails early with `unsupported output_format for engine text_data`, or returns valid JSON whose schema is documented.
- A DOCX requested as `chunks` either fails early or returns a valid chunks JSON/JSONL payload with chunk IDs and metadata.
- The UI never shows an HTML/JSON/chunks tab for an engine that cannot produce it.
- Downloads have content that matches filename extension and media type.

#### Testing plan

- Unit: registry says native text/spreadsheet/audio/video have only supported formats.
- REST integration: upload CSV/DOCX/PPTX/audio with each format; assert response behavior.
- Download contract: validate MIME + extension + parseability for JSON/chunks.
- Frontend: selected file plan disables unsupported format chips.
- MCP/CLI: capabilities output exactly matches registry.

---

### MUI-002 â€” Runtime fallback to `marker_pdf` is unsafe and can route unsupported files into Marker

**Severity:** P0
**Confidence:** confirmed from source

#### What is happening

`ConversionService.convert_file()` catches any exception from a non-Marker converter and retries the same file with `marker_pdf`. This is intended as a best-effort fallback for corrupt Office files, but `MarkerPdfConverter` only handles PDF/images/EPUB. Feeding it CSV, JSON, XLSX, DOCX, PPTX, audio, video, XML, notebook, or ZIP after a converter error can create a heavier, less understandable failure.

There is also an override bug: in `ConversionRouter._plan_engine_override()`, when `engine_override="marker_pdf"` is used on an extension that has a native route, the code copies the native route metadata but still returns `engine="marker_pdf"`. This can mark a `.csv` override as CPU-thread execution while actually running Marker.

#### Evidence in code

- `backend/app/services/conversion_service.py` catches all non-marker converter exceptions and creates a `marker_pdf` fallback plan.
- `backend/app/conversion/converters/marker_pdf.py` declares support only for `.pdf`, image formats, and `.epub`.
- `backend/app/conversion/router.py` `_plan_engine_override()` mutates label/resource metadata when `engine == "marker_pdf" and ext in _EXT_TO_ENTRY` but still returns `engine=engine`.

#### How to reproduce

1. Upload malformed JSON (`{bad`) or JSONL with one invalid line.
2. `TextDataConverter` raises `json.JSONDecodeError`.
3. ConversionService catches and attempts Marker PDF fallback.
4. Result is a confusing Marker failure instead of a precise JSON parsing failure.

For the override bug:

1. Convert `sample.csv` with `engine_override=marker_pdf`.
2. The plan can report native-looking labels/resource requirements while the engine stays `marker_pdf`.
3. The job may be scheduled to the wrong backend.

#### Best solution

Fallback must be compatibility-aware and explicit.

Rules:

1. Fallback only to engines whose `supported_extensions` includes the file extension.
2. Do not fallback from a deterministic parser to Marker unless Marker actually supports the file.
3. Preserve the original exception as the primary error.
4. For Office/Text native failures, use a compatible fallback such as MarkItDown or Pandoc/Docling, not Marker.
5. Engine override should be rejected if incompatible, unless an explicit `--force-unsafe-engine` debug flag exists.

Suggested fallback chains:

| Input | Primary | Fallback 1 | Fallback 2 |
|---|---|---|---|
| PDF | LiteParse or Marker | Marker | none |
| Image | Marker | none | none |
| EPUB | Marker or Docling | MarkItDown/Pandoc | none |
| DOCX/PPTX/XLSX | Docling | MarkItDown | legacy converter |
| CSV/JSON/XML/HTML | native/Pandoc/Docling | MarkItDown | plain text with parse warning |
| Audio | selected provider | local faster-whisper if user allowed fallback | fail |
| Video | media pipeline | audio-only transcript | fail with partial artifact |

#### Acceptance criteria

- Unsupported engine overrides return a deterministic validation error.
- Native parse errors do not invoke Marker unless the file is a Marker-supported type.
- Error payload includes original exception type, file type, selected engine, and fallback attempts.
- No CPU-thread plan can execute a Marker-backed conversion.

#### Testing plan

- Unit: router override matrix across every extension and engine.
- Unit: fallback resolver only emits compatible engines.
- Integration: malformed JSON, broken XLSX, unsupported audio codec, corrupt ZIP.
- Regression: `.csv + engine_override=marker_pdf` is rejected.

---

### MUI-003 â€” Job submission can race database commit and cause skipped finalization

**Severity:** P0
**Confidence:** high-confidence likely

#### What is happening

In REST upload, the job is added and flushed to the current DB session, but the dependency commits only after the endpoint returns. The code then starts the task manager before the request transaction is committed. For very fast CPU/native jobs, the worker thread can open a new session inside `_finalize_job()` before the job row is visible. `_finalize_job()` checks whether the job exists; if not, it logs and returns without writing completion.

#### Evidence in code

- `backend/app/database.py` dependency commits after yielding to the endpoint.
- `backend/app/routes/convert.py` adds the job, flushes, records audit, enqueues durable job, and calls `task_manager.submit_job()` before returning.
- `backend/app/services/task_manager.py` `_finalize_job()` opens a new session and skips finalization if the job is not found.

#### How to reproduce

1. Use a very fast deterministic converter such as `.txt` or `.tsv`.
2. Inject a test converter that returns immediately.
3. Submit via `/api/convert/upload`.
4. Observe whether `_finalize_job()` can run before the upload route commits.

This race is more likely under SQLite and fast tests.

#### Best solution

Make job submission transactionally explicit.

Preferred design:

1. Insert job row and durable queue row in one transaction.
2. Commit the transaction.
3. Only after commit, submit the in-memory worker.
4. If worker submission fails, mark the job failed or leave it queued for durable recovery.

Implementation approach:

```python
async with session.begin():
    session.add(job)
    await durable_queue.enqueue(...)
# commit has happened here
try:
    task_manager.submit_job(...)
except Exception:
    await mark_job_failed(...)
```

FastAPI route should not rely on dependency auto-commit for a side effect that must be visible to a worker.

#### Acceptance criteria

- A zero-delay test converter always completes.
- `_finalize_job()` never sees a missing just-submitted job.
- Durable queue recovery can pick up committed jobs if in-memory submission fails.

#### Testing plan

- Integration with a mocked instant converter.
- Stress test 100 parallel `.txt` uploads.
- Simulate task_manager.submit_job raising after DB commit.
- Durable queue recovery test after forced process restart.

---

### MUI-004 â€” Cancel/delete lifecycle is inconsistent and running thread cancellation is not reliable

**Severity:** P0
**Confidence:** confirmed design defect

#### What is happening

REST `DELETE /api/convert/{job_id}` cancels if running, deletes the upload, deletes result files, and deletes the DB row in one operation. MCP exposes `marker_cancel_job` and `marker_delete_job`, but `marker_cancel_job` calls `delete_job(delete_files=False)`, so it still deletes the job record rather than preserving a cancelled job with output/log state.

For thread-backed conversions, `future.cancel()` does not stop a Python function already running inside a `ThreadPoolExecutor`. The code cannot safely kill the process because it is the main server process. Therefore a long running Marker conversion can continue after cancellation/deletion.

#### Evidence in code

- `backend/app/routes/convert.py` REST delete does cancellation and deletion in one endpoint.
- `backend/app/mcp_server.py` `marker_cancel_job()` calls `delete_job(job_id, delete_files=False)`.
- `backend/app/agent_api.py` `delete_job()` deletes the row and optionally removes files.
- `backend/app/services/task_manager.py` `cancel_job()` uses `future.cancel()` for threads and only kills a PID if one exists; `_kill_pid()` refuses to kill the main server process.

#### Why it matters

Users need three separate operations:

1. **Cancel**: stop work, preserve row/logs/status.
2. **Delete history**: remove row/card after terminal state.
3. **Purge files**: delete uploaded source/output artifacts.

Combining them makes UI behavior unpredictable and makes agents unsafe: an agent asking to cancel may accidentally delete the audit trail.

#### Best solution

Introduce a formal job lifecycle state machine.

States:

`pending -> queued -> processing -> cancelling -> cancelled | completed | failed -> deleted`

Operations:

- `POST /jobs/{id}/cancel`: cooperative cancel, status becomes `cancelling`, then `cancelled`.
- `DELETE /jobs/{id}`: delete DB record only if terminal or with explicit `force=true`.
- `POST /jobs/{id}/purge-files`: remove source/output files, preserve row metadata.
- MCP mirrors those operations exactly: `marker_cancel_job`, `marker_delete_job`, `marker_purge_job_files`.

Execution design:

- Use cooperative cancellation tokens for CPU/native converters.
- Run Marker-heavy jobs in a process backend where cancellation can terminate a worker without killing the server.
- For local thread jobs that cannot be interrupted, report `cancelling` and suppress finalization if cancelled.

#### Acceptance criteria

- Cancel never deletes history by default.
- Delete never implies cancel unless explicitly requested.
- Running Marker jobs can be stopped without killing the API server.
- UI accurately shows `cancelling` and `cancelled`.

#### Testing plan

- Start a long mocked converter and cancel; assert job row remains with `cancelled`.
- Cancel a queued job; assert it never starts.
- Cancel a running process job; assert worker PID is terminated/replaced.
- Delete a completed job with and without file purge.
- MCP and REST contract parity tests.

---

### MUI-005 â€” Markdown preview can leak remote image requests

**Severity:** P0 for privacy/local-first promise
**Confidence:** confirmed from frontend source

#### What is happening

`OutputViewer` renders Markdown through `ReactMarkdown`. For Markdown image nodes, it returns:

```tsx
<img src={src} alt={alt} {...props} />
```

This means a converted document containing `![tracker](https://attacker.example/pixel?... )` causes the browser to fetch that URL when the output is viewed. This can leak IP address, user agent, timing, and possibly document/job identifiers if included in the URL.

#### Evidence in code

- `frontend/src/components/features/OutputViewer.tsx` custom `img` component passes `src` directly into `<img>`.
- No URL sanitizer or asset resolver is visible in the component.

#### How to reproduce

1. Convert a Markdown/text file containing `![x](https://example-attacker.invalid/pixel.png)`.
2. Open the Markdown tab.
3. Browser attempts to request the remote image.

#### Best solution

Default-deny remote resource loading in converted output preview.

Rules:

- Allow only:
  - relative filenames that match manifest assets,
  - backend-generated `/api/convert/assets/{job_id}/...` URLs,
  - safe `blob:` URLs created by the app,
  - optionally `data:` if enabled for trusted outputs and size-limited.
- Block `http:`, `https:`, `file:`, `ftp:`, `javascript:`, and unknown protocols in Markdown preview.
- Render blocked images as a privacy notice with a â€œcopy URLâ€ action, not an auto-fetch.
- Add a restrictive Content-Security-Policy for the frontend: `img-src 'self' blob: data:` by default.

#### Acceptance criteria

- Viewing untrusted output never triggers external network requests.
- Local extracted assets still render correctly.
- User can explicitly open/copy a blocked external link if needed.
- CSP blocks remote images even if a frontend regression reintroduces raw `<img>`.

#### Testing plan

- Unit: sanitizer rejects external image URLs and dangerous protocols.
- Playwright: intercept `https://attacker.test/*` and assert zero requests after rendering converted Markdown.
- Playwright: local asset image displays.
- Snapshot: blocked remote image shows privacy warning.

---

### MUI-006 â€” RAG/semantic chunking is absent or only available on the Marker stock chunk renderer path

**Severity:** P0/P1 depending on advertised roadmap
**Confidence:** confirmed

#### What is happening

The MCP tool `marker_read_output_chunk` is just an alias for `read_output()` with character offset and limit. It does not know about sections, tables, pages, bounding boxes, headings, captions, tokens, embeddings, or source provenance. Native converters mostly return one Markdown string and no structured chunks.

Markerâ€™s own `chunks` renderer can work for Marker-backed PDF/image paths, but that does not solve native converters or agent reading.

#### Evidence in code

- `backend/app/mcp_server.py` `marker_read_output_chunk()` calls `read_output(output_path, offset, limit)`.
- `backend/app/agent_api.py` `read_output()` counts characters and reads a slice by offset.
- `backend/app/conversion/converters/*` native converters return Markdown strings, not chunk records.

#### Why it matters

A RAG-ready document pipeline needs stable chunk IDs, metadata, hierarchy, and provenance. Offset paging is useful for long text viewing, but it is not retrieval chunking.

#### Best solution

Create a real `ChunkingService` with a two-tier approach:

1. **Preferred:** Docling `HybridChunker` for any document represented as a `DoclingDocument`.
2. **Fallback:** Unstructured element chunking (`basic`/`by_title`) over a local `DocumentElement` list.
3. **Last resort:** Markdown heading-aware splitter with token limits and table protection.

Chunk schema:

```json
{
  "chunk_id": "jobid:000123",
  "source_id": "document.pdf",
  "chunk_index": 123,
  "text": "...",
  "contextual_text": "Section > Subsection\n...",
  "section_path": ["Section", "Subsection"],
  "page_start": 4,
  "page_end": 5,
  "bbox": [x0, y0, x1, y1],
  "element_types": ["title", "paragraph", "table"],
  "asset_refs": ["image_3.png"],
  "token_count": 512,
  "char_start": 10000,
  "char_end": 12000,
  "table_header_repeated": true
}
```

Output files:

- `document.md`
- `document.chunks.jsonl`
- `document.elements.jsonl`
- `document.marker.json` manifest references all artifacts

MCP tools:

- `marker_list_chunks(output_path, section=None, page=None, query=None)`
- `marker_read_chunk(chunk_id)`
- `marker_search_chunks(...)` only after an explicit index backend is configured
- keep `marker_read_output` as raw file paging

#### Acceptance criteria

- Native DOCX/PPTX/HTML/CSV/PDF all produce chunks when `output_format=chunks` or `generate_chunks=true` is requested.
- Tables are not split row-by-row without repeated headers.
- Each chunk has stable IDs and provenance back to page/section/asset when available.
- `marker_read_output_chunk` is renamed or documented as offset paging; semantic chunking gets a separate tool.

#### Testing plan

- Unit: heading-aware splitter preserves section boundaries.
- Unit: table splitter repeats headers and respects token/character caps.
- Golden fixtures: PDF, DOCX, PPTX, XLSX, HTML, CSV, audio transcript.
- RAG eval: small QA corpus with retrieval hit@k, context precision/recall, answer faithfulness using Ragas or a local judge harness.
- Regression: chunk IDs remain stable across repeated conversions of same source/config.

---

### MUI-007 â€” Audio provider registry advertises deferred providers but only local faster-whisper actually ships

**Severity:** P1
**Confidence:** confirmed

#### What is happening

The capability matrix lists OpenAI, Groq, Deepgram, AssemblyAI, Azure, local WhisperX, and custom OpenAI-compatible STT. The actual provider registry only implements `local_faster_whisper`; all others are in `_DEFERRED_PROVIDERS` and raise `NotImplementedError` if selected.

The UI filters out `available === false`, which is good, but the backend still contains many advanced audio controls that are accepted and stored without being implemented.

#### Evidence in code

- `backend/app/audio/providers/capabilities.py` declares many provider capabilities and marks availability dynamically.
- `backend/app/audio/providers/registry.py` implements only `local_faster_whisper`; other IDs are deferred.
- `frontend/src/components/features/audio/AudioAdvancedSettings.tsx` filters selectable providers by `available !== false`.

#### Best solution

Keep the provider matrix, but add a required `implementation_state` field:

```python
implementation_state: Literal["implemented", "beta", "deferred", "unsupported"]
```

Backend rules:

- `deferred` provider selected by API/MCP/CLI returns a validation error before job submission.
- UI displays deferred providers in a â€œcoming soonâ€ read-only comparison table, not in the provider dropdown.
- Capabilities endpoint returns both `available` and `implementation_state`.

Then implement cloud adapters one at a time using official/documented APIs, not a generic guessed shape.

Recommended provider order:

1. **OpenAI**: fastest to implement, official Python SDK, current diarization model exists.
2. **Deepgram**: strong diarization/word metadata and low-latency cloud STT.
3. **AssemblyAI**: speaker labels and speaker options are well documented.
4. **Azure**: enterprise-friendly, but more auth/config complexity.
5. **Groq**: good Whisper-compatible option if project needs speed/cost diversity.
6. **WhisperX local**: optional local diarization, but heavier dependency/licensing path.

#### Acceptance criteria

- Provider dropdown never lets users select a non-shipped adapter.
- API/MCP/CLI reject deferred providers with a clear message.
- Provider capability matrix is the source of truth for UI controls.
- Each implemented provider has a fixture-based adapter test with normalized `RawTranscript` output.

#### Testing plan

- Unit: provider registry resolves implemented providers only.
- Unit: deferred providers raise validation errors.
- Contract: capabilities endpoint includes implementation state.
- Mock provider tests: each cloud adapter normalizes words, segments, speaker labels, confidence, language, duration.

---

### MUI-008 â€” Audio advanced controls are mostly no-op or deterministic shells

**Severity:** P1
**Confidence:** confirmed from code

#### What is happening

The frontend exposes controls for diarization, confidence heatmap, quality diagnostics, review requirements, text enhancement strength, structural enhancement, contradiction detection, context/fusion, cloud enhancement, and provider comparison. The backend accepts many of these in `audio_config`.

But `AudioConverter` mostly does:

1. Resolve provider.
2. Transcribe.
3. Normalize transcript.
4. If `audio_output_mode == transcript`, render transcript.
5. Otherwise call deterministic `render_enhanced_markdown()`.

`render_enhanced_markdown()` is extractive and deterministic. It does not implement true correction, rewrite strength, contradiction detection, fusion with converted context documents, provider benchmark comparison, or cloud/local enhancement provider selection.

#### Evidence in code

- Frontend sends many `audio_*` keys in `frontend/src/lib/api.ts`.
- REST route accepts arbitrary `audio_*` blob keys.
- `backend/app/conversion/converters/audio.py` only uses provider, model/language-ish config, vocabulary, diarization warning, output mode, and metadata.
- `backend/app/audio/pipeline.py` `render_enhanced_markdown()` is deterministic extractive notes.

#### Best solution

Split audio into explicit layers:

1. **STT layer**: produces raw normalized transcript.
2. **Quality layer**: computes confidence, gaps, no speech, overlap, language mismatch, diarization availability.
3. **Structure layer**: reorganizes transcript into meeting/lecture/Q&A/action formats without rewriting words.
4. **Correction/enhancement layer**: optional LLM/local rules with source-bound validation.
5. **Fusion layer**: optional conversion of context documents and transcript-authoritative synthesis.
6. **Benchmark layer**: multiple providers transcribe same fixture, compare WER/proxy metrics/vocab hits/latency/cost.

Do not expose a control until the layer exists.

For enhancement, avoid overengineering:

- Start with local deterministic structure-only modes.
- For text correction, use the existing LLM provider system with strict JSON output:
  - input transcript segments,
  - output corrected segments with segment IDs,
  - no segment may introduce uncited claims,
  - validation checks edit distance and preserves timestamps/speaker labels,
  - raw transcript always preserved.

#### Acceptance criteria

- Turning on â€œImprove Transcript Wordingâ€ changes output only through the enhancement layer and records an audit diff.
- Turning on â€œContradiction Detectionâ€ produces a `contradictions` metadata block or the UI disables it.
- Turning on â€œCompare Providersâ€ actually runs the selected comparison providers or is not available.
- Every audio output mode has a distinct renderer and tests.

#### Testing plan

- Golden transcript fixtures with known actions/questions/contradictions.
- Deterministic structure tests for meeting, lecture, Q&A, action log.
- LLM enhancement mock tests: valid output, invalid JSON, hallucinated segment, missing citation, excessive edit distance.
- Provider benchmark tests with mocked adapters and latency/cost capture.
- UI tests: unsupported toggles are disabled with clear copy.

---

### MUI-009 â€” Unknown audio provider silently falls back to local faster-whisper

**Severity:** P1
**Confidence:** confirmed

#### What is happening

`get_capability()` returns the local default for unknown provider IDs, and `build_provider()` logs a warning and falls back to `local_faster_whisper` for unknown IDs.

This is unsafe because a user who selected a provider by typo or stale preset may believe they used cloud STT, diarization, or some stronger provider, while the system silently used local faster-whisper.

#### Evidence in code

- `backend/app/audio/providers/capabilities.py` `get_capability()` falls back to default.
- `backend/app/audio/providers/registry.py` `build_provider()` falls back to default when factory is missing.

#### Best solution

Provider selection should be strict.

- Unknown provider ID: fail validation.
- Deferred provider ID: fail validation with `implementation_state=deferred`.
- Missing provider setting: default to local faster-whisper.
- Stale preset: UI warns and rewrites to default only after user confirmation.

#### Acceptance criteria

- `audio_provider=typo` fails before transcription.
- Metadata always states the actual provider used.
- UI displays stale provider warning on preset load.

#### Testing plan

- Unit: unknown provider raises `ProviderNotFoundError`.
- REST/MCP: invalid provider returns stable error payload.
- UI: stale provider badge and reset flow.

---

### MUI-010 â€” Video conversion is experimental but exposed as if it is a meaningful multimodal timeline

**Severity:** P1/P2
**Confidence:** confirmed

#### What is happening

The video converter:

- requires `ffmpeg`/`ffprobe`,
- demuxes audio and uses the audio transcriber,
- samples frames at a fixed interval,
- computes brightness/dominant color,
- optionally runs Tesseract OCR,
- renders a timeline.

It does not perform scene detection, visual semantic classification, object/chart/slide understanding, image VLM extraction, keyframe selection, or exact frame timestamp extraction. Frame timestamps are calculated by `index * interval_s`, not read from ffmpeg frame metadata.

#### Evidence in code

- `backend/app/conversion/converters/video.py` `_analyze_frame()` returns mean RGB, brightness, dominant color, optional OCR.
- `_extract_frames()` uses ffmpeg `fps=1/{interval}` and then timestamps by enumeration.
- No VLM/image-understanding service is used for frames.

#### Best solution

Rename current behavior honestly and improve in layers.

Immediate:

- Mark video as **experimental audio+keyframe summary**.
- Add timeouts to ffmpeg/ffprobe subprocess calls.
- Store warnings prominently.
- Do not claim visual understanding unless enabled.

Next:

- Use scene/keyframe selection instead of fixed frame interval. Minimal implementation can use ffmpeg scene filter; more robust optional implementation can use PySceneDetect.
- Reuse the existing image-understanding pipeline on selected keyframes only when `allow_cloud_vlm` or local OCR route permits.
- Record true timestamps from ffmpeg metadata.
- Add WebVTT output for audio transcript if requested.

#### Acceptance criteria

- Video output clearly says which modalities were used: audio, frame OCR, VLM, scene detection.
- No video job can hang indefinitely on ffmpeg.
- A no-audio video still produces a frame-only artifact with warnings.
- A no-frame/corrupt video fails cleanly.

#### Testing plan

- Fixtures: no audio, no video stream, short clip with slides, long clip with max frames, corrupt file.
- Mock ffmpeg timeout.
- Assert provenance flags match actual pipeline.
- If VLM enabled, assert only selected keyframes are sent.

---

### MUI-011 â€” ZIP archive conversion loses child assets and is not yet a serious batch document artifact

**Severity:** P1
**Confidence:** confirmed

#### What is happening

`ArchiveConverter` recursively converts deterministic children and inserts their text into the archive Markdown. It does not carry child images/assets into the parent result. If a DOCX/PPTX child creates image references, those images are not surfaced in the parent output. The archive manifest records conversion actions but not a structured child artifact bundle.

It also uses per-child size caps, but does not track total uncompressed size or compression ratio across the archive. That leaves room for resource abuse with many allowed children near the limit or unusual zip structures.

#### Evidence in code

- `backend/app/conversion/converters/archive.py` appends `child_result.text` only.
- The return value includes no `images` or `assets` collected from children.
- `_convert_child()` reads child data with `zf.read(info)` after checking `info.file_size`, but no total archive uncompressed budget is enforced.

#### Best solution

Treat archive conversion as a batch artifact, not string concatenation.

Output model:

```json
{
  "children": [
    {
      "path": "folder/file.docx",
      "engine": "docling",
      "status": "converted",
      "text_path": "children/folder_file.md",
      "assets": [...],
      "metadata": {...}
    }
  ]
}
```

Implementation:

- Namespace child assets: `archive_assets/<safe_child_path>/...`.
- Rewrite child Markdown asset refs to namespaced asset paths.
- Emit child outputs as separate files when output layout supports sidecars.
- Track `max_total_uncompressed_bytes`, compression ratio, nested depth, file count, and extraction time.
- Keep suspicious path checks.

#### Acceptance criteria

- ZIP containing DOCX with image produces parent output with working image asset refs.
- Nested ZIP respects depth and total-size caps.
- Manifest lists each childâ€™s status, output, assets, and failure reason.
- A zip bomb fixture is rejected before memory blowup.

#### Testing plan

- ZIP with text, DOCX image, PPTX image, nested ZIP, suspicious path, many files, large compressed ratio.
- Asset link rewrite tests.
- Manifest schema validation.

---

### MUI-012 â€” Native Office/spreadsheet converters are too shallow for the product standard

**Severity:** P1
**Confidence:** confirmed by implementation shape; exact failures need fixtures

#### What is happening

The native converters are hand-rolled:

- DOCX uses Mammoth to HTML, markdownify, and custom embedded image handling.
- PPTX manually traverses python-pptx shapes and approximates reading order.
- XLSX/XLS reads cell values into Markdown tables with row caps.
- HTML uses BeautifulSoup + markdownify.
- XML/RSS/Atom manually renders common structures.
- Notebooks manually serialize Markdown/code/plain outputs.

These are fine as first-pass deterministic converters, but they are not enough for â€œproduction-grade, layout-aware, search-optimizedâ€ native conversion.

#### Examples of likely gaps

- DOCX: comments, footnotes/endnotes, tracked changes, headers/footers, captions, nested tables, equations, section hierarchy, styles, hyperlinks around images.
- PPTX: slide masters, SmartArt, animations, z-order, speaker notes structure, charts with embedded workbooks, grouped shape ordering, image captions.
- XLSX: formulas vs cached values, charts, pivot tables, hidden sheets, merged cells, comments, hyperlinks, named ranges, very wide sheets.
- HTML: relative resource resolution, tables with rowspan/colspan, iframes, complex semantics, unsafe external links.
- JSON/JSONL: invalid-line tolerance, huge nested structures, schema summaries, truncation semantics.

#### Best solution

Do not keep deepening custom parsers unless there is a unique Marker UI advantage. Use established tools as backends.

Recommended backend policy:

1. **Docling first for structured native documents** where JSON/chunks/provenance matter.
2. **MarkItDown fallback for fast Markdown** where broad format coverage matters and high-fidelity structure is less critical.
3. **Pandoc for markup/text conversions** such as Markdown, HTML, LaTeX, RST, EPUB, ODT when its AST fits.
4. **Legacy custom converters only as fallback** or for Marker-specific image-understanding augmentation.

This keeps custom code focused on orchestration, privacy, provenance, and UX â€” the projectâ€™s differentiator â€” not on reimplementing Office parsers.

#### Acceptance criteria

- Native conversion backend is selectable and recorded: `docling`, `markitdown`, `pandoc`, `legacy`.
- Default backend per extension is justified by quality tests.
- Complex fixture corpus shows improvement over current custom parsers.
- Existing simple fixtures still pass.

#### Testing plan

- Golden Office corpus with expected Markdown/metadata snapshots.
- Differential tests comparing legacy vs Docling vs MarkItDown vs Pandoc for selected files.
- Human review scoring for reading order, table integrity, image handling, metadata.
- Performance budget tests for large XLSX/PPTX.

---

### MUI-013 â€” Format and capability support is duplicated across too many places

**Severity:** P1
**Confidence:** confirmed

#### What is happening

Supported extensions, formats, modes, and options are duplicated in:

- REST upload `ALLOWED_EXTENSIONS`.
- `ConversionRouter` route table.
- Safe URL content-type extension mapping.
- Frontend `ACCEPTED_EXTENSIONS`.
- Frontend `OutputFormat` and options.
- CLI/MCP capabilities and Pydantic contract.
- Audio output mode unions in REST/frontend/agent contract.

Drift already exists: REST/frontend support `interview_qna` and `action_decision_log`, but `agent_contract.AudioOutputMode` and `agent_api.build_conversion_config()` only allow transcript/enhanced/notes/meeting_notes/lecture_notes.

#### Evidence in code

- `backend/app/routes/convert.py` `ALLOWED_EXTENSIONS`.
- `backend/app/conversion/router.py` `_ROUTE_TABLE` and engine compatible extensions.
- `backend/app/services/safe_url_fetcher.py` `_CONTENT_TYPE_EXTENSIONS`.
- `frontend/src/components/features/FileUpload.tsx` `ACCEPTED_EXTENSIONS`.
- `frontend/src/lib/api.ts` audio output mode includes 7 values.
- `backend/app/agent_contract.py` audio output mode includes fewer values.
- `backend/app/agent_api.py` capabilities and conversion config include fewer audio modes.

#### Best solution

Create one `CapabilityRegistry` and export it.

Files:

- `backend/app/capabilities/formats.py`
- `backend/app/capabilities/engines.py`
- `backend/app/capabilities/options.py`
- `backend/app/capabilities/providers.py`

Expose:

- `GET /api/capabilities`
- MCP `marker_list_capabilities`
- CLI `marker capabilities --json`
- generated frontend type or runtime fetch.

Frontend should not hard-code extensions; it should load capabilities and build accept strings/options from the backend.

#### Acceptance criteria

- Adding a new extension requires one registry change.
- REST, CLI, MCP, frontend, safe URL fetcher, and docs reflect the same capabilities.
- A test fails if any surface drifts.

#### Testing plan

- Snapshot test of capabilities payload.
- Cross-surface test: REST OpenAPI enum == MCP schema enum == frontend generated type.
- Frontend unit: accept string is derived from capabilities fixture.

---

### MUI-014 â€” Agent/MCP/REST contracts have real schema drift

**Severity:** P1
**Confidence:** confirmed

#### What is happening

The REST API, frontend API types, agent contract, and MCP tools are not generated from the same schema. Drift examples:

- Audio output modes differ.
- Agent job status metadata excludes `audio`, `audio_batch`, and `video`, while REST includes them.
- MCP tool list is richer than `agent_api.TOOL_NAMES`; `marker_list_capabilities` patches the tool list at MCP layer.
- Some MCP convenience tools accept fewer options than `marker_convert_file`.

#### Best solution

Use one source of truth:

- Pydantic v2 models for backend contracts.
- OpenAPI schema for REST.
- MCP tool schemas generated from the same Pydantic models where possible.
- Frontend TypeScript generated via `openapi-typescript` or equivalent.

Do not manually maintain parallel unions.

#### Acceptance criteria

- One schema snapshot covers REST/CLI/MCP/frontend.
- Audio modes are identical everywhere.
- Job metadata fields are identical everywhere or intentionally surface-scoped with documented redaction.

#### Testing plan

- Contract snapshot test: generated frontend types match backend schema.
- MCP self-test includes schema enum checks, not just tool names.
- Regression test for every audio output mode via REST and CLI/MCP.

---

### MUI-015 â€” Source URL SSRF protection is good but still has DNS/connect-time gaps

**Severity:** P1 for enterprise/local-first deployments
**Confidence:** high-confidence risk

#### What is happening

The safe URL fetcher validates scheme, credentials, allowlist, DNS resolution, and private/local IPs before the HTTP request. It manually follows redirects and re-validates each redirect URL. This is good.

However, after validation, `httpx` connects using the hostname again. DNS can theoretically change between validation and connection. This is the classic DNS rebinding gap. Also, by default the app appears to allow any public host unless `MARKER_SOURCE_URL_ALLOWLIST` is set.

#### Evidence in code

- `backend/app/services/safe_url_fetcher.py` `assert_safe_source_url()` resolves host and checks IPs.
- `download_source_url()` then calls `client.stream("GET", current_url)` with the original URL.

#### Best solution

Security posture should depend on deployment mode.

Immediate:

- Default local GUI can keep public URL downloads, but warn clearly.
- Enterprise/server mode should require `MARKER_SOURCE_URL_ALLOWLIST` for remote downloads.
- Add audit event for host/IP chosen.

Stronger:

- Resolve and connect to pinned IPs with Host header/SNI care, or use a hardened URL fetch library/service.
- Re-resolve immediately before connect and after redirect; record final IP.
- Block redirects from HTTPS to HTTP unless explicitly allowed.
- Enforce total download time and body sniffing.

#### Acceptance criteria

- Private IP, loopback, link-local, multicast, and reserved addresses are blocked.
- Redirects to blocked IPs are blocked.
- Enterprise mode refuses source_url when allowlist is empty.
- DNS rebinding behavior is covered by a mocked resolver test.

#### Testing plan

- Unit tests for IPv4/IPv6 private ranges and credentials.
- Integration tests with a local redirect server.
- Mock `socket.getaddrinfo` to simulate validation IP != connect IP.
- Oversized content-length and streaming-over-limit tests.

---

### MUI-016 â€” Database migrations are not production-grade

**Severity:** P1/P2
**Confidence:** confirmed

#### What is happening

The project includes Alembic, but startup uses `Base.metadata.create_all()` plus a custom `_add_missing_columns()` helper. That only handles additive columns. It cannot handle renamed columns, dropped columns, type changes, data migrations, indexes, constraints, or non-SQLite production behavior.

#### Evidence in code

- `backend/app/database.py` `create_tables()` calls `Base.metadata.create_all()` and `_add_missing_columns()`.
- `_add_missing_columns()` explicitly says dropped/retyped columns are out of scope.

#### Best solution

Use Alembic as the real migration path.

- Keep self-heal additive migration only for dev/test SQLite if desired.
- Add `alembic upgrade head` in startup or deployment entrypoint.
- Add migration tests from old fixture DBs.
- Version the schema in the DB.

#### Acceptance criteria

- Fresh install creates all tables through migrations.
- Existing DB upgrades through migrations.
- CI fails if models changed without migration.

#### Testing plan

- `alembic upgrade head` from empty DB.
- Upgrade from fixture DB at previous versions.
- Downgrade only if project wants it; otherwise document forward-only.
- Model/migration diff check in CI.

---

### MUI-017 â€” Backend type/lint posture is too weak for the project ambition

**Severity:** P2
**Confidence:** confirmed

#### What is happening

`pyrightconfig.json` is set to basic type checking and disables unknown member/variable/argument/private usage reporting. Backend code uses many `Any` and dynamic dicts. This is understandable for integration-heavy code, but it lets schema drift and no-op options survive.

#### Evidence in code

- `backend/pyrightconfig.json` has `typeCheckingMode: basic` and disables several unknown-type reports.
- Many contract-sensitive layers use dicts: conversion config, metadata, capabilities, provider options.

#### Best solution

Increase type strictness around boundaries, not everywhere at once.

- Strict Pydantic models for public options/results.
- Typed `ConversionArtifact`, `EngineSpec`, `OutputFormatSpec`, `AudioProviderResult`, `ChunkRecord`.
- Keep dynamic `Any` only at third-party library boundaries.
- Add Ruff for backend lint/format and import hygiene.

#### Acceptance criteria

- Public API/config/metadata models are typed.
- No untyped dict crosses from route to converter without validation.
- CI runs pyright/mypy for boundary modules and Ruff globally.

#### Testing plan

- Type-check CI job.
- Schema property tests for serialization/deserialization.
- Mutation tests on unknown option names.

---

### MUI-018 â€” Image-understanding pipeline has powerful custom code with global side effects

**Severity:** P2, potentially P1 if Markdown rendering regressions occur
**Confidence:** confirmed design risk

#### What is happening

`ImageUnderstandingProcessor` patches `marker.renderers.markdown.Markdownify` at import time to support `marker-comment` and preserve code language in `<pre>`. This works as an integration hack, but it is a global mutation of a third-party renderer class. Any future Marker upgrade or another renderer path can break unexpectedly.

#### Evidence in code

- `backend/app/processors/image_understanding.py` imports `Markdownify` and assigns `Markdownify.convert_marker_comment` and wraps `Markdownify.convert_pre`.

#### Best solution

Move renderer customizations into a dedicated custom renderer class only, not global monkeypatching.

- Keep `ImageUnderstandingRenderer` as the owner of Markdown behavior.
- If Marker does not expose a hook, isolate patching behind an idempotent adapter with version checks and tests.
- Add a Marker version compatibility test.

#### Acceptance criteria

- Importing the processor does not mutate global Markdown behavior unless the custom renderer is active.
- Mermaid fences remain preserved.
- Marker version upgrade test catches renderer API changes.

#### Testing plan

- Unit: Markdown renderer without image-understanding is unchanged.
- Unit: image-understanding renderer preserves Mermaid code language.
- Compatibility test with pinned Marker version.

---

### MUI-019 â€” Documentation and README overstate feature completeness

**Severity:** P1 for user trust
**Confidence:** confirmed

#### What is happening

The README describes production-grade conversion, intelligent VLM image extraction, CLI/MCP, audio/video support, search-optimized output, multi-GPU execution, and local-first privacy. Some of this is real, but several parts are experimental or half-built. `docs/limitations.md` is short and does not reflect many current limitations.

#### Evidence in code/docs

- README lists many advanced capabilities.
- `docs/limitations.md` only mentions image links, VLM requirement, and RAM/VRAM footprint.
- Code shows native formats are mostly Markdown-only; chunks are not semantic; audio cloud providers are deferred; video is experimental.

#### Best solution

Adopt explicit maturity badges in docs and UI:

- Stable
- Beta
- Experimental
- Planned
- Unsupported

Every advertised feature should have:

- exact supported formats,
- dependencies,
- privacy behavior,
- quality caveats,
- test coverage status,
- known failure modes.

#### Acceptance criteria

- README feature claims map to implementation-state registry.
- UI labels experimental video/audio controls honestly.
- Docs include a real limitations matrix.

#### Testing plan

- Docs link check already exists; add capability-doc snapshot check.
- Generate docs tables from capability registry to avoid drift.

---

## 5. Architecture plan: â€œzero to one hundredâ€ without overengineering

The right plan is not to build a huge custom framework. It is to put a small number of strong architectural seams in place, then reuse existing tools behind those seams.

### 5.1 North-star architecture

```text
                  â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                  â”‚ Capability Registry       â”‚
                  â”‚ formats/engines/options   â”‚
                  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                â”‚
      REST / GUI / CLI / MCP â—„â”€â”€â”¼â”€â”€â–º Generated schemas/types/docs
                                â”‚
                  â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                  â”‚ Conversion Orchestrator   â”‚
                  â”‚ plan -> execute -> save   â”‚
                  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                â”‚
        â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
        â”‚                       â”‚                         â”‚
â”Œâ”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”     â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”       â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚ Marker Backend â”‚     â”‚ Docling Backend   â”‚       â”‚ MarkItDown/Pandocâ”‚
â”‚ PDF/image/OCR  â”‚     â”‚ structured IR     â”‚       â”‚ markdown fallbackâ”‚
â””â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”˜     â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”˜       â””â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”˜
        â”‚                       â”‚                         â”‚
        â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”´â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                       â–¼                       â–¼
              ConversionArtifact        ChunkingService
           text/elements/assets/meta     chunks/elements/provenance
                       â”‚                       â”‚
                       â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                  â–¼
                         OutputWriter + Manifest
                       text, json, chunks, assets
```

### 5.2 The central data model

Introduce a canonical artifact. It should be small enough to implement quickly but rich enough for RAG and output correctness.

```python
class DocumentElement(BaseModel):
    element_id: str
    type: Literal["title", "paragraph", "list", "table", "image", "code", "audio_segment", "video_frame"]
    text: str = ""
    markdown: str | None = None
    html: str | None = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    section_path: list[str] = []
    asset_refs: list[str] = []
    metadata: dict[str, Any] = {}

class ConversionArtifact(BaseModel):
    primary_text: str
    primary_format: str
    elements: list[DocumentElement] = []
    assets: list[Asset] = []
    metadata: dict[str, Any] = {}
    available_formats: set[str]
```

Do not try to fully model every document feature on day one. Use `metadata` for advanced third-party fields, but make chunking and output rendering operate on stable basics.

### 5.3 Reuse choices by area

| Area | Best reuse | Why | Custom code should do |
|---|---|---|---|
| PDF/image OCR | Marker | Already core strength | routing, privacy, metadata, output normalization |
| Native structured docs | Docling | Unified document model, JSON output, chunkers, broad formats | adapter, output manifest, privacy, UI integration |
| Fast Markdown fallback | MarkItDown | Broad coverage, Markdown intended for LLM/text analysis | fallback selection, manifest, warnings |
| Markup conversion | Pandoc | Mature AST and broad readers/writers | subprocess wrapper, sandbox/timeouts, caveats |
| Chunking | Docling HybridChunker + Unstructured fallback | Avoid custom RAG chunker initially | chunk schema, eval harness, metadata preservation |
| STT cloud | Official SDKs / documented APIs | Avoid fragile generic adapters | normalization, privacy gates, retries, cost metadata |
| Evaluation | Ragas/TruLens + golden fixtures | Avoid inventing metrics | fixture suite and CI gates |
| Observability | OpenTelemetry | Standard tracing/metrics | spans around conversion stages |
| Browser privacy testing | Playwright | Real network interception and traces | product-specific assertions |

### 5.4 Phased roadmap

#### Phase 0 â€” Stabilize truthfulness and prevent bad outputs

Goal: remove misleading behavior immediately.

Tasks:

1. Add capability registry for engines/extensions/formats.
2. Validate requested output format against planned engine.
3. Fix `engine_override=marker_pdf` routing bug.
4. Disable unsupported native output tabs in UI.
5. Add frontend Markdown URL sanitizer.
6. Add explicit feature-state badges for audio/video/chunking.
7. Split REST cancel/delete or at least add `POST /cancel` and mark `DELETE` as destructive.
8. Add tests for every above regression.

Success:

- No invalid JSON downloads.
- No remote image fetches in output preview.
- No unsupported engine fallback into Marker.
- UI matches actual capabilities.

#### Phase 1 â€” Canonical artifact and real chunking

Goal: make RAG outputs real.

Tasks:

1. Add `ConversionArtifact` and `DocumentElement` model.
2. Adapt current converters to produce at least text + elements where easy.
3. Add Docling backend behind optional dependency.
4. Implement `ChunkingService` with Docling HybridChunker, Unstructured fallback, and Markdown fallback.
5. Output `.chunks.jsonl` and `.elements.jsonl`.
6. Add MCP semantic chunk tools.

Success:

- Native DOCX/PPTX/HTML/CSV produce stable chunks.
- Chunks have section/page/table metadata when available.
- RAG eval harness shows improvement over offset/fixed splitting.

#### Phase 2 â€” Provider-grade audio

Goal: make audio controls real or remove them.

Tasks:

1. Add provider `implementation_state`.
2. Implement OpenAI adapter first.
3. Implement Deepgram or AssemblyAI adapter next for diarization strength.
4. Normalize provider outputs into one transcript schema.
5. Implement provider comparison with mocks and optional real integration tests.
6. Implement structure-only renderers for all audio output modes.
7. Implement enhancement/correction layer with validation and raw transcript preservation.

Success:

- Provider selection is strict.
- Diarization works for at least one cloud provider.
- Benchmark actually runs selected providers.
- Enhancement has source-bound audit trail.

#### Phase 3 â€” Native conversion excellence

Goal: replace shallow native parsing with high-quality backend selection.

Tasks:

1. Add Docling/MarkItDown/Pandoc backends.
2. Build fixture-based comparison harness.
3. Choose default backend per format based on quality/performance.
4. Preserve assets and metadata in artifacts.
5. Make legacy converters fallback-only.

Success:

- Complex DOCX/PPTX/XLSX fixtures improve in reading order, tables, images, and chunks.
- Output metadata says exactly which backend was used.

#### Phase 4 â€” Job lifecycle, queue, observability, enterprise polish

Goal: make it operationally safe.

Tasks:

1. Formal job state machine.
2. Durable queue as source of truth.
3. Process isolation for cancellable heavy jobs.
4. Alembic migrations.
5. OpenTelemetry spans/metrics.
6. Security posture modes: local/dev/enterprise.
7. MCP resources and roots aligned with spec.

Success:

- Cancel works predictably.
- Restart recovery works.
- Traces show time/cost per stage.
- Enterprise deployment has clear controls and audit events.

---

## 6. Detailed integration plans

### 6.1 Docling integration plan

Use Docling where structured representation and chunking matter most.

#### Integration shape

- Add optional extra: `marker-ui[docling]`.
- New adapter: `backend/app/conversion/backends/docling_backend.py`.
- Convert supported native formats into Docling Document.
- Export:
  - Markdown for preview,
  - JSON for structured output,
  - text for plain output,
  - chunks through `HybridChunker`.
- Normalize into `ConversionArtifact`.

#### Routing

Default candidates:

- DOCX/XLSX/PPTX/ODT/ODS/ODP/HTML/CSV/Markdown/AsciiDoc/LaTeX/EPUB: Docling first if installed.
- PDF: keep Marker first; allow `engine_override=docling_pdf` for comparison.
- Audio/video: keep current pipeline until audio/video plan matures; Docling can be evaluated later.

#### Testing

- Smoke conversion for each Docling-supported input class.
- Asset extraction comparison.
- Chunk output schema validation.
- Performance budgets and fallback behavior if Docling missing.

### 6.2 MarkItDown integration plan

Use MarkItDown as a broad Markdown fallback, not as structured IR.

#### Integration shape

- Add optional extra: `marker-ui[markitdown]`.
- Adapter returns Markdown text plus metadata: backend, warnings, dependency versions.
- Use for Office/HTML/text/ZIP/EPUB fallback and for quick CLI mode.

#### When not to use

- Do not use MarkItDown as the only source for JSON/chunks.
- Do not use MarkItDown when high-fidelity layout/structured table metadata is required.

#### Testing

- Golden Markdown snapshots for common formats.
- Fallback tests when legacy native converter fails.
- Ensure no hidden cloud call unless explicitly configured.

### 6.3 Pandoc integration plan

Use Pandoc for markup conversions where it is strong.

#### Integration shape

- Subprocess wrapper with timeout, input/output temp files, no shell.
- Supported engines: `pandoc_html`, `pandoc_docx`, `pandoc_epub`, `pandoc_rst`, `pandoc_latex`, as applicable.
- Always record warnings about possible lossiness for complex input.

#### Testing

- Subprocess timeout.
- Missing binary behavior.
- HTML/RST/LaTeX/EPUB fixtures.
- Complex table fixture marked expected lossy.

### 6.4 Audio provider adapter plan

Provider protocol:

```python
class AudioTranscriptionProvider(Protocol):
    id: str
    def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None) -> RawTranscript: ...
```

Normalized `RawTranscript` fields:

- provider
- model
- language
- duration
- segments: start, end, text, confidence, speaker, words
- provider_metadata
- warnings
- cost/latency

Implementation order:

1. OpenAI `gpt-4o-transcribe` and `gpt-4o-transcribe-diarize`.
2. Deepgram Nova with diarization/keyterms.
3. AssemblyAI with speaker labels.
4. Azure fast transcription.
5. Groq Whisper-compatible.
6. WhisperX optional local diarization.

Testing:

- Use mocked HTTP/SDK responses for CI.
- Optional real-provider tests behind env vars.
- Contract tests ensure every adapter maps to normalized schema.
- Privacy tests ensure cloud providers require opt-in.

### 6.5 Job lifecycle plan

Replace implicit cancel/delete behavior with a small state machine.

Data additions:

- `status`: queued/processing/cancelling/cancelled/completed/failed.
- `cancel_requested_at`.
- `deleted_at` if soft delete desired.
- `purged_at` for files.
- `runner_id` / `worker_pid`.
- `idempotency_key`.

Endpoints:

- `POST /api/convert/{job_id}/cancel`
- `DELETE /api/convert/{job_id}`
- `POST /api/convert/{job_id}/purge-files`

MCP tools mirror these names.

Testing:

- State transition unit tests.
- Process cancellation integration.
- Running thread cancellation behavior documented and tested.

---

## 7. Product-quality testing plan

### 7.1 Golden fixture corpus

Build a public-safe test corpus. Each fixture has expected properties, not necessarily byte-exact output.

#### PDF

- clean digital text PDF,
- scanned image-only PDF,
- OCR sandwich PDF,
- table-heavy PDF,
- figure-heavy PDF,
- multi-column academic PDF,
- mixed digital/scanned pages,
- huge PDF with page range,
- corrupt PDF,
- encrypted/password PDF.

#### Office/native

- DOCX with headings, nested tables, images, captions, footnotes, comments, track changes,
- PPTX with notes, grouped shapes, charts, images, SmartArt-like shapes,
- XLSX with formulas, hidden sheets, merged cells, comments, charts, wide tables,
- XLS legacy with dates/errors,
- HTML with relative links, tables, scripts, remote images,
- JSON/JSONL valid and invalid,
- XML/RSS/Atom with namespaces,
- notebook with text/plain, markdown, images.

#### Audio/video

- no speech,
- mono single speaker,
- two-speaker diarization fixture,
- noisy audio,
- non-English audio,
- long audio > 30 seconds,
- video no audio,
- video with slide text,
- corrupt video.

#### Archive

- ZIP with text,
- ZIP with DOCX/PPTX images,
- nested ZIP,
- zip slip paths,
- compression-ratio stress,
- many files.

### 7.2 Test layers

| Layer | What to test | Tools |
|---|---|---|
| Unit | Registry, routing, fallback, sanitizers, provider normalization | pytest/vitest |
| Contract | REST/MCP/CLI/frontend schema parity | Pydantic/OpenAPI snapshots |
| Integration | Upload -> status -> download for every format | httpx async client |
| E2E browser | UI conversion flows, remote-image blocking, tabs | Playwright |
| Golden output | Reading order, table integrity, assets, chunks | pytest snapshots + property asserts |
| Security | SSRF, zip bombs, path traversal, CSP | pytest + local test server |
| Performance | memory/time budgets, concurrency, cancellation | pytest-benchmark/locust-like scripts |
| RAG quality | chunk retrieval/answer grounding | Ragas/TruLens/local judge |
| Provider | cloud adapters with mocks and optional real tests | respx/vcrpy/env-gated tests |

### 7.3 CI gates

Minimum gates before marking a feature stable:

- Unit tests pass.
- Contract drift test passes.
- Golden fixtures pass for that feature.
- Browser/privacy test passes if UI renders user-controlled content.
- No unsupported format can be selected or downloaded as the wrong MIME type.
- Docs generated from registry include feature state.

### 7.4 RAG evaluation gates

For chunking/RAG output, define measurable goals:

- chunk parseability: 100% valid JSONL,
- chunk ID uniqueness: 100%,
- table chunks include headers when split,
- retrieval hit@5 improves over fixed-size splitter on fixture QA set,
- context precision/recall measured with Ragas or equivalent,
- answer faithfulness does not regress when chunking changes.

---

## 8. Immediate issue backlog

### P0 â€” must fix before broad release

1. Native requested JSON/HTML/chunks must not return mislabeled Markdown.
2. Compatibility-aware fallback; remove generic Marker fallback.
3. Fix `engine_override=marker_pdf` route metadata/backend bug.
4. Commit job rows before worker submission.
5. Add Markdown image URL sanitizer and CSP.
6. Add capability registry skeleton and wire REST/UI/MCP basics.
7. Split cancel/delete semantics at least at API level.

### P1 â€” next quality tranche

1. Real semantic chunking service and chunk artifact files.
2. Docling/MarkItDown/Pandoc backend integration and native converter fallback policy.
3. Audio provider implementation-state matrix.
4. First cloud STT provider adapter with tests.
5. Archive child asset preservation.
6. Alembic migration workflow.
7. Contract generation for frontend/MCP/REST.
8. Golden fixture test corpus.

### P2 â€” polish and scale

1. Video keyframe/scene pipeline.
2. OpenTelemetry spans and metrics.
3. Provider benchmark and audio enhancement validation.
4. Strict typing around public contracts.
5. Renderer patch isolation.
6. Enterprise source URL posture and DNS pinning.

---

## 9. What â€œsuccessful implementationâ€ means

A fix is successful only when all of these are true:

1. **Correctness:** output content matches its declared format, schema, extension, and media type.
2. **Truthfulness:** UI/docs/API do not advertise unsupported behavior.
3. **Provenance:** converted text/chunks/assets can be traced back to source sections/pages/files where possible.
4. **Privacy:** no user content is sent to cloud or external URLs unless explicitly allowed and audited.
5. **Recoverability:** failed/cancelled jobs leave clear state and actionable errors.
6. **Interoperability:** REST, CLI, MCP, and frontend share contracts.
7. **Quality:** feature has golden fixtures and measurable acceptance gates.
8. **Maintainability:** no new broad custom parser is added where an existing high-quality tool solves the problem.
9. **Performance:** large files respect documented time/memory/size limits.
10. **User experience:** advanced features are powerful but discoverable, disabled when unavailable, and accompanied by clear explanations.

---

## 10. Final assessment

Marker UI is worth hardening. The project has the right ambition and several valuable components. The main risk is that the product has outgrown ad hoc feature additions. The next stage should be a deliberate architectural consolidation:

- one registry,
- one artifact model,
- real chunking,
- honest provider capabilities,
- strict lifecycle,
- strong fixture/eval suite.

That plan avoids overengineering because it does not attempt to build a new universal converter from scratch. It preserves Marker where Marker is strong, adopts Docling/MarkItDown/Pandoc/Unstructured where they are stronger than custom code, and focuses custom engineering on the parts that make Marker UI special: local-first orchestration, privacy, UX, provenance, and agent-friendly outputs.
