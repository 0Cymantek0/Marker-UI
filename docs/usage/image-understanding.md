# Image Understanding

Marker UI includes an advanced **Image Understanding** pipeline that uses Vision-Language Models (VLMs) to convert document visuals into accessible text formats.

---

## How It Works

Instead of leaving images as opaque file links (e.g. `![](image_0.jpg)`), the VLM pipeline:
1. **Classifies** each visual element (charts, tables, diagrams, equations, screenshots, photos, decorative items).
2. **Extracts** structured data tailored to the class:
   - **Charts (Bar/Line/Pie/Scatter)**: Converted to markdown data tables.
   - **Diagrams (Flow/Sequence/State/Class/Architecture)**: Converted to Mermaid.js code blocks.
   - **Equations**: Rendered as LaTeX blocks (`$$ ... $$`).
   - **Screenshots & Photos**: Transcribed as context-aware alt-text descriptions.
   - **Decorative items**: Omitted cleanly.

---

## Options

In the **Advanced Settings** modal on the Convert page, choose from three modes:

| Mode | Behavior |
|---|---|
| **Extraction only** | Default marker behavior. Extracts images to disk and links them in markdown. |
| **Understanding only** | Replaces image blocks with text tables, Mermaid, or alt-text. Images are not linked. |
| **Both** | Combines VLM descriptions with the original image linked as a reference comment. |

---

## Privacy Defaults

Cloud image analysis is off unless explicitly enabled for a job. With cloud access disabled, the router can still omit decorative visuals and use local OCR where available, but it will not send image bytes to a remote VLM provider. Configure local or cloud providers in [VLM Providers Configuration](../configuration/vlm-providers.md).

---

## Supported Document Paths

PDF image understanding runs inside Marker PDF's block pipeline, so charts, diagrams, screenshots, photos, equations, and decorative image blocks can be replaced or augmented before Markdown rendering. Native DOCX and PPTX conversion also process embedded images through the shared image-understanding service.

Fast deterministic converters for CSV, JSON/JSONL, HTML, XML/RSS/Atom, Jupyter notebooks, XLSX workbooks, and ZIP archives do not run the Marker PDF image-block processor. They convert text-like structure directly and keep conversion local.

---

## Error Handling

If a VLM call fails due to rate limits or API issues:
- The system **fails soft**.
- It falls back to standard extraction, preserving the image.
- A warning is logged, but conversion completes.
