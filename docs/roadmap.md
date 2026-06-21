# Product Roadmap

This document outlines the planned evolutionary phases for Marker UI. We focus on improving the accuracy of document conversions, enhancing system flexibility, and integrating the platform into modern AI development workflows.

---

## Short-Term Roadmap

### 1. Model Context Protocol (MCP) Server Integration
- **Objective**: Develop and integrate a Model Context Protocol (MCP) server directly into Marker UI.
- **Why**: This will enable agentic LLM assistants (such as Claude Desktop, cursor, or custom AI harnesses) to interface directly with your local Marker UI instance.
- **Workflow**: Your preferred AI assistant will be able to invoke the Marker UI tool natively to convert local files, search history, and retrieve high-fidelity Markdown outputs directly into the LLM context window without manual copy-pasting.

### 2. Inline Asset & Output Preview
- Provide a side-by-side split pane in the web interface showing the input PDF and the formatted Markdown/HTML output side-by-side.
- Render extracted images inline inside the preview area.

### 3. Conversion Profiles
- Save custom parameters configurations (e.g. "Strict Layout", "Fast OCR", "LLM-Refined Table-Extract") as reusable profiles.

---

## Medium-Term Roadmap

### 1. Batch Conversion Workflows
- Allow users to upload folders or lists of documents to be processed in parallel or queued sequentially.
- Implement folder watchers that trigger conversions automatically when a new PDF is added.

### 2. Context-Aware Visual Reconstruction & Alt-Text (VLM Integration)
- **Objective**: Solve the visual data gap for vision-less language models and downstream RAG pipelines.
- **Problem Statement**: Still images currently get linked into the markdown file, separated from the PDF, and we have to download it as a zip to maintain the folder structure so that the images can be linked in the markdown. For a model that has no vision, this becomes difficult to read the whole thing.
- **Approach**: We will find a way to have very accurate descriptions of images or representations of any chart or anything that is context-aware about the whole markdown file and would represent the visual aspects in text in a very lossless manner, as good as possible.
  - [x] Implement optional Vision-Language Model (VLM) pipelines (e.g., using local Llama-3.2-Vision, Qwen2-VL, or cloud API endpoints) to transcribe visual elements.
  - [x] Convert flowcharts, architectural diagrams, and system block diagrams directly into equivalent Mermaid.js graphs.
  - [x] Automatically convert graphs and charts into high-fidelity markdown data tables or structured JSON arrays.
  - [x] Generate deep, context-aware alt-text descriptions for photos and screenshots, placing them directly within the Markdown document.

### 3. Website URL Compilation & Rich Media Extraction
- Allow users to compile any website from a URL.
- Convert all website information—including text, charts, bar graphs, imagery, audio, and video—into Markdown, JSON, HTML, and chunks formats.
- **YouTube & Rich Video Processing**:
  - Support full extraction of video data from YouTube.
  - Employ an agentic framework or multimodal models to analyze the video from start to finish.
  - Go beyond simple transcripts to extract intent, generate comprehensive descriptions for specific timestamps, and catalog exact highlight moments.
  - Fetch external and metadata context: comments, video description, links/resources mentioned or talked about in the video.
  - Build a high-fidelity visual surrogate/description to act as a lossless textual replacement for the physical video.

### 4. Custom OCR Engine Selection
- Allow users to select and configure custom OCR models.
- Support both locally hosted models (e.g., Tesseract, EasyOCR, PaddleOCR) and cloud API-based OCR services.

---

## Long-Term Vision & Enhancements

### 1. Single-File Portable Outputs (Base64 Embeds)
- Provide a configuration option to package output Markdown files with images embedded directly as inline Base64 data URIs.
- This eliminates the need to download ZIP archives, keeping Markdown documents completely portable and self-contained for easy ingestion by AI agents and other tools.

### 2. Direct Vector Database / RAG Pipeline Connectors
- Integrate ingestion adapters for popular vector databases (Chroma, Qdrant, Milvus) and framework connectors (LlamaIndex, LangChain) directly into the UI.
- Allow users to convert documents and push them directly to their search index in one step.

---

## Ideas & Community Contributions

We believe the best tool development is driven by practical user needs. If you have:
- New architectural concepts or UI suggestions.
- Integration ideas for specific RAG pipelines or vector databases.
- Feedback on processing speeds or API design.

Please open a feature request in the GitHub Discussions or Issues tracker. We actively review and implement community ideas!
