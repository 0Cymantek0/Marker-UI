# LLM-Enhanced Refinement

While Marker UI's neural engine handles text extraction, layout segmentation, and OCR, some documents present layout breaks, complex formula misreadings, or spelling mistakes. Marker UI can route the raw output through a connected Large Language Model (LLM) to clean up and refine the final Markdown formatting.

---

## How Refinement Works

1. **Conversion**: The local `marker` engine processes the PDF/document and produces a draft Markdown file.
2. **Analysis**: If **LLM Refinement** is enabled, the backend constructs a prompt containing the draft Markdown.
3. **API Call**: The prompt is sent to your configured LLM provider (e.g. Gemini, Claude, or OpenAI).
4. **Correction**: The model returns a sanitized, clean version of the Markdown - fixing spacing, missing headers, broken tables, or malformed LaTeX blocks.
5. **Storage**: The refined output is saved as the final output.

---

## Configuration

To use LLM Refinement:
1. Go to the **Settings** page.
2. In the **LLM Providers** section, click the keys/settings drawer buttons for your chosen service (Gemini, Claude, OpenAI, Ollama, Azure, Vertex) to enter API keys, base URLs, and customize timeout/retries.
3. Set the active service and model globally using the **Active LLM Provider** dropdown selector card at the top of the settings page.
5. Ensure the encryption key exists in `data/.secret_key` (this happens automatically on first startup).

---

## When to Enable Refinement

- **Enable**: For academic papers with heavy math notations, ancient scanned PDFs with OCR mistakes, or multi-column layouts where reading order gets slightly scrambled.
- **Disable**: For simple text files, or if you have privacy constraints that prevent sending document content to third-party APIs (unless you are using a local `Ollama` endpoint).
