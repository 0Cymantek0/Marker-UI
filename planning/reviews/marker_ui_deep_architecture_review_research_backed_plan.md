# Marker UI — Extreme Deep Code Review and Research-Backed Remediation Plan

> Status note (2026-07-09): This is a historical static-review input from
> 2026-07-03. Several P0/P1 findings have since been fixed or partially fixed
> in the local repo. Treat detailed findings as audit history and consult
> `planning/reviews/README.md` for the current status ledger before using this
> document as implementation truth.

**Repository:** `0Cymantek0/Marker-UI`  
**Branch reviewed:** `master`  
**Review date:** 2026-07-03  
**Review method:** static source review through the GitHub connector, supported by current external research from official/project documentation. I could not clone the repository directly in the execution container because outbound DNS to GitHub is blocked in this environment; therefore, code evidence is cited as repository paths and line ranges observed through GitHub connector fetches/searches.

---

## 0. Executive summary

Marker UI has the foundation of a strong local-first document conversion platform: it wraps Marker PDF conversion, routes some native formats away from GPU, has a frontend that exposes advanced controls, and has started an agent/CLI/MCP surface. The problem is that the project currently exposes a much more complete product than the backend can truthfully deliver. The deepest architectural issue is not one missing converter or one missing option; it is that the system does not yet have a single canonical contract for **what a conversion produces**, **which formats are genuinely renderable**, **which options are valid for a provider/engine**, **which lifecycle operation is destructive**, and **which outputs are safe to display or read**.

The highest-risk areas are:

1. **Output-format truthfulness is broken.** REST, CLI/MCP, frontend, and the format cache all advertise `markdown`, `json`, `html`, and `chunks`, but most non-PDF/native converters only produce Markdown. A user can request JSON/HTML/chunks for DOCX/PPTX/CSV/audio/video/archive and receive Markdown stored under the requested format key. This is a product-integrity bug, not merely a UX issue.
2. **Runtime fallback can make bad situations worse.** Any non-marker converter failure falls back to `marker_pdf`, even for formats MarkerPdfConverter does not accept, and the registry does not enforce `converter.accepts(...)` before execution.
3. **Chunking is only paging.** `marker_read_output_chunk` is the same offset/limit file reader as `marker_read_output`; it is not semantic chunking, not RAG-aware chunking, and not tied to document structure.
4. **Audio UI has many “professional” controls, but the backend only has a local faster-whisper adapter.** Cloud STT provider records and capability rows exist, but provider adapters are not shipped. Enhancement, contradiction detection, fusion, and benchmark controls largely do not alter output.
5. **Privacy is still leaky in the frontend.** `ReactMarkdown` auto-renders image URLs through a normal `<img src=...>` path. A converted Markdown document can cause the user’s browser to request external image URLs.
6. **Lifecycle semantics are inconsistent.** REST `DELETE /api/convert/{job_id}` cancels if running and deletes the job and files in one operation. MCP has `cancel` and `delete`, but `cancel` still calls the delete helper with `delete_files=False`, removing the DB record. This is not clean enterprise lifecycle behavior.
7. **Format/option support is duplicated in at least five places.** REST, frontend TypeScript, agent contract, MCP parameters, and format-store allow-lists drift from one another.

The best path forward is **not** to build a huge custom document framework from scratch. The best path is a small custom product kernel around existing proven tools:

- Use **Marker** where it is strongest: PDF/image OCR, layout, tables, and Marker-native renderers.
- Use **Docling** where it is strongest: a canonical document representation, multi-format export, and native `HybridChunker`/hierarchical chunking for RAG. Docling explicitly supports unified representation, PDF/DOCX/XLSX/PPTX/HTML/CSV/images/audio/video inputs, and JSON/Markdown/HTML/text outputs, and its chunkers operate on the document model rather than only post-processed Markdown. See Docling supported formats and chunking docs: https://docling-project.github.io/docling/usage/supported_formats/ and https://docling-project.github.io/docling/concepts/chunking/.
- Use **MarkItDown** as a lightweight Markdown-first fallback for broad file support when a canonical structure is not needed. Microsoft describes MarkItDown as an LLM-oriented converter that supports PDF, PowerPoint, Word, Excel, images, audio, HTML, text formats, ZIP, YouTube URLs, EPUB, and more, with output intended for text analysis rather than high-fidelity human conversion: https://github.com/microsoft/markitdown.
- Use **Pandoc** only where its AST and media extraction are a good fit, especially DOCX/HTML/EPUB/Markdown transformations with `--extract-media`. Pandoc itself documents media extraction behavior and many extension controls; it should be a tool in the toolbox, not the only document engine: https://pandoc.org/MANUAL.html.
- Use provider APIs/SDKs for STT rather than pretending a custom provider layer exists. OpenAI, Groq, Deepgram, and Azure have documented transcription endpoints and provider-specific features; adapters should map those into a single internal transcript schema.
- Build custom only where Marker UI is legitimately adding product value: privacy policy, option registry, format registry, provenance-preserving output envelope, unified job lifecycle, provider capability gating, test harnesses, and UI workflows.



---

## 0.1 Current capability truth table

This table is intentionally blunt. It describes what the system appears to do today under normal conditions, and where it should not yet be treated as production-complete.

| Area | Current extent of working behavior | Conditions where it breaks or becomes misleading | Remediation direction |
|---|---|---|---|
| PDF via Marker | Likely the strongest path. Marker service wraps Marker converters, can render multiple formats from one parsed document, and has image-understanding hooks. | Expensive on CPU/low VRAM; image-understanding requires careful cloud/VLM gating; mixed routing and LiteParse fallback need more quality tests. | Keep Marker as primary PDF/image engine. Harden routing, OOM tests, manifest/chunk output, and VLM privacy. |
| Clean digital PDF via LiteParse | Routed when probe says text layer is clean; has short-output fallback to Marker. | Short-output heuristic is coarse. Mixed PDF route requires full-page probe and has stitched Markdown only. | Keep as fast path, but add fixture-based route-quality evaluation and per-page failure accounting. |
| Images | Routed to Marker image OCR. Image-understanding processor exists. | Frontend can auto-load Markdown image URLs; VLM path needs privacy and provider validation. | Keep local OCR first; add safe asset proxy and blocked external image preview. |
| DOCX | Mammoth-based Markdown conversion exists, with embedded image processing. | Does not produce true JSON/HTML/chunks; may lose Word-specific structure; fallback to Marker PDF is unsafe; tables/footnotes/comments/equations need validation. | Use Docling/MarkItDown/Pandoc as evaluated backends; keep current converter as fallback. |
| PPTX | python-pptx traversal extracts text, tables, charts, notes, and images in simple cases. | Shape ordering, charts, grouped shapes, SmartArt, speaker notes, and assets are hard to get right manually; only Markdown output. | Evaluate Docling/MarkItDown; use PyMuPDF/LibreOffice-rendered slide images only if measured useful, not default. |
| XLSX/XLS | Basic sheet-to-Markdown table extraction works via openpyxl/xlrd. | Formulas, merged cells, charts, hidden sheets, large sheets, typed JSON, and sidecar CSV are incomplete. | Keep native reader but add structured sheet JSON and CSV sidecars; compare with Docling/MarkItDown. |
| CSV/TSV/JSON/JSONL/text | Basic deterministic Markdown/fenced conversion works. | JSON output is not a structured conversion artifact; invalid JSON raises; huge CSV handling is simplistic. | Add typed text/data artifact schema and honest format support. |
| HTML/XML/RSS/Atom | Simple Markdown conversion works. | Scripts/styles removed, but remote assets, complex tables, MathJax, and structured JSON are not first-class. | Use Pandoc or Docling where structure/media matters; keep simple converter for safe Markdown fallback. |
| ZIP | Recursively converts deterministic children under caps and skips dangerous paths. | Child assets are not fully preserved; no global uncompressed budget; audio batch is partial; PDF/image children are skipped. | Add archive budget object, child manifests, and namespaced child assets. |
| Audio | Local faster-whisper path exists, with normalized transcript, warnings, vocabulary report, and deterministic notes. | Cloud providers are declared but not implemented; many advanced UI controls are no-op; diarization unsupported locally unless future adapter added. | Implement real adapters and separate raw/extractive/LLM-enhanced output modes. |
| Video | ffmpeg demux/transcribe plus fixed-frame OCR/brightness timeline exists. | Not true video understanding; no scene detection; frame timestamps approximate; keyframe assets not persisted as first-class output. | Use PySceneDetect + keyframe assets + audio transcript + optional OCR/VLM. |
| MCP/CLI | Real surface exists with many tools/resources/prompts and some scopes. | Chunk tool is offset paging; lifecycle semantics are not clean; option schemas drift from REST/UI. | Generate tools/options from registry; add semantic chunks; split cancel/delete. |
| Frontend preview | Markdown/HTML/JSON/raw/audio tabs exist and regenerate path exists. | Multi-format availability is guessed by filename; remote Markdown images auto-load; advanced controls may not match backend capability. | Drive UI from plan/capability registry and safe rendering policy. |

---

## 1. Review scope and evidence model

### 1.1 Files and subsystems inspected

The review inspected the following areas through GitHub connector reads/searches:

- Conversion router, registry, result envelope, and orchestrator:
  - `backend/app/conversion/router.py`
  - `backend/app/conversion/registry.py`
  - `backend/app/conversion/result.py`
  - `backend/app/services/conversion_service.py`
- Native converters:
  - `backend/app/conversion/converters/text_data.py`
  - `backend/app/conversion/converters/html.py`
  - `backend/app/conversion/converters/xml_rss.py`
  - `backend/app/conversion/converters/notebook.py`
  - `backend/app/conversion/converters/office_docx.py`
  - `backend/app/conversion/converters/office_pptx.py`
  - `backend/app/conversion/converters/spreadsheet.py`
  - `backend/app/conversion/converters/archive.py`
  - `backend/app/conversion/converters/audio.py`
  - `backend/app/conversion/converters/video.py`
  - `backend/app/conversion/converters/marker_pdf.py`
- Output writer/cache and task manager:
  - `backend/app/services/output_writer.py`
  - `backend/app/services/format_store.py`
  - `backend/app/services/task_manager.py`
- Audio provider layer and pipeline:
  - `backend/app/audio/providers/capabilities.py`
  - `backend/app/audio/providers/registry.py`
  - `backend/app/audio/providers/faster_whisper.py`
  - `backend/app/audio/pipeline.py`
  - `backend/app/audio/transcribe.py`
  - `backend/app/audio/vocabulary.py`
- REST routes/settings:
  - `backend/app/routes/convert.py`
  - `backend/app/routes/settings.py`
- CLI/MCP/agent contracts:
  - `backend/app/agent_api.py`
  - `backend/app/agent_contract.py`
  - `backend/app/cli.py`
  - `backend/app/mcp_server.py`
  - `backend/app/mcp_resources.py`
  - `backend/app/mcp_prompts.py`
- Security and policy:
  - `backend/app/services/safe_url_fetcher.py`
  - `backend/app/services/policy.py`
  - `backend/app/security/auth.py`
  - `backend/app/security/scopes.py`
- Frontend:
  - `frontend/src/lib/api.ts`
  - `frontend/src/components/features/OutputViewer.tsx`
  - `frontend/src/components/features/ConversionOptions.tsx`
  - `frontend/src/components/features/FileUpload.tsx`
  - `frontend/src/components/features/audio/AudioAdvancedSettings.tsx`
- Project/package files:
  - `README.md`
  - `pyproject.toml`
  - `backend/requirements.txt`
  - `frontend/package.json`

### 1.2 External research sources used

Primary/project documentation used for solution design:

- Docling supported formats: https://docling-project.github.io/docling/usage/supported_formats/
- Docling chunking: https://docling-project.github.io/docling/concepts/chunking/
- Microsoft MarkItDown README: https://github.com/microsoft/markitdown
- Pandoc manual: https://pandoc.org/MANUAL.html
- Unstructured open-source overview and limitations: https://docs.unstructured.io/open-source/introduction/overview
- OpenAI speech-to-text docs: https://developers.openai.com/api/docs/guides/speech-to-text
- Groq speech-to-text docs: https://console.groq.com/docs/speech-to-text
- Deepgram prerecorded audio docs: https://developers.deepgram.com/docs/pre-recorded-audio
- Azure fast transcription docs: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/fast-transcription-create
- WhisperX repository: https://github.com/m-bain/whisperX
- pyannote.audio repository: https://github.com/pyannote/pyannote-audio
- PySceneDetect docs: https://www.scenedetect.com/docs/latest/
- MCP security best practices/spec pages:
  - https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices
  - https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
  - https://modelcontextprotocol.io/specification/2025-06-18/server/tools
  - https://modelcontextprotocol.io/specification/2025-06-18/server/resources

### 1.3 Confidence categories

- **Confirmed from code:** The issue is directly visible in source control and does not require runtime execution to prove the logic defect.
- **Highly likely, dynamic validation needed:** The source strongly suggests the defect, but final confirmation should come from running a fixture or environment-specific test.
- **Strategic gap:** The code may not be “wrong,” but the product promise, docs, UI, and implementation do not yet match a production-grade standard.

---

## 2. Design principles for the remediation plan

### 2.1 Reuse-first, custom-second

The remediation should start from this rule:

> Build custom only when Marker UI can produce a materially better outcome than existing open-source or provider-native solutions. “Materially better” means better privacy, better provenance, better user workflow, better engine routing, or better quality measurement — not simply “we can write our own version.”

This implies:

- Do **not** build a custom semantic chunker from scratch before evaluating Docling’s `HybridChunker` and hierarchical chunkers.
- Do **not** build custom Office parsers for all DOCX/PPTX/XLSX semantics when Docling, MarkItDown, Pandoc, and format-native libraries already exist.
- Do **not** fake cloud STT through a local provider; implement actual provider adapters or do not show those providers as active choices.
- Do **not** expose JSON/HTML/chunks as if they are true render formats for converters that only emit Markdown.
- Do **not** write custom video scene logic if PySceneDetect already solves scene/keyframe detection and ffmpeg already solves demuxing/extraction.

### 2.2 The custom kernel that is justified

Custom work is justified in these areas because it is Marker UI’s product value:

1. **Canonical capability registry:** one source of truth for formats, engines, provider features, option schema, security requirements, and UI availability.
2. **Conversion result envelope:** a stable object that records text, assets, metadata, provenance, warnings, structured chunks, and renderable formats.
3. **Policy layer:** local-first privacy gates, root scoping, safe URL fetching, output read/write scoping, cloud opt-in, and audit trails.
4. **Adapter normalization:** provider-specific STT/OCR/VLM outputs normalized into common internal schemas.
5. **Quality gates:** golden output tests, differential converter evaluation, chunk retrieval tests, and benchmark fixtures.
6. **UX orchestration:** clear advanced controls that only appear when meaningful, and user-friendly affordances for local/cloud tradeoffs.

### 2.3 “Excellent feature” definition

A feature should not ship as “present” until all of these are true:

- The UI, REST, CLI, MCP, docs, and tests agree on what the feature does.
- Unsupported combinations fail early with a helpful reason or are not selectable.
- Outputs are typed correctly. A `.json` download must be JSON; an HTML tab must be HTML; a chunks output must be chunk JSON/JSONL, not raw character paging.
- The feature has fixture tests for happy path, edge path, security path, and regression path.
- The implementation uses a proven external tool when possible, with a small adapter and fallback plan.
- The metadata clearly states the engine, provider, confidence, warnings, and provenance.

---

## 3. Highest-priority findings and solutions

## F-001 — Native converters can return Markdown while the system labels it JSON/HTML/chunks

**Severity:** P0  
**Category:** Product correctness / API contract / data integrity  
**Status:** Confirmed from code

### What is happening

The system exposes `markdown`, `json`, `html`, and `chunks` as supported formats in multiple places:

- `backend/app/services/format_store.py` defines `SUPPORTED_FORMATS = ("markdown", "json", "html", "chunks")` at lines 27–30 and uses this in cache parsing/merging.
- `backend/app/routes/convert.py` accepts `output_format` and `output_formats` with the same four options in the upload route.
- `frontend/src/lib/api.ts` defines `OutputFormat = 'markdown' | 'json' | 'html' | 'chunks'` at lines 7–8.
- `backend/app/agent_contract.py` defines `OutputFormat = Literal["markdown", "json", "html", "chunks"]` at line 22.
- `backend/app/mcp_server.py` exposes `OutputFormatParam` with the same list.

However, most native converters return a `UniversalConversionResult` with `extension="md"` and Markdown text only:

- `text_data.py` returns Markdown tables or fenced JSON/JSONL, but always `extension="md"` for CSV/TSV/JSON/JSONL/plain text (`backend/app/conversion/converters/text_data.py:95-123`).
- `html.py` converts HTML to Markdown and returns `extension="md"` (`backend/app/conversion/converters/html.py:50-54`).
- `xml_rss.py` returns Markdown and `extension="md"` (`backend/app/conversion/converters/xml_rss.py:57-61`).
- `notebook.py` returns Markdown and `extension="md"` (`backend/app/conversion/converters/notebook.py:63-67`).
- `spreadsheet.py` returns Markdown and `extension="md"` for XLSX/XLS (`backend/app/conversion/converters/spreadsheet.py:71-75`, `112-116`).
- `office_docx.py` returns Markdown and `extension="md"` (`backend/app/conversion/converters/office_docx.py:182-187`).
- `office_pptx.py` returns Markdown and `extension="md"` (`backend/app/conversion/converters/office_pptx.py:299-304`).
- `archive.py`, `audio.py`, and `video.py` also return Markdown (`archive.py:126-130`, `audio.py:127-164`, `video.py:95-122`).

The orchestrator explicitly states that Office/text/audio engines are “single markdown output natively” and not multi-format (`backend/app/services/conversion_service.py:86-102` in the fetched range), but the task finalizer still caches the primary result under the requested format name. `_formats_payload_for_finalize` guarantees `payload[primary_format] = primary_result.text` when no multi-format render ran (`backend/app/services/task_manager.py:80-103`). If a DOCX job requested `output_format=json`, the Markdown result can be stored as the `json` format and downloaded as `.json`.

### How to reproduce

1. Upload a DOCX, XLSX, PPTX, CSV, audio, video, or ZIP with `output_format=json` or `output_formats=json`.
2. Wait for completion.
3. Call `/api/convert/status/{job_id}` or download `format=json`.
4. Observe that the payload is not structured JSON. It is Markdown text labeled as JSON.

The same risk applies to HTML/chunks. The chunk case is especially misleading because “chunks” implies RAG chunks, while current non-PDF paths produce normal Markdown.

### Why this is serious

This breaks downstream automation. A user, MCP client, or RAG pipeline may trust the file extension/content type and parse the result as JSON. It also creates long-term compatibility debt: once clients depend on “JSON” containing Markdown, fixing it later becomes breaking.

### Best solution

Create a **single capability and format registry** and make output-format support engine-specific.

#### Minimum viable product design

Add `backend/app/conversion/capabilities.py`:

```python
from dataclasses import dataclass
from typing import Literal

OutputFormat = Literal["markdown", "html", "json", "chunks", "text"]

@dataclass(frozen=True)
class FormatCapability:
    format: OutputFormat
    media_type: str
    extension: str
    semantic: str  # rendered_text | structured_document | rag_chunks | plain_text

@dataclass(frozen=True)
class ConverterCapability:
    engine: str
    extensions: frozenset[str]
    output_formats: tuple[OutputFormat, ...]
    native_semantic_chunks: bool = False
    native_structured_json: bool = False
    supports_assets: bool = False
    cloud_required: bool = False
```

Then each converter advertises truthfully:

- `marker_pdf`: `markdown`, `html`, `json`, `chunks` because Marker can render these from its document model.
- `docling_*` future converters: `markdown`, `html`, `json`, `text`, and `chunks` if using Docling document/chunkers.
- `markitdown_*`: `markdown` only unless wrapped into a **separate** metadata JSON format that is explicitly named as such.
- Current native converters: `markdown` only, with `raw` handled by UI locally as plain view of the same content, not as output format.

#### Important contract rule

Do not silently downgrade. If a user asks for JSON and the selected engine cannot generate structured JSON, return a 400/406 with:

```json
{
  "error": "unsupported_output_format",
  "requested": "json",
  "engine": "office_docx",
  "available_formats": ["markdown"],
  "hint": "Use markdown or enable the Docling Office engine for structured JSON."
}
```

For backward compatibility, you can allow a compatibility mode for one release:

- `MARKER_STRICT_FORMATS=true` default in dev/nightly.
- log deprecation warnings when fallback happens.
- after one release, strict mode becomes default.

### Integration plan

1. Move supported format constants out of `format_store.py`, `marker_service.py`, `agent_contract.py`, frontend TypeScript, and MCP parameter definitions into a generated registry.
2. Add a `/api/convert/capabilities` or extend `/api/convert/plan` to return `available_output_formats` and `unavailable_reasons`.
3. Generate frontend TypeScript from backend registry using a checked-in script, or expose a runtime endpoint and cache it at app startup.
4. Update `ConversionOptions.tsx` so it disables unsupported formats per selected source/engine, not globally.
5. Update `TaskManager._formats_payload_for_finalize` so it only caches a format under its real format key. It must not label Markdown as JSON.
6. Update `download_result` and `write_conversion_output` to use the same registry for extension/media type mapping.

### Testing path

**Unit tests**

- `test_format_registry_declares_native_markdown_only`
- `test_output_writer_uses_registry_media_type_and_extension`
- `test_format_store_rejects_unknown_and_unrendered_formats`

**Integration tests**

- DOCX + `output_format=json` returns 400/406 or a clear unsupported-format response.
- CSV + `output_format=html` does not produce Markdown under `.html`.
- PDF + `output_formats=markdown,json,html,chunks` still produces all formats.
- Download `format=chunks` for a PDF produces valid JSON/JSONL chunks, not `.txt`.

**Success criteria**

- No native Markdown-only converter can produce a `.json` or `.html` file unless it genuinely renders that format.
- `/status.available_formats` exactly matches what can be downloaded.
- Frontend tabs match backend capability results.
- MCP capabilities and CLI `capabilities` output are generated from the same source.

---

## F-002 — Runtime fallback to `marker_pdf` is unsafe and can fail on unsupported formats

**Severity:** P0  
**Category:** Routing / reliability / resource isolation  
**Status:** Confirmed from code

### What is happening

`ConversionService.convert_file` catches any non-marker converter exception and falls back to `marker_pdf` (`backend/app/services/conversion_service.py:322-350` in the fetched range). It then calls `fb_converter.convert(filepath, config, device=device)` without checking whether MarkerPdfConverter accepts the file.

`MarkerPdfConverter` only declares support for PDF, images, and EPUB (`backend/app/conversion/converters/marker_pdf.py:37-52`). It does not accept DOCX, PPTX, XLSX, XLS, MSG, CSV, JSON, audio, video, or ZIP.

The registry also does not enforce `accepts(...)`. It selects by `engine_name` only (`backend/app/conversion/registry.py:76-115`), and `ConversionService` gets by planned engine (`backend/app/services/conversion_service.py:305-323`).

### How to reproduce

1. Corrupt a DOCX, PPTX, XLSX, JSON, or audio file so the native converter raises.
2. Submit conversion.
3. Observe the runtime fallback to Marker PDF.
4. Marker then attempts to handle a format it was not designed to process.

### Why this is serious

- It hides the true converter failure.
- It can convert a clean CPU-path failure into a heavier GPU/Marker failure.
- It violates the local resource routing model, because CPU jobs can fall into marker paths unpredictably.
- It creates confusing logs and wrong user expectations.

### Best solution

Replace “fallback to marker_pdf for all non-marker failures” with a **typed fallback policy**.

```python
@dataclass(frozen=True)
class FallbackRule:
    from_engine: str
    to_engine: str
    allowed_extensions: frozenset[str]
    reason: str
    only_on: tuple[type[Exception], ...] | None = None
```

Suggested fallback rules:

- `liteparse_pdf -> marker_pdf` for `.pdf`, on short output or parse failure.
- `marker_pdf -> no fallback` unless a specific local OCR fallback is implemented.
- `office_docx -> markitdown_docx` or `docling_docx`, not `marker_pdf`.
- `office_pptx -> markitdown_pptx` or `docling_pptx`, not `marker_pdf`.
- `spreadsheet -> markitdown_excel` or native spreadsheet fallback, not `marker_pdf`.
- `audio -> no fallback` unless local faster-whisper fallback is selected after a failed cloud provider and user allows it.
- `video -> audio-only fallback` if frame extraction fails, not Marker PDF.

### Integration plan

1. Add `fallback_policy.py` with explicit rules.
2. In `ConversionService`, when a converter fails:
   - Ask `fallback_policy.next(plan, stream_info, exc)`.
   - If no fallback, raise a domain-specific `ConversionFailedError` with the original engine and error.
   - If fallback exists, verify `fallback_converter.accepts(stream_info, fallback_config)`.
3. Put the fallback chain in metadata with the original error class and safe message.
4. Display fallback warnings in the UI.

### Testing path

- Corrupt DOCX should fail with `office_docx` error and **not** call Marker PDF.
- LiteParse short output should fallback to Marker PDF for PDF only.
- Cloud STT failure should not fallback to local unless a policy says `allow_local_fallback=true`.
- Runtime fallback metadata includes `from_engine`, `to_engine`, `reason`, and `original_error_type`.

**Success criteria**

- No engine fallback can cross into an incompatible file extension.
- Every fallback is visible in job metadata and UI.
- PDF fallback still works.

---

## F-003 — Engine override can mislabel resource requirements and route Marker work to CPU thread pool

**Severity:** P0/P1 depending on deployment  
**Category:** Routing / concurrency / GPU safety  
**Status:** Confirmed from code

### What is happening

`ConversionRouter._plan_engine_override` contains a bug:

- It resolves `engine = config["engine_override"]`.
- For `engine == "marker_pdf"` and a non-PDF extension that appears in `_EXT_TO_ENTRY`, it assigns `_engine, label, needs_marker, needs_gpu, confidence = _EXT_TO_ENTRY[ext]`.
- It then returns `ConverterPlan(engine=engine, ...)`, not `_engine`.

For example, if a DOCX is forced to `marker_pdf`, `_EXT_TO_ENTRY[".docx"]` says `office_docx`, label `Fast Office (Word)`, `needs_marker=False`, `needs_gpu=False`, `execution_backend="cpu_thread"`. But the returned engine remains `marker_pdf`. That means the selected converter is Marker, while the plan metadata and executor path can say CPU/non-marker.

Relevant code: `backend/app/conversion/router.py:245-264`.

### Why this is serious

Marker conversion uses shared model state and is intentionally isolated through a one-wide marker backend (`backend/app/services/task_manager.py:315-332`). A bad plan can route Marker work through the CPU pool, which exists precisely to parallelize non-marker jobs.

### Best solution

Make override planning strict:

1. If `engine_override` is incompatible with the extension, return a rejected plan or raise a validation error.
2. If the user chooses `marker_pdf`, the plan must always reflect Marker’s own requirements, not the native entry’s requirements.
3. Never infer label/resource fields from `_EXT_TO_ENTRY` when the engine is overridden.

### Testing path

- `.docx` + `engine_override=marker_pdf` must either be rejected as incompatible or produce `needs_marker_models=True`, `needs_gpu=True`, `execution_backend="marker_worker"` and then fail before execution because MarkerPdfConverter does not accept DOCX.
- `.png` + `engine_override=marker_pdf` is valid and uses Marker worker.
- `.pdf` + `engine_override=liteparse_pdf` is valid only for PDF.

**Success criteria**

- Plan engine, label, requirements, and selected converter always describe the same actual engine.
- No Marker converter runs on the CPU backend accidentally.

---

## F-004 — “Chunks” means offset paging, not semantic/RAG chunking

**Severity:** P0 for RAG claims, P1 for general use  
**Category:** RAG / MCP / output contract  
**Status:** Confirmed from code

### What is happening

In MCP, `marker_read_output_chunk` is implemented as:

```python
return read_output(output_path, offset=offset, limit=limit)
```

This is the same implementation as `marker_read_output` (`backend/app/mcp_server.py:250-289`). `read_output` in `backend/app/agent_api.py` counts text characters and returns a slice by offset/limit (`agent_api.py:119-169`).

This is useful paging, but it is not semantic chunking. It has no awareness of headings, pages, tables, captions, figures, token budgets, source spans, or embeddings.

### Why this is serious

The README claims “search-optimized” output, and the product exposes `chunks` as an output format. Agents and RAG systems will assume `chunks` means stable retrievable chunks with provenance. Offset paging cannot support high-quality RAG.

### Best solution

Use a two-tier chunking strategy, reusing proven tools where possible.

#### Tier 1 — Docling-native chunking for canonical documents

Docling’s docs describe native chunkers operating directly on a `DoclingDocument`; `HybridChunker` applies tokenization-aware refinements on top of hierarchical chunking, splitting oversized chunks and merging undersized chunks with the same headings/captions. It also supports table chunking with repeated headers. This is exactly the kind of reusable component Marker UI should not reinvent.

Use Docling chunking when:

- The source was processed through Docling.
- A Marker PDF output can be converted/adapted into a Docling-like document model, or when a Docling backend is chosen for Office/HTML/CSV.
- The user requests `output_format=chunks` or MCP `marker_read_output_chunked`.

#### Tier 2 — Markdown structural chunker for Markdown-only outputs

For MarkItDown/current native converters that only produce Markdown, do a lightweight structural chunker using an existing Markdown parser (`markdown-it-py` or `mistune`) rather than regex-only splitting. The custom part should be minimal: walk headings, fenced code blocks, tables, lists, and paragraphs; preserve atomic tables and code fences; split by token budget only when necessary.

Recommended output schema:

```json
{
  "schema_version": "marker.chunks.v1",
  "source": {"name": "...", "uri": null},
  "chunks": [
    {
      "chunk_id": "document_0007",
      "text": "...",
      "contextual_text": "...",
      "source_refs": [
        {"page": 3, "block_id": "...", "bbox": [0,0,0,0]}
      ],
      "headings": ["Section", "Subsection"],
      "content_types": ["table", "text"],
      "token_count": 423,
      "char_start": 1500,
      "char_end": 2210
    }
  ]
}
```

### Integration plan

1. Add a new `ChunkingService` with providers:
   - `DoclingChunkProvider`
   - `MarkdownChunkProvider`
   - `MarkerChunkRendererProvider` only if Marker stock chunk output is proven adequate.
2. Add converter capability: `supports_semantic_chunks`.
3. Add output writer support for `.chunks.json` or `.chunks.jsonl`.
4. Deprecate `marker_read_output_chunk` naming or clarify it as `marker_read_output_page`.
5. Add MCP tool `marker_read_semantic_chunks` or make `marker_get_output_chunks` return the chunk schema.

### Testing path

**Golden fixtures**

- PDF with headings, tables, figures, captions.
- DOCX with nested headings and lists.
- CSV/XLSX with long tables.
- Markdown with code fences and tables.
- Audio transcript with timestamped segments.

**Tests**

- Every chunk must include `chunk_id`, `text`, `source_refs`, and headings/context when available.
- Tables spanning chunks repeat headers or include a `table_header` field.
- Code fences are not split mid-fence.
- Chunk token counts stay under configured limit.
- Retrieval smoke: a query for a table header/row retrieves the chunk containing both.

**Success criteria**

- `chunks` output is valid JSON/JSONL with schema version.
- No chunk is just an arbitrary character slice unless explicitly using a raw paging tool.
- MCP can page chunks by chunk index/id, not character offset only.

---

## F-005 — Native converter implementation is too hand-written for the quality target

**Severity:** P1  
**Category:** Maintainability / conversion quality  
**Status:** Confirmed from code plus strategic gap

### What is happening

The native converters are manually implemented:

- DOCX uses Mammoth -> HTML -> BeautifulSoup/markdownify with custom image handling.
- PPTX walks python-pptx shapes manually, with heuristic sorting, simple table/chart extraction, and custom image handling.
- XLSX/XLS reads cell values into Markdown tables.
- HTML uses BeautifulSoup + markdownify.
- XML/RSS/Atom uses defusedxml and custom renderers.
- Notebook reads JSON and serializes Markdown/code outputs manually.
- Archive recursively converts children through a local converter map.

Some of this is good as a lightweight deterministic fallback. It is not enough as the main “production-grade, layout-aware, search-optimized” route for every non-PDF format.

### Best solution

Adopt a **converter backend strategy** instead of one custom converter per format.

#### Recommended backend priority

1. **Marker** for PDF/images/EPUB where existing Marker path is strongest.
2. **Docling** for structured Office/HTML/CSV/image/audio/video/document conversion when users request structured JSON, HTML, or RAG chunks.
3. **MarkItDown** for Markdown-first conversion when broad support and low complexity matter more than structured JSON.
4. **Pandoc** for DOCX/HTML/EPUB/Markdown transformations where its AST/media extraction is strong.
5. Existing custom converters as last-resort deterministic fallback or for very small/simple formats.

### Why Docling should be evaluated first

Docling’s supported-format docs state that it parses many document formats into a unified `Docling Document` and exports to HTML, Markdown, JSON, DocLang XML, plain text, and WebVTT. It supports PDF, DOCX, XLSX, PPTX, ODT/ODS/ODP, EPUB, Markdown, AsciiDoc, LaTeX, HTML/XHTML, CSV, images, audio, video, and schema-specific XML formats. That directly overlaps Marker UI’s stated product scope.

Docling chunking also addresses the RAG problem at the document-model level, not as post-hoc string slicing.

### Why MarkItDown should be used selectively

Microsoft describes MarkItDown as a lightweight utility for converting various files to Markdown for LLM/text-analysis pipelines, supporting PDF, PowerPoint, Word, Excel, images, audio, HTML, CSV/JSON/XML, ZIP, YouTube URLs, EPUB, and more. It says the output is meant for text analysis and may not be the best option for high-fidelity human document conversions. That makes it an excellent Markdown fallback, not a structured-output backend.

### Why Pandoc should be used selectively

Pandoc is very strong for markup/AST conversion and has mature `--extract-media` behavior for embedded images/media. But its own scope is document/markup conversion, not arbitrary OCR/layout understanding. It should not replace Marker for scanned PDFs or replace Docling/Marker for layout-heavy documents.

### Integration plan

1. Add engine names:
   - `docling_document`
   - `markitdown_markdown`
   - `pandoc_document`
2. Add backend-selection rules:
   - If requested output includes `json` or `chunks` and source type is supported by Docling, prefer Docling.
   - If requested output is Markdown only and source is a native Office/text/archive format, prefer MarkItDown or the existing deterministic path depending on quality benchmarks.
   - If source is DOCX/HTML/EPUB and user requests media extraction, evaluate Pandoc route.
3. Implement a `ConversionIR`/`DocumentArtifact` object that can hold either:
   - a DoclingDocument reference/export,
   - a Marker Document/rendered output,
   - Markdown-only content,
   - plus assets and metadata.
4. Keep current converters, but rename them internally as `legacy_*` or `deterministic_*` to make their role honest.
5. Add a benchmark harness comparing current native converter vs Docling vs MarkItDown vs Pandoc per file type.

### Testing path

For each format, create fixture categories:

- DOCX: headings, nested lists, images, tables, footnotes, comments, equations, tracked changes.
- PPTX: title slides, grouped shapes, notes, tables, charts, images, speaker notes.
- XLSX: formulas, merged cells, multiple sheets, dates, hidden sheets, large sheets.
- HTML: scripts/styles, relative images, tables, MathJax, code blocks.
- IPYNB: markdown cells, code outputs, images, rich HTML output.
- ZIP: nested archives, mixed file types, suspicious paths, large children, audio batch.

For each backend:

- Compare text completeness.
- Validate structure preservation.
- Validate asset references exist.
- Validate JSON schema if produced.
- Validate chunk provenance.
- Track runtime and memory.

**Success criteria**

- The default backend for each file type is chosen by measured quality, not by assumption.
- Existing native converters remain fallback, not the main long-term high-quality path.
- A source can never claim a structured output format unless the chosen backend supports it.

---

## F-006 — Audio provider layer exists, but only local faster-whisper actually ships

**Severity:** P1  
**Category:** Audio / provider integration / product truthfulness  
**Status:** Confirmed from code

### What is happening

`backend/app/audio/providers/capabilities.py` declares provider capabilities for:

- `local_faster_whisper`
- `local_whisperx`
- `openai`
- `groq`
- `deepgram`
- `assemblyai`
- `azure`
- `custom_openai_compatible`

But `backend/app/audio/providers/registry.py` has `_DEFERRED_PROVIDERS` for all of those except `local_faster_whisper`, and `_local_factories()` only returns `local_faster_whisper`. `build_provider()` raises `NotImplementedError` for deferred providers (`registry.py:36-49`, `57-104`).

The frontend filters `capabilities.filter((c) => c.available !== false)` for the provider dropdown, so most cloud options are not selectable in the main provider dropdown (`frontend/src/components/features/audio/AudioAdvancedSettings.tsx:82-88`). However, active provider settings and config blobs can still store unsupported providers, and enabling cloud STT will then fail at runtime.

### Best solution

Implement real provider adapters in a measured order, using provider APIs rather than building custom STT.

#### Provider order

1. **OpenAI adapter**
   - Current docs list `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, and `gpt-4o-transcribe-diarize`; diarized output supports `diarized_json` with speaker/start/end metadata and requires `chunking_strategy` for longer audio.
   - Good for high-quality cloud transcription and speaker labels.
2. **Groq adapter**
   - OpenAI-compatible endpoints, `whisper-large-v3` and `whisper-large-v3-turbo`, `verbose_json`, segment/word timestamp granularities, prompt support, documented file limits.
   - Good for fast/low-cost cloud Whisper path.
3. **Azure fast transcription adapter**
   - Supports diarization, channels, segment/word timestamps, phrase list/custom prompting depending on mode, and large file constraints.
   - Good for enterprise users already on Azure.
4. **Deepgram adapter**
   - Strong production STT features, smart formatting, diarization support in its API ecosystem, good for call/meeting transcription.
5. **WhisperX/pyannote optional local diarization**
   - Use only as an optional local route because it adds heavier dependencies and model/license requirements.

#### Adapter design

```python
class AudioTranscriptionProvider(Protocol):
    id: str
    def transcribe(
        self,
        filepath: str,
        config: dict[str, Any],
        *,
        device: str | None = None,
        vocabulary_prompt: str | None = None,
    ) -> RawTranscript: ...
```

Keep this interface. The missing piece is provider-specific config resolution:

- `build_provider(provider_id, provider_record=None, secret_resolver=None)`
- `provider_record` includes API key, base URL, region, deployment, timeout, retries, concurrency.
- The converter should load the active provider record and pass it to the adapter.
- Unknown provider IDs must error clearly; do not silently fall back to local.

### Integration plan

1. Add `AudioProviderConfig` loader from settings.
2. Implement adapters:
   - `openai_stt.py`
   - `groq_stt.py`
   - `azure_stt.py`
   - `deepgram_stt.py`
3. Convert provider responses into `RawTranscript` with normalized fields:
   - language
   - duration
   - segments start/end/text/speaker/confidence/words
   - provider metadata
   - warnings
4. Add cloud audit events when a cloud provider is used, not only when UI toggles are set.
5. Add provider-level feature gating:
   - prompt support
   - diarization support
   - word timestamps
   - confidence support
   - max file size
   - URL upload support
6. Add explicit fallback policy:
   - `audio_local_fallback_on_cloud_failure=false` by default.
   - If enabled, metadata must say cloud failed and local fallback ran.

### Testing path

**Unit tests with mocked HTTP**

- OpenAI normal JSON.
- OpenAI diarized JSON.
- Groq verbose JSON with segment and word timestamps.
- Azure diarized phrase response.
- Deepgram response with smart formatting and speaker labels.
- HTTP 401/429/5xx handling.
- Timeout/retry behavior.
- Cloud opt-in enforcement.
- Unknown provider rejection.

**Fixture tests**

- 5-second mono audio.
- 45-second audio requiring chunking/diarization behavior.
- Multi-speaker meeting.
- No speech/silence.
- Noisy low-confidence sample.

**Success criteria**

- Selecting a provider either works or is disabled with a reason.
- No cloud audio leaves the machine without explicit opt-in.
- Metadata always records provider, model, cloud/local, timestamps, speaker behavior, and warning list.
- Provider features are not shown as available unless the adapter supports them.

---

## F-007 — Audio enhancement, correction, fusion, contradiction, and benchmark controls are mostly UI/metadata, not implemented features

**Severity:** P1  
**Category:** Audio product quality / UX truthfulness  
**Status:** Confirmed from code plus strategic gap

### What is happening

The frontend exposes advanced audio controls for:

- text enhancement strength,
- structural enhancement mode,
- contradiction detection,
- context/fusion,
- cloud enhancement,
- provider benchmark comparison.

Evidence: `frontend/src/components/features/audio/AudioAdvancedSettings.tsx:106-258`.

Backend audio conversion mostly does:

- provider transcription,
- normalization,
- optional output mode selection,
- deterministic render through `render_transcript_markdown` or `render_enhanced_markdown`.

`render_enhanced_markdown` is template-labeled but does not implement truly different meeting/lecture/interview/action structures. It builds extractive summary, key points, actions, questions, evidence notes, source map, diagnostics, and original transcript regardless of template (`backend/app/audio/pipeline.py:219-271`).

`audio_text_enhancement_strength`, `audio_structural_enhancement_mode`, `audio_fusion_mode`, `audio_contradiction_detection`, and `audio_benchmark_compare` are not implemented as real processing paths.

### Best solution

Split audio features into three honest tiers.

#### Tier A — Transcript core

Always available:

- timestamped segments,
- speaker labels if provider supports them,
- word timestamps if provider supports them,
- confidence/warning metrics,
- vocabulary diagnostics,
- source map.

#### Tier B — Deterministic extractive notes

No LLM, no rewrite:

- extractive summary,
- action/question heuristics,
- speaker sections,
- timeline,
- original transcript appendices.

This can remain custom because it is simple, private, and provenance-preserving. But it must be described as “extractive deterministic notes,” not “corrected transcript.”

#### Tier C — LLM enhancement/correction

Only when explicitly enabled:

- local LLM provider if configured,
- cloud LLM provider only if `audio_enhancement_allow_cloud=true`,
- strict evidence validator,
- diff output between raw and enhanced text,
- source refs required for every nontrivial claim,
- fallback to Tier B if validation fails.

### Integration plan

1. Add `AudioDocument` internal schema:
   - raw transcript,
   - normalized transcript,
   - enhancement blocks,
   - validation report,
   - source map.
2. Add `AudioRenderer` strategies:
   - `raw_transcript`
   - `speaker_timeline`
   - `meeting_notes_extractive`
   - `lecture_notes_extractive`
   - `interview_qna_extractive`
   - `action_decision_log_extractive`
   - `llm_corrected_transcript`
   - `llm_polished_minutes`
3. Add `EvidenceValidator`:
   - every generated bullet must include at least one source segment id,
   - generated text cannot introduce named entities not present in transcript/context unless explicitly marked from context,
   - contradictions are surfaced, not resolved silently.
4. Implement benchmark only after at least two provider adapters exist.

### Testing path

- Each output mode has a golden snapshot fixture.
- Enhancement disabled means no text rewrite.
- Structural-only mode preserves original words.
- Cloud enhancement fails unless explicitly allowed.
- LLM output without source refs fails validation and falls back.
- Contradiction fixture with “budget is approved” and “budget is not approved” produces a contradiction section with segment refs.
- Benchmark fixture runs two mocked providers and reports latency, segment count, vocabulary hits, confidence availability, and cost estimate.

**Success criteria**

- Every visible audio control changes output or is disabled/hidden.
- “Corrected”/“enhanced” claims are validated and auditable.
- Raw transcript is always preserved.

---

## F-008 — Frontend Markdown preview auto-loads remote images

**Severity:** P0/P1 depending on threat model  
**Category:** Privacy / frontend security  
**Status:** Confirmed from code

### What is happening

`OutputViewer.tsx` renders Markdown through `ReactMarkdown`. Its custom `img` renderer returns:

```tsx
<img src={src} alt={alt} {...props} />
```

Evidence: `frontend/src/components/features/OutputViewer.tsx:218-246`.

If conversion output contains:

```markdown
![tracking](https://example.com/pixel?doc=...)
```

then the browser requests that URL during preview. This leaks IP/user-agent/timing and may leak sensitive converted filenames or query-bearing URLs.

### Best solution

Implement an **asset policy renderer**.

Rules:

1. Do not auto-load `http://` or `https://` images in Markdown preview by default.
2. Allow only:
   - persisted local asset URLs generated by the backend,
   - `blob:` URLs created from downloaded local assets,
   - optionally `data:` only when size/type is safe and configured.
3. Replace external images with a blocked placeholder:

> External image blocked for privacy. Click to load once.

4. Add a user setting:
   - “Auto-load external images in converted Markdown” default false.
5. Backend should rewrite persisted asset references into safe `/api/convert/assets/{job_id}/{asset_id}` URLs with authorization/scope checks.

### Integration plan

- Add `SafeMarkdownImage` component.
- Add `isSafeImageSrc(src, allowedAssetBase)` helper.
- Add backend asset proxy with content-type allow-list and no redirects.
- Add CSP: `img-src 'self' blob: data:` by default; no external wildcard.

### Testing path

- Render Markdown with external HTTP image. Assert no network request is made and placeholder appears.
- Render Markdown with local asset. Assert image displays.
- Click “load once” and assert only that specific URL loads.
- HTML/Markdown injection tests with `javascript:` and unusual URL encodings.

**Success criteria**

- No external image URL is fetched by default.
- Users still get a good preview for local extracted assets.

---

## F-009 — REST cancel/delete lifecycle is destructive and inconsistent with MCP

**Severity:** P1  
**Category:** Job lifecycle / enterprise UX  
**Status:** Confirmed from code

### What is happening

REST:

- `DELETE /api/convert/{job_id}` cancels if running, deletes upload/result files, and deletes the DB row (`backend/app/routes/convert.py:265-300`).

MCP:

- `marker_cancel_job` calls `delete_job(job_id, delete_files=False)` and returns cancelled (`backend/app/mcp_server.py:98-115` in fetched range).
- `agent_api.delete_job` deletes the DB record regardless of `delete_files` (`backend/app/agent_api.py:224-238`).

So “cancel” does not mean “mark job cancelled and preserve history.” It means “remove DB row, optionally keep files.”

### Best solution

Create separate lifecycle operations:

1. `POST /api/convert/{job_id}/cancel`
   - Running/pending jobs: mark `cancel_requested`, kill/interrupt worker if possible, final DB status `cancelled`.
   - Completed/failed jobs: return 409 or no-op depending on policy.
   - Preserve job record and files.
2. `DELETE /api/convert/{job_id}`
   - Delete history row and files if requested.
   - Cannot be confused with cancellation.
3. `POST /api/convert/{job_id}/retry`
   - Requeue failed/cancelled job with same source/config if source exists.
4. `POST /api/convert/{job_id}/archive`
   - Hide from normal history but keep files/metadata.

### Integration plan

- Add DB fields if needed: `cancel_requested_at`, `cancelled_at`, `deleted_at`, `delete_files`.
- Update task manager to poll/check cancellation before finalization.
- MCP tools:
  - `marker_cancel_job`: cancel only.
  - `marker_delete_job`: delete record/files.
  - `marker_archive_job`: optional.
- Frontend buttons:
  - “Cancel” while pending/processing.
  - “Delete” in history.
  - “Remove files” secondary option.

### Testing path

- Cancel pending job: DB row remains, status `cancelled`, source/result preserved.
- Cancel running thread job: future cancellation or status update works; finalization does not mark completed.
- Delete completed job: DB row and files removed when requested.
- MCP cancel does not remove DB row.
- REST and MCP lifecycle results match.

**Success criteria**

- Cancel and delete are semantically separate everywhere.
- Job history remains audit-friendly.

---

## F-010 — Format support and option schemas are duplicated across REST, CLI, MCP, frontend, and docs

**Severity:** P1  
**Category:** Architecture / maintainability  
**Status:** Confirmed from code

### What is happening

The same values appear repeatedly:

- Output formats: `format_store.py`, `marker_service.py`, `routes/convert.py`, `agent_contract.py`, `mcp_server.py`, `frontend/src/lib/api.ts`, `ConversionOptions.tsx`.
- Audio output modes: backend accepts `interview_qna` and `action_decision_log` in REST, while `agent_contract.py` still limits `AudioOutputMode` to `transcript`, `enhanced`, `notes`, `meeting_notes`, `lecture_notes` at line 25. `agent_api.build_conversion_config` also only accepts those older modes at lines 173–174.
- OCR engines, image handling modes, conversion profiles, archive options, and advanced audio fields are similarly spread out.

### Best solution

Create a **single schema registry** that generates or serves all surfaces.

#### Minimal backend structure

```python
class OptionSpec(BaseModel):
    name: str
    type: str
    default: Any
    enum: list[str] | None = None
    applies_to: list[str]
    surfaces: list[str]  # rest, cli, mcp, ui
    privacy: str | None
    requires_cloud_opt_in: bool = False
    capability_gate: str | None = None
```

Then:

- REST validates from registry.
- CLI/MCP parameter docs are generated from registry or use registry-derived Pydantic models.
- Frontend fetches `/api/capabilities/schema` and uses it to render options.
- Docs are generated from the same registry.

### Testing path

- Snapshot test registry JSON.
- Frontend type generation test: TypeScript enum values match backend JSON.
- Contract test: REST accepted modes == agent contract modes == MCP modes.
- Test that adding a new audio mode in one place fails CI unless registry is updated.

**Success criteria**

- No feature option is hand-maintained in more than one canonical place.
- UI never exposes a mode the backend drops.

---

## F-011 — Output writer and downloader disagree on extension/content semantics

**Severity:** P1  
**Category:** Output contract  
**Status:** Confirmed from code

### What is happening

`output_writer._extension_from_result` maps `chunks` to `json` when no result extension is present (`backend/app/services/output_writer.py:137-146`).

REST download maps `chunks` to `txt` (`backend/app/routes/convert.py:122-127` in the fetched download section).

This means output path, download extension, and content semantics can diverge.

### Best solution

Use the same `FormatCapability` registry for:

- storage extension,
- download extension,
- content-type/media-type,
- UI tab label,
- MCP schema.

Recommended extensions:

- `markdown` -> `.md`, `text/markdown`
- `html` -> `.html`, `text/html`
- `json` -> `.json`, `application/json`
- `chunks` -> `.chunks.json` or `.chunks.jsonl`, `application/json` or `application/x-ndjson`
- `text` -> `.txt`, `text/plain`

### Testing path

- Snapshot `FormatRegistry` extension/media mappings.
- Downloaded filename extension matches output writer extension.
- Content parses as declared media type.

**Success criteria**

- Same format always means same extension/media type everywhere.

---

## F-012 — Audio/video/ZIP metadata can become very large in DB rows

**Severity:** P1/P2  
**Category:** Storage / performance  
**Status:** Highly likely from code

### What is happening

Task finalization stores audio transcript metadata and video metadata directly in `result_metadata_json` (`backend/app/services/task_manager.py:89-95`). For long audio/video, segment arrays and word timestamps can become large. Audio converter metadata includes `audio.transcript` with all segments and possibly words (`backend/app/conversion/converters/audio.py:143-162`). Video metadata can include transcript and frame analyses (`video.py:110-120`).

SQLite rows with large JSON blobs will make history/status slower and hard to paginate. It also makes metadata truncation/policy harder.

### Best solution

Split heavy artifacts out of DB rows.

- DB row: small summary only.
- Output manifest: references artifact files.
- Artifact files:
  - `audio.transcript.json`
  - `audio.words.jsonl`
  - `video.timeline.json`
  - `chunks.jsonl`
  - `assets/index.json`

### Testing path

- Convert long mocked transcript with 10k word timestamps.
- Status response remains under size budget, e.g. `<200KB`.
- Artifact file exists and validates schema.
- UI lazy-loads audio inspection data only when Audio tab opens.

**Success criteria**

- History/status remain fast for long media.
- Full metadata remains available via manifest/artifacts.

---

## F-013 — Source URL SSRF protection is good but still has DNS TOCTOU risk

**Severity:** P1 for hostile/multiuser deployments  
**Category:** Security  
**Status:** Confirmed design risk from code

### What is happening

`safe_url_fetcher.assert_safe_source_url` resolves the hostname with `socket.getaddrinfo`, rejects private/local/link-local/reserved addresses, and validates each redirect before making the next request. This is a good baseline (`backend/app/services/safe_url_fetcher.py:157-179`).

But `download_source_url` then calls `httpx.AsyncClient.stream("GET", current_url)` (`safe_url_fetcher.py:97-153`). `httpx` performs its own DNS resolution when connecting. A malicious domain can resolve to a public IP during validation and a private IP during the actual request (DNS rebinding/TOCTOU). MCP security guidance explicitly calls out DNS resolution considerations and warns about TOCTOU issues.

### Best solution

For local single-user mode, current protection is acceptable with clear warnings. For production/enterprise mode:

1. Prefer an egress proxy/network policy to block private ranges at the network layer.
2. Add optional strict downloader mode:
   - resolve host,
   - pin IP,
   - connect to pinned IP with original `Host` header/SNI if library supports it safely,
   - or use a hardened SSRF guard library/proxy.
3. Always enforce redirect validation.
4. Add allowlist mode for source URLs in enterprise deployments.

### Testing path

- Private IP direct URL is blocked.
- Redirect to private IP is blocked.
- URL with credentials is blocked.
- Malformed/octal/IPv6-mapped addresses are blocked.
- DNS rebinding simulated test is added if strict mode is implemented.

**Success criteria**

- Public default is safe enough for local use.
- Enterprise mode has defense-in-depth beyond application-layer DNS checks.

---

## F-014 — ZIP conversion needs stronger archive-bomb and output-asset behavior

**Severity:** P1/P2  
**Category:** Security / reliability / archive quality  
**Status:** Confirmed design gap

### What is happening

The archive converter:

- scans up to `archive_max_files`,
- checks suspicious paths,
- checks child file size,
- reads each child into memory before conversion,
- recursively converts deterministic children,
- skips Marker/PDF/image engines.

This is thoughtful for a first version. Remaining gaps:

- No global uncompressed byte budget across all children.
- No compression ratio guard.
- Child assets from converted children are not persisted/namespaced into archive output assets; only child Markdown text is inlined.
- Nested ZIPs can recurse up to depth but still require careful total work accounting.

### Best solution

Add `ArchiveBudget`:

```python
@dataclass
class ArchiveBudget:
    max_files: int
    max_total_uncompressed_bytes: int
    max_child_bytes: int
    max_depth: int
    max_converted_children: int
    max_compression_ratio: float
```

Also namespace child outputs:

- `children/{safe_path}/output.md`
- `children/{safe_path}/assets/...`
- manifest includes every child action, engine, warning, output path, and skipped reason.

### Testing path

- Zip-slip filenames skipped.
- Many small files beyond file count cap skipped.
- One huge child skipped.
- High compression ratio zip skipped.
- Nested zip obeys depth and total budget.
- Child images/assets are preserved and referenced correctly.

**Success criteria**

- Archive conversion cannot explode memory/disk beyond configured budgets.
- Converted child assets are not silently lost.

---

## F-015 — Video conversion is a prototype, not a production multimodal pipeline

**Severity:** P2 currently, P1 if advertised strongly  
**Category:** Video / multimodal quality  
**Status:** Confirmed from code plus strategic gap

### What is happening

`VideoConverter`:

- requires ffmpeg/ffprobe,
- probes video,
- extracts audio to WAV and transcribes through local audio path,
- extracts frames at fixed interval,
- computes mean RGB/brightness/dominant color,
- optionally runs Tesseract OCR,
- returns Markdown timeline.

Evidence: `backend/app/conversion/converters/video.py:41-122`, `190-247`, `269-328`.

This is a good prototype but not a serious multimodal understanding pipeline.

### Best solution

Use existing tools for video primitives:

- ffmpeg/ffprobe for demuxing and media metadata.
- PySceneDetect for scene/keyframe detection instead of fixed interval frame sampling.
- AudioProvider adapters for transcription.
- Existing image understanding processor/VLM for selected keyframes only, with privacy opt-in.
- Output keyframes as assets, not only text metadata.

### Integration plan

1. Add `VideoBudget`: max duration, max scenes, max frames, max VLM frames.
2. Extract scenes/keyframes using PySceneDetect or ffmpeg scene detection.
3. Persist keyframes as assets with timestamps.
4. Run local OCR on keyframes.
5. Optionally run VLM on keyframes if cloud/local VLM allowed.
6. Merge audio transcript and frame observations into a unified timeline with provenance.

### Testing path

- Silent video -> no audio, keyframes extracted.
- Audio-only-like video -> transcript still works.
- Slide recording -> OCR text from slides appears with frame timestamp.
- Scene-change video -> keyframes align with scenes, not fixed arbitrary intervals.
- Cloud disabled -> no VLM calls.

**Success criteria**

- Video output contains timeline, transcript, keyframe assets, OCR/VLM provenance, and safe warnings.
- Fixed brightness-only summaries are not treated as visual understanding.

---

## F-016 — MCP surface is promising but not enterprise-complete

**Severity:** P1/P2  
**Category:** Agent/CLI/MCP  
**Status:** Confirmed from code plus strategic gap

### What is working

The MCP server has a large tool surface, annotations, resources, prompts, token/scopes, and local root scoping. This is a real foundation.

### Gaps

1. `marker_read_output_chunk` is not semantic chunking.
2. Cancel/delete semantics are not clean.
3. Tool names/options are not fully generated from one registry.
4. Agent contract lags REST on audio modes.
5. Resource access is path-based and manifest-aware, but enterprise deployments need stronger per-client scoping and resource lifecycle semantics.
6. MCP HTTP mode security needs systematic compliance with MCP best practices: session safety, token audience, scope minimization, SSRF controls, and local server risk controls.

MCP security docs emphasize scope minimization, SSRF, session hijacking, token passthrough risks, and local MCP server compromise. Marker UI has some pieces of this, but the enterprise story should be explicit and tested.

### Best solution

Build an **Agent Surface Registry** from the same schema registry:

```json
{
  "tools": [...],
  "resources": [...],
  "prompts": [...],
  "schemas": {...},
  "scopes": {...},
  "output_formats": {...},
  "lifecycle": {...}
}
```

Then generate:

- MCP tool schemas,
- CLI help/options,
- REST OpenAPI extras,
- docs tables,
- frontend option metadata.

### Testing path

- MCP self-test compares registered tools/resources/prompts against registry, not hand-coded arrays.
- Scope tests for every read/write/destructive tool.
- Cancel preserves job record; delete deletes.
- Resource read blocked outside output roots/manifest.
- MCP HTTP without auth only on loopback; non-loopback requires token.

**Success criteria**

- Agent clients see the same capabilities as GUI/REST.
- No duplicated option list drifts silently.
- Lifecycle operations are audit-safe.

---

## 4. Best possible target architecture

### 4.1 North-star architecture

```text
Input source
  -> SourcePolicy + SourceResolver
  -> ProbeService
  -> CapabilityRegistry / PlanService
  -> ConverterBackend
       - MarkerBackend
       - DoclingBackend
       - MarkItDownBackend
       - PandocBackend
       - NativeFallbackBackend
       - AudioBackend
       - VideoBackend
  -> ConversionArtifact
       - primary_text
       - structured_document_json?
       - html?
       - chunks?
       - assets
       - metadata
       - warnings
       - provenance
  -> OutputWriter + Manifest
  -> Surfaces
       - REST
       - GUI
       - CLI
       - MCP
```

### 4.2 ConversionArtifact schema

```python
@dataclass
class ConversionArtifact:
    source: SourceRef
    engine: EngineRef
    primary_format: str
    text: str
    renderings: dict[str, Rendering]
    assets: list[Asset]
    chunks: list[Chunk] | None
    metadata: dict[str, Any]
    warnings: list[Warning]
    provenance: list[SourceSpan]
```

`renderings` must be honest: only include formats that were actually rendered.

### 4.3 Capability registry as the anti-drift mechanism

Every surface consumes the same registry:

- REST validates requests.
- Plan endpoint returns availability.
- UI renders enabled/disabled controls.
- CLI/MCP docs reflect the same options.
- Tests snapshot it.

### 4.4 External tool selection matrix

| Area | Preferred external tool | Why | Custom Marker UI work |
|---|---|---|---|
| PDF/image OCR | Marker | Already core; layout/OCR/table models | routing, privacy, VLM image enrichment, output contract |
| Structured multi-format documents | Docling | Unified document model, supported formats, native chunkers | adapter, policy, quality scoring, UX |
| Broad Markdown fallback | MarkItDown | Lightweight, LLM-oriented Markdown conversion, broad support | wrapper, provenance, asset policy, fallback scoring |
| DOCX/HTML/EPUB transformations | Pandoc | Mature AST and media extraction | safe subprocess wrapper, validation, asset manifests |
| XLSX/XLS tables | openpyxl/xlrd now; evaluate Docling/MarkItDown | Simple and local for basic sheets | sidecars, formulas/merged cells metadata, truncation policy |
| Video scene detection | PySceneDetect + ffmpeg | Existing scene detection/keyframe primitives | timeline merger, privacy, VLM/OCR selection |
| Local STT | faster-whisper now; WhisperX optional | Existing local ASR; WhisperX adds word timestamps/diarization | provider adapter, capability gating, model install UX |
| Cloud STT | OpenAI/Groq/Deepgram/Azure APIs | Provider-native quality/features | normalized transcript schema, cloud opt-in, audit |
| RAG chunking | Docling HybridChunker; Markdown parser fallback | Avoid custom semantic chunking | chunk schema, source refs, eval harness |

---

## 5. Product-level roadmap

### Phase 0 — Stop wrong outputs and unsafe fallbacks

**Goal:** Make the system honest and safe before adding more features.

Tasks:

1. Add capability/format registry.
2. Restrict native converters to Markdown until a real structured backend exists.
3. Fix output writer/download extension mapping.
4. Remove generic runtime fallback to Marker PDF.
5. Fix engine override resource-requirement bug.
6. Block remote Markdown images in preview.
7. Split REST cancel/delete.

Acceptance:

- A native DOCX cannot produce fake JSON.
- A corrupted DOCX does not fallback to Marker PDF.
- External images do not auto-load.
- Cancel preserves job row.

### Phase 1 — Introduce structured conversion backends

**Goal:** Add serious non-PDF conversion without over-customizing.

Tasks:

1. Add Docling backend behind feature flag.
2. Add MarkItDown backend for Markdown fallback.
3. Add Pandoc wrapper for DOCX/HTML/EPUB where useful.
4. Run benchmark matrix against current native converters.
5. Promote best backend per format based on tests.

Acceptance:

- DOCX/PPTX/XLSX can produce truthful structured JSON/chunks when routed to Docling.
- Markdown-only fallback remains available.
- Backend choice is visible in metadata.

### Phase 2 — Real semantic chunks

**Goal:** Make `chunks` a real RAG output.

Tasks:

1. Implement Docling chunk provider.
2. Implement Markdown structural chunk provider.
3. Add `marker.chunks.v1` schema.
4. Add chunk artifact persistence and MCP chunk tool.
5. Add retrieval-quality smoke tests.

Acceptance:

- Chunks are valid JSON/JSONL with source refs.
- Tables/code are preserved atomically or split safely.
- MCP chunk tool returns chunk ids, not character offsets.

### Phase 3 — Audio provider and enhancement maturity

**Goal:** Make the advanced audio UI real.

Tasks:

1. Implement OpenAI and Groq adapters first.
2. Add Azure and Deepgram after adapter harness is stable.
3. Add optional WhisperX/pyannote local diarization route.
4. Split deterministic extractive notes from LLM enhancement.
5. Add evidence validator and contradiction report.
6. Add benchmark only after at least two providers exist.

Acceptance:

- Cloud providers work only with explicit opt-in.
- Provider capabilities control UI and backend validation.
- Enhancement controls actually change output and preserve raw transcript.

### Phase 4 — MCP/CLI enterprise hardening

**Goal:** Make agent usage production-grade.

Tasks:

1. Generate MCP/CLI schemas from registry.
2. Implement clean lifecycle tools.
3. Harden resource access and audit trails.
4. Add profile support and scoped resource enforcement.
5. Expand self-test into conformance test.

Acceptance:

- MCP self-test is registry-driven.
- Every destructive operation requires write scope and is audited.
- Agent docs match actual capabilities.

### Phase 5 — Video and archive quality

**Goal:** Move video/archive from prototype to reliable multimodal/compound document outputs.

Tasks:

1. Add PySceneDetect keyframe extraction.
2. Persist frame assets.
3. Add archive global budgets and child asset manifests.
4. Merge audio/video/frame provenance.

Acceptance:

- Video timeline has real keyframes/assets/provenance.
- ZIP cannot exceed configured work/disk budgets.
- Child outputs are traceable.

---

## 6. Comprehensive testing strategy

### 6.1 Test pyramid

1. **Unit tests** for registries, route planning, adapters, renderers, output writer, URL policy.
2. **Integration tests** for REST upload/status/download/regenerate/cancel/delete.
3. **MCP/CLI contract tests** for schemas, scopes, resources, lifecycle.
4. **Golden fixture tests** for conversion quality.
5. **Security tests** for SSRF, path traversal, Markdown image loading, archive bombs, root policies.
6. **Performance tests** for large docs/media and output size limits.
7. **Differential backend tests** comparing Marker/Docling/MarkItDown/Pandoc/current native output.

### 6.2 Golden fixture matrix

| Fixture | Required assertions |
|---|---|
| Clean text PDF | LiteParse/Marker routing correct; Markdown complete; JSON valid if requested |
| Scanned PDF | Marker OCR route; images/assets; warnings if OCR uncertain |
| Mixed PDF | full-page probe required; segment order preserved; fallback visible |
| DOCX complex | headings/tables/images preserved; unsupported formats rejected or Docling route used |
| PPTX complex | slide order/notes/tables/images preserved; chart handling visible |
| XLSX formulas/merged cells | sheet metadata, truncation warnings, CSV sidecars if added |
| HTML with remote images | preview blocks external images; conversion does not auto-fetch unsafe resources |
| JSON/JSONL | invalid JSON fails clearly; valid JSON conversion remains truthful |
| Audio mono | local faster-whisper mocked; transcript schema valid |
| Audio multi-speaker | diarization adapter output normalized; raw transcript preserved |
| Video slide recording | keyframes/assets; OCR/VLM provenance; transcript alignment |
| ZIP nested | budget enforcement; child manifest; suspicious path skip |

### 6.3 Success gates before release

A release should not be considered production-grade until:

- `pytest` backend passes with new format/adapter/security tests.
- `vitest` frontend passes with privacy and capability UI tests.
- Every public output format has a schema/content-type test.
- Every advertised provider has a live adapter or is hidden/disabled.
- Every advanced control is either implemented, disabled, or explicitly labeled experimental/no-op.
- The docs are generated from the same capability registry or are tested against it.

---

## 7. Definition of “done” per major solution

### Format registry done

- One backend registry file owns output formats and media types.
- REST, CLI, MCP, and UI use registry data.
- Unsupported format combinations are rejected before conversion.
- Tests fail if any surface drifts.

### Chunking done

- `chunks` is JSON/JSONL with `marker.chunks.v1`.
- Chunks include source refs and heading/context metadata.
- Tables/code blocks are safely preserved.
- MCP can read chunks by id/index.

### Native conversion done

- Docling/MarkItDown/Pandoc backends are evaluated against fixtures.
- Default backend is benchmark-selected per format.
- Legacy custom converters are fallback, not the only strategy.
- Output truthfulness is enforced.

### Audio providers done

- Provider records include secrets/configs.
- At least OpenAI and Groq adapters pass mocked and fixture tests.
- Cloud opt-in is enforced and audited.
- Unknown providers fail clearly.
- UI shows only available provider features.

### Audio enhancement done

- Raw transcript always preserved.
- Deterministic notes are honest and source-backed.
- LLM enhancement has source-reference validator and fallback.
- Contradiction detection has explicit source refs.

### Frontend privacy done

- External images are blocked by default.
- Local assets still render nicely.
- CSP matches policy.
- Tests verify no unwanted network requests.

### MCP enterprise done

- Cancel/delete are separate.
- Scopes are enforced for every resource/tool.
- Tool schemas are generated from registry.
- Self-test validates tools/resources/prompts/schemas/scopes.

---

## 8. Immediate issue backlog

### P0

1. Stop fake JSON/HTML/chunks for Markdown-only converters.
2. Remove generic runtime fallback to Marker PDF.
3. Fix engine override/resource plan bug.
4. Block external Markdown images in OutputViewer.
5. Split REST cancel from delete.
6. Use one format extension/media-type registry.

### P1

1. Add Docling backend experiment and benchmark fixtures.
2. Add MarkItDown backend for Markdown fallback.
3. Implement semantic chunk schema and Markdown structural chunker.
4. Implement OpenAI and Groq audio adapters.
5. Make audio advanced controls honest/functional.
6. Move large metadata to artifact files.
7. Add archive budget controls.
8. Generate frontend/MCP/CLI option schemas from registry.

### P2

1. Add Azure/Deepgram adapters.
2. Add optional WhisperX/pyannote local diarization route.
3. Add PySceneDetect-based video pipeline.
4. Add full differential conversion benchmark dashboard.
5. Add enterprise profiles and stronger MCP resource policies.

---

## 9. Final recommendation

The project should not try to become a fully custom replacement for Marker, Docling, MarkItDown, Pandoc, WhisperX, pyannote, or cloud STT APIs. That would create too many breaking points. Marker UI should become the **orchestrator and quality layer** above these tools:

- choose the best backend per file and requested output,
- enforce privacy and local/cloud policy,
- normalize output into truthful artifacts,
- preserve provenance,
- expose clear GUI/CLI/MCP workflows,
- test conversion quality continuously.

The most important shift is to stop thinking of “conversion” as one text string. Treat every conversion as a typed artifact with renderings, assets, chunks, metadata, and provenance. Once that foundation exists, the surrounding features — chunking, MCP resources, audio notes, downloads, previews, audits, and benchmarks — become simpler and more reliable.

---

## Appendix A — Code evidence index

- `backend/app/conversion/router.py:245-264` — engine override logic can mix marker engine with native metadata/resource requirements.
- `backend/app/conversion/registry.py:76-115` — registry selects by engine name and does not enforce `accepts(...)` at lookup.
- `backend/app/services/conversion_service.py:322-350` — generic runtime fallback from any non-marker engine to marker_pdf.
- `backend/app/conversion/converters/marker_pdf.py:37-52` — MarkerPdfConverter supports only PDF/images/EPUB.
- `backend/app/conversion/converters/text_data.py:95-123` — text/CSV/JSON/JSONL converter returns Markdown extension.
- `backend/app/conversion/converters/html.py:50-54` — HTML converter returns Markdown extension.
- `backend/app/conversion/converters/spreadsheet.py:71-75`, `112-116` — spreadsheet converter returns Markdown extension.
- `backend/app/conversion/converters/office_docx.py:182-187` — DOCX converter returns Markdown extension.
- `backend/app/conversion/converters/office_pptx.py:299-304` — PPTX converter returns Markdown extension.
- `backend/app/conversion/converters/archive.py:126-130` — archive converter returns Markdown extension.
- `backend/app/conversion/converters/audio.py:127-164` — audio converter returns Markdown and embeds transcript metadata.
- `backend/app/conversion/converters/video.py:95-122` — video converter returns Markdown and embeds transcript/frame metadata.
- `backend/app/services/format_store.py:27-30` — global format list includes markdown/json/html/chunks.
- `backend/app/services/task_manager.py:80-103` — finalizer cache can store primary text under requested primary format.
- `backend/app/services/output_writer.py:137-146` — output writer maps chunks to json.
- `backend/app/routes/convert.py:122-127` in download route — downloader maps chunks to txt.
- `frontend/src/components/features/OutputViewer.tsx:218-246` — Markdown image renderer directly emits `<img src={src}>`.
- `backend/app/audio/providers/registry.py:36-49`, `57-104` — only local faster-whisper adapter ships; cloud providers deferred.
- `backend/app/audio/providers/capabilities.py:177-227` — capability matrix advertises providers and availability flag.
- `backend/app/audio/pipeline.py:219-271` — enhanced audio render is deterministic/extractive and does not implement separate structural modes.
- `frontend/src/components/features/audio/AudioAdvancedSettings.tsx:106-258` — UI exposes advanced audio controls that backend largely does not honor.
- `backend/app/mcp_server.py:250-289` — `marker_read_output` and `marker_read_output_chunk` both call offset-based `read_output`.
- `backend/app/agent_api.py:119-169` — output chunk reading is character offset paging.
- `backend/app/routes/convert.py:265-300` — REST delete cancels and deletes in one operation.
- `backend/app/agent_api.py:224-238` — agent delete helper deletes DB row even when `delete_files=False`.
- `backend/app/services/safe_url_fetcher.py:157-179` — safe URL hostname/IP validation before fetch.

---

## Appendix B — External source notes

- **Docling supported formats:** Docling parses many formats into a unified `Docling Document` and exports HTML, Markdown, JSON, DocLang XML, plain text, Doctags, and WebVTT. Source: https://docling-project.github.io/docling/usage/supported_formats/
- **Docling chunking:** Docling native chunkers operate on `DoclingDocument`; `HybridChunker` refines hierarchical chunks by token limits and merges peer chunks with same headings/captions; table header repetition is supported. Source: https://docling-project.github.io/docling/concepts/chunking/
- **MarkItDown:** Microsoft describes it as a lightweight Python utility for converting many formats to Markdown for LLM/text-analysis pipelines, with broad support and a warning that it may not be best for high-fidelity human conversion. Source: https://github.com/microsoft/markitdown
- **Pandoc:** Pandoc supports many input/output formats and has `--extract-media` to extract images/media and rewrite references. Source: https://pandoc.org/MANUAL.html
- **Unstructured:** Useful reference point for document element/chunking concepts, but its own docs state the open-source library is a prototyping starting point and list production limitations. Source: https://docs.unstructured.io/open-source/introduction/overview
- **OpenAI STT:** Current docs list `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, and `gpt-4o-transcribe-diarize`, including diarized JSON with speaker/start/end metadata. Source: https://developers.openai.com/api/docs/guides/speech-to-text
- **Groq STT:** OpenAI-compatible transcription/translation endpoints, Whisper models, file limits, response formats, segment/word timestamp granularities, and metadata guidance. Source: https://console.groq.com/docs/speech-to-text
- **Deepgram STT:** Prerecorded audio API with `nova-3` and `smart_format` examples. Source: https://developers.deepgram.com/docs/pre-recorded-audio
- **Azure fast transcription:** Supports diarization, channels, segment/word timestamps, and documented file constraints/features. Source: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/fast-transcription-create
- **PySceneDetect:** Provides CLI and Python APIs for scene detection and frame/image outputs; suitable for replacing fixed-frame video sampling. Source: https://www.scenedetect.com/docs/latest/
- **MCP security best practices:** Highlights SSRF, token passthrough, scope minimization, session hijacking, and local MCP compromise risks. Source: https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices
