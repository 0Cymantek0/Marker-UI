# VLM Providers Configuration

Marker UI supports local and cloud Vision-Language Models (VLMs) to drive the image-understanding pipeline.

---

## Privacy Model

Image bytes stay local unless a conversion is run with cloud VLM access enabled. Local-only routes can still use deterministic routing and local OCR. Cloud extraction requires an explicitly configured provider and a per-job opt-in.

---

## Local Models

Use a local provider such as Ollama for vision-capable models already downloaded on the host. Local models avoid sending document images off-machine, but quality and speed depend on available CPU/GPU memory.

---

## Cloud Models

Cloud providers can be configured for Gemini, Claude, OpenAI-compatible endpoints, Azure, and other supported LLM providers. API keys are stored through the existing provider settings and are resolved server-side at request time.

Cloud VLMs are best for charts, diagrams, screenshots, and photos where local OCR is not enough. They should be treated as a data-sharing path and enabled only for documents that can leave the machine.

---

## Cloud-Dependent Media Features

Cloud-only media features, such as Azure Content Understanding or remote transcription services, are not product defaults. When a Markitdown-compatible feature depends on cloud processing, Marker UI should first define a local route of similar quality:

- Audio: local ASR before cloud transcription.
- Video: local audio transcription plus scene/keyframe sampling, frame OCR, and local VLM summaries before any cloud video analyzer.
- YouTube/URLs: captions and metadata are useful context, but visual claims must come from frame or clip analysis, not transcript text alone.

Cloud analyzers may be offered later as explicit fallback/comparison paths with provider disclosure.

---

## Selection Logic

Saving API keys in **Settings** enables matching provider models configured by the user. Local processing runs through configured local providers. If cloud VLM access is not enabled for the job, cloud providers are not called even when credentials exist.

The conversion output records image-understanding metadata such as image type, confidence, model, and omission state when available, so history and result details can explain what happened.
