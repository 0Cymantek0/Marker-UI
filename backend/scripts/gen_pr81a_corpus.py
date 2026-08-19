"""Generate the committed PR81A corpus under backend/eval_data/pr81a.

Builds every PDF from ``app.eval.pr81a.corpus_gen``, asserts byte
determinism by building twice and comparing hashes, then writes:

* ``corpus/<doc-key>.pdf``         - deterministic page renders source
* ``oracle/<doc-key>.json``        - per-page oracle text transcript
* ``queries.json``                 - judged queries with gold answers
* ``manifest.json``                - fail-closed loader contract

Run:  python scripts/gen_pr81a_corpus.py [--output backend/eval_data/pr81a]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.eval.pr81a import corpus_gen  # noqa: E402

#: document-level manifest facts: domain, slices, visual admission.
#: ``visual_admitted`` is the declared selective-admission policy the
#: benchmark measures: only visually rich documents enter the visual route.
DOC_FACTS: dict[str, dict] = {
    "doc-fin-01": {
        "domain": "general",
        "slices": ["chart.bar", "table.grid", "text.summary"],
        "visual_admitted": True,
    },
    "doc-fin-03": {
        "domain": "general",
        "slices": ["chart.bar", "table.grid", "text.summary"],
        "visual_admitted": True,
    },
    "doc-fin-02": {
        "domain": "general",
        "slices": ["table.grid", "chart.line"],
        "visual_admitted": True,
    },
    "doc-rd-01": {
        "domain": "general",
        "slices": ["chart.pie", "table.grid"],
        "visual_admitted": True,
    },
    "doc-rd-02": {
        "domain": "general",
        "slices": ["chart.pie", "table.grid"],
        "visual_admitted": True,
    },
    "doc-ops-01": {
        "domain": "general",
        "slices": ["form.columns"],
        "visual_admitted": True,
    },
    "doc-ops-02": {
        "domain": "general",
        "slices": ["layout.two_column"],
        "visual_admitted": True,
    },
    "doc-hr-01": {
        "domain": "general",
        "slices": ["text.plain"],
        "visual_admitted": False,
    },
    "doc-hr-02": {
        "domain": "general",
        "slices": ["chart.org"],
        "visual_admitted": True,
    },
    "doc-mfg-01": {
        "domain": "general",
        "slices": ["chart.dashboard"],
        "visual_admitted": True,
    },
    "doc-rev-01-v3": {
        "domain": "general",
        "slices": ["revision.checklist"],
        "visual_admitted": True,
    },
    "doc-rev-01-v4": {
        "domain": "general",
        "slices": ["revision.checklist"],
        "visual_admitted": True,
    },
    "doc-sec-01": {
        "domain": "restricted",
        "slices": ["authz.restricted"],
        "visual_admitted": True,
    },
    "doc-sec-02": {
        "domain": "restricted",
        "slices": ["authz.restricted"],
        "visual_admitted": True,
    },
    "doc-pub-03": {
        "domain": "general",
        "slices": ["authz.public", "text.summary"],
        "visual_admitted": True,
    },
    "doc-leg-01": {
        "domain": "general",
        "slices": ["text.plain"],
        "visual_admitted": False,
    },
}

#: Map artifact doc_keys (which carry the revision suffix for doc-rev-01)
#: to logical doc ids in the manifest.
def _logical_doc_id(doc_key: str) -> str:
    if doc_key.startswith("doc-rev-01-"):
        return "doc-rev-01"
    return doc_key


def _revision_name(doc_key: str) -> str:
    if doc_key.startswith("doc-rev-01-"):
        return doc_key.rsplit("-", 1)[-1]
    return "v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=BACKEND / "eval_data" / "pr81a",
        help="corpus root (default: backend/eval_data/pr81a)",
    )
    args = parser.parse_args()
    root: Path = args.output

    first = corpus_gen.build_all()
    second = corpus_gen.build_all()
    for doc_key, artifact in first.items():
        twin = second[doc_key]
        if artifact.pdf_bytes != twin.pdf_bytes:
            print(f"NOT DETERMINISTIC: {doc_key}", file=sys.stderr)
            return 1
    print(f"determinism check passed for {len(first)} documents")

    corpus_dir = root / "corpus"
    oracle_dir = root / "oracle"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    oracle_dir.mkdir(parents=True, exist_ok=True)

    documents: dict[str, dict] = {}
    for doc_key in sorted(first):
        artifact = first[doc_key]
        logical = _logical_doc_id(doc_key)
        (corpus_dir / f"{doc_key}.pdf").write_bytes(artifact.pdf_bytes)
        oracle_payload = {
            "doc_id": logical,
            "revision": _revision_name(doc_key),
            "pages": [
                {"page_number": page.page_number, "nodes": list(page.nodes)}
                for page in artifact.pages
            ],
        }
        oracle_path = oracle_dir / f"{doc_key}.json"
        oracle_path.write_text(
            json.dumps(oracle_payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        entry = documents.setdefault(
            logical,
            {
                "doc_id": logical,
                **{k: v for k, v in DOC_FACTS[doc_key].items()},
                "revisions": [],
            },
        )
        revision_entry: dict = {
            "revision": _revision_name(doc_key),
            "pdf": f"corpus/{doc_key}.pdf",
            "oracle": f"oracle/{doc_key}.json",
        }
        if logical == "doc-rev-01" and _revision_name(doc_key) == "v3":
            revision_entry["superseded_by"] = "v4"
        entry["revisions"].append(revision_entry)

    queries_payload = [
        {
            "query_id": q.query_id,
            "text": q.text,
            "slice_tag": q.slice_tag,
            "doc_id": q.doc_id,
            "page_number": q.page_number,
            "answer": q.answer,
            "answer_kind": q.answer_kind,
            **({"phase": q.phase} if q.phase != "baseline" else {}),
            **({"profile": q.profile} if q.profile != "default" else {}),
            **({"expectation": q.expectation} if q.expectation != "answer" else {}),
        }
        for q in corpus_gen.QUERIES
    ]
    (root / "queries.json").write_text(
        json.dumps(queries_payload, indent=2) + "\n", encoding="utf-8"
    )

    slice_counts: dict[str, int] = {}
    for q in queries_payload:
        slice_counts[q["slice_tag"]] = slice_counts.get(q["slice_tag"], 0) + 1

    manifest = {
        "manifest_version": "marker.pr81a_corpus.v1",
        "benchmark": "PR81A selective visual retrieval promotion experiment",
        "generated": "2026-08-19",
        "generator_version": corpus_gen.GENERATOR_VERSION,
        "provenance": (
            "Fully synthetic documents generated by "
            "backend/scripts/gen_pr81a_corpus.py from "
            "app.eval.pr81a.corpus_gen; ASCII-only content; no personal "
            "data; deterministic bytes (reportlab invariant mode)."
        ),
        "documents": [documents[k] for k in sorted(documents)],
        "queries": "queries.json",
        "slice_counts": dict(sorted(slice_counts.items())),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote corpus to {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
