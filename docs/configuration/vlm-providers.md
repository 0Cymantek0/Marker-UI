               # VLM Providers Configuration

Marker UI supports local and cloud Vision-Language Models (VLMs) to drive the image-understanding pipeline.

---

## Models

### Local Models (Ollama)
- Any local vision-capable model downloaded to your Ollama node.

### Cloud Models (API Key)
- Configure the models and versions you wish to use for Gemini, Claude, OpenAI, etc.

---

## Selection Logic

Saving API keys in **Settings** enables matching provider models configured by the user. Local processing runs via configured Ollama models. Out-of-the-box support for Azure and custom OpenAI-compatible endpoints is available.
