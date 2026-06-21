# LLM Providers Configuration

Marker UI can use external or local Large Language Models to refine Markdown layouts, fix OCR slips, and structure tables.

---

## Supported Services & Setup

You can configure your preferred LLM provider on the **Settings** page:

### 1. OpenAI
- **Keys Required**: OpenAI API Key (`sk-...`).
- **Models**: Configure the models and versions you wish to use (e.g. `gpt-4o`, `gpt-4o-mini`).

### 2. Google Gemini
- **Keys Required**: Gemini API Key.
- **Models**: Configure the models and versions you wish to use (e.g. `gemini-2.0-flash`, `gemini-2.5-flash`).

### 3. Anthropic Claude
- **Keys Required**: Anthropic API Key.
- **Models**: Configure the models and versions you wish to use (e.g. `claude-3-7-sonnet-20250219`, `claude-3-5-sonnet-20241022`).

### 4. Ollama (Local)
- **Base URL**: The local port address where Ollama is running (typically `http://127.0.0.1:11434` or `http://host.docker.internal:11434` if running inside Docker).
- **Models**: Any local model downloaded to your Ollama node.

### 5. Azure OpenAI
- **Credentials Required**: Azure API Key, Azure Endpoint, and Azure Deployment Name.

### 6. Vertex AI
- **Credentials Required**: Google Cloud Project ID, Location (e.g. `us-central1`), and Service Account Credentials JSON path.

---

## Local Encryption

When you enter an API Key in the UI:
1. The frontend posts the key to `/api/settings`.
2. The backend generates a Fernet symmetric key (saved to `data/.secret_key`) on first run if not already present.
3. The API Key is encrypted using this Fernet key and persisted in the SQLite database.
4. When displayed back to you, the key is masked as `sk-proj-****abcd` to avoid accidental exposure.
