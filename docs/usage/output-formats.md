# Supported Output Formats

Marker UI supports several output formats, but availability depends on the
selected conversion route. Use `/api/capabilities` or `marker_capabilities` to
read per-input `output_formats` before enabling UI or agent choices.

---

## 1. Markdown (Default)
Generates standard GitHub Flavored Markdown (GFM).
- **Text & Flow**: Restructures multi-column layouts into standard top-to-bottom reading blocks.
- **Equations**: Re-formats inline and display math equations into standard LaTeX (e.g. `\( E=mc^2 \)`).
- **Tables**: Parses rows and columns into clean Markdown grid syntax.

---

## 2. JSON
Returns a structured JSON payload representing the document structure.
- Supported for Marker-backed PDF/image/EPUB routes today.
- Native deterministic routes reject JSON instead of returning mislabeled
  Markdown.
- Ideal for downstream LLM ingestion, agentic workflows, or custom database ingestion.
- Contains separate fields for document metadata, raw text, and tables.

---

## 3. HTML
Generates structured HTML5 markup with semantic layout tags.
- Supported for Marker-backed PDF/image/EPUB routes today.
- Native deterministic routes reject HTML instead of returning mislabeled
  Markdown.
- Keeps tables as `<table>` tags and equations formatted clearly.

---

## 4. Chunks
Segments the document text into smaller, overlapping chunks.
- Optimized for creating vector embeddings in Retrieval-Augmented Generation (RAG) applications.
- Native Markdown-only converters use the deterministic `marker.chunks.v1`
  semantic chunker. It preserves heading paths, line spans, chunk IDs,
  `previous_id` / `next_id` links, character counts, token estimates,
  source document SHA-256, per-chunk content hashes, and source references.
- Large prose blocks split on sentence and character boundaries with bounded
  overlap. Markdown tables split by rows and repeat the header. Fenced code
  blocks stay fenced, even when split.
- Agent, CLI, MCP, and REST callers can set `chunk_max_tokens` /
  `--chunk-max-tokens` to request tokenizer-backed budgets for prose chunks.
  Structural chunks such as tables and fenced code stay intact where possible.
  When `tiktoken` is available, each chunk reports both `token_count` and
  `token_count_source`; otherwise the schema falls back to `token_estimate`.
- Read semantic chunks by index through `marker_read_output_chunk` with
  `mode="semantic"` instead of loading the whole chunks JSON into agent
  context.

---

## Handling Extracted Images & Assets

When converting documents containing images, charts, or diagrams:
1. **Extraction**: The underlying engine extracts images and stores them as separate files on the server.
2. **Markdown Links**: The generated Markdown links these files using relative paths (e.g. `![Image 1](images/page_1_img_1.png)`).
3. **ZIP Downloads**: To prevent broken image links, downloading a job with extracted images will package the Markdown file and the `images/` folder together into a single `.zip` archive.
4. **Visual Refinements**: If the optional Image Understanding VLM pipeline is enabled, visual elements (like charts and diagrams) are transcribed directly into Markdown tables or Mermaid.js diagrams instead of static image links.
