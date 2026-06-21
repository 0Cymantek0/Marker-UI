# Converting Documents

This guide walks through using the Marker UI browser interface to convert documents.

---

## 1. Onboarding (First Startup)
Before you can run any conversions, the app must ensure the local neural models are fully downloaded and configured.
- If model weights are missing, you will be redirected to the **Onboarding Page**.
- The onboarding page displays download progress, speed indicators, and logs for the following engines:
  - **Text Detection Engine**
  - **Layout Segmentation Engine**
  - **Neural OCR Engine**
  - **Table Structure Analyzer**
- Once weights are downloaded, the system unlocks the conversion interface.

---

## 2. Setting up a Conversion Job
Navigate to the **Convert** page:
1. **File Upload**: Drag and drop your documents (PDF, DOCX, PPTX, XLSX, EPUB, images) into the upload zone, or click to browse.
2. **Local Paths (Optional)**: If you are running the server locally, you can supply absolute path strings directly to avoid uploading massive files. (See [Local File Paths](local-file-paths.md)).
3. **Select Options**:
   - **Output Format**: Choose Markdown, HTML, JSON, or Chunks.
   - **Converter Engine**: Select standard options:
     - `PdfConverter`: Standard PDF/document layout, text, tables, and images.
     - `TableConverter`: Table-focused layout optimized for spreadsheets.
     - `OCRConverter`: OCR-only extraction for scanned/low-quality inputs.
     - `ExtractionConverter`: Fast text-only parser bypassing heavy styling.
   - **LLM Refinement**: Toggle this to route outputs through a connected LLM to clean up OCR noise and fix structural flow.

---

## 3. Monitoring Progress
Click the **Execution Console** to view the live execution log.
- During conversion, Server-Sent Events (SSE) stream the terminal logs and status updates directly to your screen.
- You can watch the execution stages progress in real time (e.g. `Extracting Text`, `Running Layout Models`, `OCR Processing`).
- If an error occurs, the log console will print the traceback for quick debugging.
