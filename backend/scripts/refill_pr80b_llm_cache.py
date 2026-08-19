"""One-shot refill driver for the PR80B LLM cache (not part of the suite).

Paces one live call per corpus document against a single free-tier
model with generous backoff, because the shared upstream pool 429s
under burst traffic. Wipes and rebuilds the cache so every document
is answered by the same model in one run.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.eval.pr80b.corpus import load_corpus
from app.eval.pr80b.llm import CACHE_SCHEMA_VERSION, OpenRouterClient

MODEL_CHAIN = ("nvidia/nemotron-3-super-120b-a12b:free",)
CACHE = BACKEND.parent / "docs" / "reference" / "measurements" / "pr80b-llm-cache.json"
INTER_DOC_PAUSE_S = 12.0


def main() -> int:
    import json

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps({"cache_schema_version": CACHE_SCHEMA_VERSION, "responses": {}}),
        encoding="utf-8",
    )
    corpus = load_corpus(BACKEND / "eval_data" / "pr80b")
    failures = 0
    for index, doc in enumerate(corpus.docs, start=1):
        client = OpenRouterClient(
            MODEL_CHAIN,
            cache_path=CACHE,
            mode="live",
            max_retries=5,
            retry_backoff=15.0,
        )
        envelope = client.extract(doc.full_text)
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
