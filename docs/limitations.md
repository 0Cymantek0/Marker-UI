# Known Limitations & Maturity

Marker UI is local-first and intentionally conservative about what it claims.
This page documents current boundaries so users can choose the right route and
so contributors can see where help is useful.

## Feature Maturity Snapshot

| Area | Status |
| --- | --- |
| CLI, MCP, output manifests, and paged output reads | Working alpha |
| PDF/image conversion with Marker-backed Markdown, HTML, JSON, and chunks | Working alpha |
| Native Office/data/archive conversion to Markdown and chunks | Working alpha |
| Semantic chunking | Working alpha with deterministic source refs and lightweight retrieval gate; larger benchmark corpus still planned |
| Audio / voice notes | Partial alpha: local faster-whisper route only; cloud STT, real diarization, and provider comparison are deferred |
| Video | Experimental local demux, transcription, keyframe/OCR provenance |
| VLM image understanding | Opt-in alpha; cloud calls require explicit consent |
| Database migrations | Startup creates tables and additive column repairs; Alembic upgrades are developer-managed |

## Output Formats

- Markdown is the universal output format.
- JSON and HTML are true renderer outputs for Marker-backed PDF, image, and
  EPUB routes. Native deterministic converters do not pretend to render JSON or
  HTML.
- `chunks` is available for Marker-backed routes and for native Markdown output.
  Native chunks use Marker UI's deterministic `marker.chunks.v1` Markdown
  chunker with headings, line spans, source refs, and neighbor links. It is not
  yet a Docling/Unstructured document-IR chunker, and the chunks envelope marks
  that boundary with `renderer_kind="derived"`, `source_format="markdown"`,
  `semantic_level="markdown_structure"`, and `structured_ir=false`.
- Downloads choose the file extension from the actual response media type. Jobs
  with extracted sidecar assets are packaged as ZIP archives.

## Native Converters

- DOCX, PPTX, XLS/XLSX, MSG, CSV/TSV, JSON/JSONL, XML/RSS/Atom, HTML, notebooks,
  archives, audio, and video use local deterministic converters where possible.
- These routes are designed for robust Markdown/agent context, not pixel-perfect
  reproduction of every source file.
- PDF/image children inside ZIP archives are skipped rather than silently
  invoking Marker models from inside archive recursion.

## Archive Conversion

- ZIP conversion is bounded by file count, child size, total uncompressed byte
  budget, depth, converted-child count, and compression-ratio limits.
- Suspicious archive paths such as absolute paths or `..` traversal entries are
  skipped and recorded in the archive manifest.
- Child converter assets and images are preserved as namespaced output assets,
  but archive output is still a combined Markdown document, not a fully
  browsable virtual filesystem.

## Audio And Video

- Local faster-whisper is the only shipped STT provider. Cloud STT provider IDs
  are visible in the capability matrix as deferred and fail before a job is
  queued.
- Audio provider comparison is not shipped yet because it needs at least two
  implemented provider adapters plus a benchmark runner.
- Real speaker diarization is not shipped. Requests for diarization fail before
  provider execution or job queueing when the selected provider lacks that
  capability; no single-speaker result is presented as diarized output.
- Audio enhancement is deterministic and evidence-first. Enhanced output must
  retain source refs or fall back to the original transcript unless strict
  failure is requested.
- Video conversion is experimental. It demuxes/transcribes audio when possible,
  samples keyframes, and records frame/audio provenance; it is not full video
  understanding.

## Image Understanding

- Cloud image analysis is off unless the user explicitly enables cloud VLM
  access for a job.
- With cloud access disabled, image routing can still omit decorative visuals
  and use local OCR where available, but remote VLM extraction will not run.
- VLM extraction quality depends heavily on the selected provider/model and on
  document image quality.

## Security And Deployment

- REST and MCP auth support static bearer-token scopes. OIDC/JWT validation is
  intentionally rejected in this build until a verifier is implemented.
- Public URL conversion uses SSRF defenses and optional allowlists. Shared or
  production deployments should set `MARKER_SOURCE_URL_REQUIRE_ALLOWLIST=true`.
- API keys are encrypted at rest, but any machine that can run conversions can
  access local source files permitted by the configured workspace policy.

## Hardware

- Marker PDF/image routes load neural models and may need significant RAM/VRAM.
  CPU-only runs are supported but can be slow.
- Large scanned PDFs can still hit GPU memory limits on small cards. Use page
  ranges, fast routing, or CPU fallback when needed.

## How To Report Gaps

Document structures vary widely across academic papers, government reports,
scanned books, spreadsheets, slides, archives, and media files.

When reporting a problem, include:

- file type and route used;
- selected output format;
- relevant conversion settings;
- logs or error output;
- whether the issue is incorrect text, broken structure, missing assets,
  unsafe behavior, or performance.
