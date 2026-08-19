"""One-shot refill driver for the PR80B LLM cache (not part of the suite).

Environment overrides so no endpoint or key is ever committed:
    LLM_BASE_URL   OpenAI-compatible chat-completions base URL
                   (default: the OpenRouter gateway)
    LLM_API_KEY    API key for that gateway
    LLM_MODELS     comma-separated model chain (default: OpenRouter chain)

Paces one live call per corpus document with generous backoff, because
shared free-tier pools 429 under burst traffic. Wipes and rebuilds the
cache so every document is answered by the same model in one run.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.eval.pr80b.corpus import load_corpus
from app.eval.pr80b.llm import (
    API_URL,
    CACHE_SCHEMA_VERSION,
    DEFAULT_MODEL_CHAIN,
    OpenRouterClient,
    cache_key,
)

CACHE = BACKEND.parent / "docs" / "reference" / "measurements" / "pr80b-llm-cache.json"
INTER_DOC_PAUSE_S = 8.0


def main() -> int:
    import json

    base_url = os.environ.get("LLM_BASE_URL", API_URL)
    api_key = os.environ.get("LLM_API_KEY")
    models = tuple(
        m.strip()
        for m in os.environ.get("LLM_MODELS", ",".join(DEFAULT_MODEL_CHAIN)).split(",")
        if m.strip()
    )
    print(f"gateway: {base_url} models: {models}")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps(
            {
                "cache_schema_version": CACHE_SCHEMA_VERSION,
                "model_chain": list(models),
                "gateway_origin": base_url,
                "responses": {},
            }
        ),
        encoding="utf-8",
    )
    corpus = load_corpus(BACKEND / "eval_data" / "pr80b")
    failures = 0
    for index, doc in enumerate(corpus.docs, start=1):
        client = OpenRouterClient(
            models,
            api_key=api_key,
            base_url=base_url,
            cache_path=None,
            mode="live",
            max_retries=5,
            retry_backoff=10.0,
        )
        envelope = client.extract(doc.full_text)
        envelope["from_cache"] = False
        cache_lock = json.loads(CACHE.read_text(encoding="utf-8"))
        cache_lock["responses"][cache_key(models[0], doc.full_text)] = envelope
        CACHE.write_text(json.dumps(cache_lock, indent=2, sort_keys=True), encoding="utf-8")
        status = "ok" if envelope["error"] is None else f"ERROR: {envelope['error'][:80]}"
        if envelope["error"] is not None:
            failures += 1
        print(f"[{index:02d}/{len(corpus.docs)}] {doc.doc_id}: {status}", flush=True)
        if index < len(corpus.docs):
            time.sleep(INTER_DOC_PAUSE_S)
    print(f"done; failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
