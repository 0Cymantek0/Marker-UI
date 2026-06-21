# Known Limitations & Constraints

While Marker UI provides a robust interface for document conversion, there are several known engineering and architectural limitations. We document these here to help you set expectations and invite you to help us improve.

---

## Technical Constraints

### 1. Extracted Image Links & ZIP Files
- When documents contain images, the engine extracts them as separate files.
- To prevent broken image references, downloads are packaged as a `.zip` file containing both the Markdown file and the associated `images/` directory.
- Direct copy-pasting of Markdown text from the history dashboard does not automatically download or embed the image assets.

### 2. Visual Context Descriptions Require VLM
- Automatic generation of text descriptions, data tables, and Mermaid.js diagrams for visual assets requires enabling the Image Understanding VLM pipeline and configuring a vision-capable LLM.
- If disabled, or if using vision-less models, these elements default to raw image links with no contextual text.

### 3. RAM & VRAM Footprint
- Running the full neural pipeline (layout detection, OCR, and table analyzer) requires significant memory.
- Running conversions on CPU-only machines can be slow. Large documents might cause CUDA Out-of-Memory (OOM) errors on systems with limited GPU VRAM (less than 8 GB).

---

## Call to Action: Help Us Discover & Patch Bugs!

Because document structures vary wildly across academic papers, government reports, scanned books, and complex spreadsheets, we rely heavily on real-world test cases.

> [!IMPORTANT]
> **We need your feedback!**
> - If you encounter a document that crashes the engine, scrambles reading orders, or displays broken tables, please **report it immediately**.
> - Tell us exactly what problems you are facing, specify the document type, and share relevant console log traces.
> - We aim to investigate and patch bugs quickly.
> - Report issues or share sample documents via the GitHub Issues page.
