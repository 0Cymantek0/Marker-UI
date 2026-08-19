"""PR81B capability probe: can this model do image QA at all?

Cheap gate before committing a model to the full benchmark: two
known-answer questions over freshly rendered corpus pages (one grouped
bar chart, one dense table) plus one label-placement question on the
same chart. A model that cannot read values or labels from a rendered
page cannot meaningfully answer or rerank for this corpus, and the
matrix records that honestly instead of averaging it away.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.eval.pr81a.corpus import Corpus, CorpusDoc
from app.eval.pr81a.normalize import NormalizeError, normalize_answer
from app.eval.pr81a.visual_store import PageRenderStore
from app.eval.pr81a.vlm import VlmClient

PROBE_SCHEMA = "marker.pr81b_capability_probe.v1"


@dataclass(frozen=True)
class ProbeCase:
    case_id: str
    doc_id: str
    page_number: int
    question: str
    gold: str
    answer_kind: str

    @property
    def normalized_gold(self) -> str:
        return normalize_answer(self.gold, self.answer_kind)


#: Two pages, three questions — gold drawn from the same constants that
#: draw the pixels (corpus generator), so the renders provably contain
#: each answer. Kept distinct from every corpus query text.
PROBE_CASES: tuple[ProbeCase, ...] = (
    ProbeCase(
        case_id="fin01-bar-value",
        doc_id="doc-fin-01",
        page_number=2,
        question="Look at the chart. What is the numeric value printed on the tallest bar?",
        gold="4.0",
        answer_kind="decimal",
    ),
    ProbeCase(
        case_id="fin01-bar-region",
        doc_id="doc-fin-01",
        page_number=2,
        question="Look at the chart. Which region label does the tallest bar belong to?",
        gold="West",
        answer_kind="string",
    ),
    ProbeCase(
        case_id="ops01-budget",
        doc_id="doc-ops-01",
        page_number=1,
        question="What is the approved budget value shown on this page?",
        gold="95000",
        answer_kind="decimal",
    ),
)

#: a model passes the gate with at most one miss; two clean value reads
#: with a failed label read is vision, one flaky value read is noise,
#: zero or one correct is not a usable answerer for this corpus
PROBE_PASS_MIN = 2


@dataclass(frozen=True)
class ProbeCaseResult:
    case_id: str
    question: str
    gold: str
    normalized_gold: str
    answer: str | None
    error: str | None
    passed: bool


def _render_case_png(corpus: Corpus, render_store: PageRenderStore, case: ProbeCase) -> bytes:
    doc: CorpusDoc = corpus.doc(case.doc_id)
    revision = doc.current
    rendered = render_store.render(
        f"sha256:{revision.pdf_sha256}",
        case.page_number - 1,
        revision.pdf_path,
        admitted=True,
    )
    return rendered.path.read_bytes()


def run_capability_probe(
    corpus: Corpus,
    render_store: PageRenderStore,
    vlm: VlmClient,
    *,
    cases: tuple[ProbeCase, ...] = PROBE_CASES,
) -> dict:
    """Ask every probe case against one model; grade by PR81A normalization."""
    from app.eval.pr81a.vlm import CacheMissError

    results: list[ProbeCaseResult] = []
    for case in cases:
        png = _render_case_png(corpus, render_store, case)
        answer: str | None = None
        error: str | None = None
        passed = False
        try:
            envelope, parsed = vlm.answer(case.question, page_png=png, page_text=None)
        except CacheMissError:
            error = "vlm: no cached response (replay miss)"
        else:
            if envelope.error:
                error = f"vlm: {envelope.error}"
            elif parsed is None or "answer" not in parsed or parsed["answer"] is None:
                error = "no answer delivered"
                if parsed is not None and parsed.get("answer") is None:
                    error = "answer was null"
            else:
                answer = str(parsed["answer"])
                try:
                    passed = (
                        normalize_answer(answer, case.answer_kind) == case.normalized_gold
                    )
                except NormalizeError:
                    error = "answer does not normalize"
        results.append(
            ProbeCaseResult(
                case_id=case.case_id,
                question=case.question,
                gold=case.gold,
                normalized_gold=case.normalized_gold,
                answer=answer,
                error=error,
                passed=passed,
            )
        )
    correct = sum(1 for r in results if r.passed)
    return {
        "schema_version": PROBE_SCHEMA,
        "model_chain": list(vlm.models),
        "cases": [r.__dict__ for r in results],
        "correct": correct,
        "total": len(results),
        "passed": correct >= PROBE_PASS_MIN,
        "pass_rule": f"at least {PROBE_PASS_MIN} of {len(cases)} probe cases correct",
    }
