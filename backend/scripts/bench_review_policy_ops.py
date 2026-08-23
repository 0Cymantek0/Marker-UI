"""Invariant 22/23/26 integrated verification-policy tracer (PR88).

One deterministic scenario over the REAL authorities on SQLite:

1. publish a document whose regions ground differently (one accepted
   region, two conflicting review-required regions);
2. run the extraction service — the accepted region is committed as a
   kernel claim, the conflicted regions enter the review lifecycle and
   are durably accounted;
3. adjudicate: accept one conflict, correct the other (human-sourced,
   warning-cleared), refuse a bypass attempt, supersede the
   publication and refuse a stale decision;
4. resolve region-relative effective status through the kernel
   assessment view before and after adjudication;
5. build the v2 calibration applicability artifact for the matched
   corpus slice and prove the gate-required discipline (named
   population, mandatory expiry, exact zero-catastrophe bound);
6. reload every number from durable state and emit the machine-readable
   measurement artifact ``marker.review_policy_ops.report.v1``.

Clocks are injected; no wall-clock sleeps. The bench fails closed: any
check or validator failure exits non-zero and nothing is written.

Usage:
  python scripts/bench_review_policy_ops.py            # print summary
  python scripts/bench_review_policy_ops.py --write    # + write artifact
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db_migration import upgrade_database  # noqa: E402
from app.eval.verification_risk import (  # noqa: E402
    CalibrationApplicability,
    build_applicability,
    evaluate_calibration,
    load_verification_risk_corpus,
)
from app.extraction.contract import INVOICE_SCHEMA, ExtractionRequest  # noqa: E402
from app.extraction.reconciliation import (  # noqa: E402
    RECONCILE_POLICY_ID,
    RECONCILE_POLICY_VERSION,
)
from app.extraction.review import ReviewDecision, ReviewError, StaleReviewError  # noqa: E402
from app.extraction.review_ops import (  # noqa: E402
    derive_review_metrics,
    load_review_transitions,
    validate_review_ops_report,
)
from app.extraction.service import (  # noqa: E402
    EXTRACTION_WORKFLOW_CLASS,
    ExtractionService,
    _assertion_record_id,
)
from app.kernel.assessment_view import (  # noqa: E402
    DOCUMENT_USABLE_WITH_UNRESOLVED_REGIONS,
    resolve_effective_assessments,
    summarize_regions,
)
from app.kernel.commit import KernelCommitBatch, KernelCommitService  # noqa: E402
from app.kernel.generations import GenerationService  # noqa: E402
from app.kernel.models import KernelRecord as KernelRecordRow  # noqa: E402
from app.kernel.publications import PublicationService  # noqa: E402
from app.kernel.records import ClaimAssessmentRecord  # noqa: E402
from app.kernel.snapshots import resolve_snapshot  # noqa: E402

REPORT_SCHEMA_VERSION = "marker.review_policy_ops.report.v1"
WS = "ws-pr88-ops"

T0 = "2026-08-20T10:00:00Z"
T1 = "2026-08-20T10:05:00Z"
T2 = "2026-08-20T10:12:00Z"
T3 = "2026-08-20T10:20:00Z"
T4 = "2026-08-20T10:25:00Z"
AS_OF = "2026-08-20T10:30:00Z"

CORPUS_FIXTURE = BACKEND / "conformance" / "fixtures" / "verification_risk_corpus_v1.json"

CALIBRATION_ASSUMPTIONS = {
    "label_definition": "witness prediction differs from the labeled truth",
    "sampling_frame": "all samples of the matched slice in the v1 corpus",
    "policy_id": "marker.high_risk.source_native",
    "policy_revision": "1",
    "workflow_class": "high_risk.source_native.v1",
    "distribution_class": "matched",
}


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _clock(*timestamps: str):
    queue = list(timestamps)
    return lambda: queue.pop(0) if queue else timestamps[-1]


def _header_doc(*, total: str = "154.97", currency: str = "USD") -> dict[str, str]:
    return {
        "h1": "Invoice Number: INV-2026-042",
        "h2": "Invoice Date: 2026-03-01",
        "h3": f"Currency: {currency}",
        "h4": f"Total Due: {total}",
        "h5": "PO Number: PO-77",
    }


def _items_doc() -> dict[str, str]:
    rows = [
        "LINEITEM | SKU-1 | Widget | 2 | 9.99 | 19.98",
        "LINEITEM | SKU-2 | Gadget | 3 | 15.00 | 45.00",
    ]
    return {f"r{index}": row for index, row in enumerate(rows, start=1)}


def _view_doc(record_id: str, texts: dict[str, str], revision: str):
    from app.kernel.patches import ViewDocumentRecord
    from app.kernel.reading_order import OrderNode, ReadingOrderGraph

    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in texts), ()
    )
    return ViewDocumentRecord(
        record_id=record_id,
        content_revision_ref=revision,
        graph=graph,
        texts=dict(texts),
        view_id=f"doc-{record_id}",
    )


async def _publish(factory, commit_service, docs) -> str:
    await commit_service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=tuple(_view_doc(record_id, texts, revision) for record_id, texts, revision in docs),
        )
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, WS)
    )
    publication = await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )
    return publication.publication_set_id


def _decision(result, field: str, action: str, **overrides) -> ReviewDecision:
    return ReviewDecision(
        result_identity=result.identity,
        schema_identity=result.schema_identity,
        publication_set_id=result.context.publication_set_id,
        field_path=field,
        action=action,
        reviewer="reviewer@example.test",
        rationale="tracer adjudication",
        **overrides,
    )


async def _load_carried_assessments(factory):
    from sqlalchemy import select

    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    KernelRecordRow.kernel_commit_id,
                    KernelRecordRow.id,
                    KernelRecordRow.payload_json,
                ).where(
                    KernelRecordRow.workspace_id == WS,
                    KernelRecordRow.record_class == "claim_assessment",
                )
            )
        ).all()
    return [
        (
            commit_id,
            ClaimAssessmentRecord.from_payload(json.loads(payload), record_id=record_id),
        )
        for commit_id, record_id, payload in rows
    ]


def _region_view(carried, head: int) -> tuple[dict, str]:
    refs = {
        "invoice_number": _assertion_record_id("demo.invoice@invoice_number", "INV-2026-042"),
        "total_due": _assertion_record_id("demo.invoice@total_due", "154.97"),
        "currency": _assertion_record_id("demo.invoice@currency", "USD"),
        # Review corrections commit under their own human-sourced claim
        # key — deliberately a different claim than the source-evaluated
        # region, so human review can never masquerade as source truth.
        "currency_reviewed": _assertion_record_id("demo.invoice@currency#reviewed", "USD"),
    }
    view = resolve_effective_assessments(
        carried,
        policy_id=RECONCILE_POLICY_ID,
        policy_revision=RECONCILE_POLICY_VERSION,
        workflow_class=EXTRACTION_WORKFLOW_CLASS,
        as_of_commit=head,
        assertion_refs=list(refs.values()),
    )
    regions = {name: view[ref].as_dict() for name, ref in refs.items()}
    return regions, summarize_regions(view)["document_state"]


def _calibration_section() -> dict:
    corpus = load_verification_risk_corpus(CORPUS_FIXTURE)
    result = evaluate_calibration(
        corpus, "model-a", slice_id="matched", distribution="matched"
    )
    artifact: CalibrationApplicability = build_applicability(
        result,
        population_name="invoice-total/en/matched/v1",
        sampling_frame="all samples of the matched slice in the v1 corpus",
        assumptions=CALIBRATION_ASSUMPTIONS,
        evaluated_at="2026-08-01T00:00:00Z",
        expires_at="2026-09-01T00:00:00Z",
        retest_triggers=frozenset(
            {"time_expiry", "policy_revision_change", "population_shift"}
        ),
        catastrophic_failures=0,
        catastrophic_trials=result.sample_count,
    )
    data = artifact.as_dict()
    catastrophic = data["catastrophic_failures"]
    return {
        "artifact": data,
        "checks": {
            "population_named": bool(data["population"]["name"]),
            "assumptions_structured": set(data["assumptions"]) >= {
                "label_definition",
                "sampling_frame",
            },
            "expiry_machine_evaluable": not artifact.is_expired(AS_OF)
            and artifact.is_expired("2026-09-02T00:00:00Z"),
            "applies_only_to_named_context": artifact.applies_to(
                policy_id="marker.high_risk.source_native",
                policy_revision="1",
            )
            and not artifact.applies_to(policy_revision="2"),
            "zero_catastrophes_not_zero_risk": catastrophic["observed_failures"] == 0
            and catastrophic["zero_failures_implies_zero_risk"] is False
            and float(catastrophic["upper_bound_95"]) > 0,
            "round_trip_fail_closed": (
                CalibrationApplicability.from_dict(data).as_dict() == data
            ),
        },
    }


async def run_scenario(factory, commit_service) -> dict:
    service = ExtractionService(
        factory,
        commit_service,
        workspace_id=WS,
        review_clock=_clock(T0, T1, T2, T3, T4),
    )

    publication = await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _header_doc(), "rev-h1"),
            ("invoice-header-alt", _header_doc(total="777.77", currency="EUR"), "rev-h1-alt"),
            ("invoice-items", _items_doc(), "rev-i1"),
        ],
    )
    result = await service.run(
        ExtractionRequest(
            schema_id=INVOICE_SCHEMA.schema_id,
            schema_version=INVOICE_SCHEMA.version,
            workspace_id=WS,
        )
    )
    from app.kernel.replay import read_head

    head_at_run = await read_head(factory, WS)

    carried_before = await _load_carried_assessments(factory)
    regions_before, state_before = _region_view(carried_before, head_at_run)

    accepted = await service.apply_review(_decision(result, "total_due", "accept"))
    corrected = await service.apply_review(
        _decision(result, "currency", "correct", value="USD")
    )

    # Replay probe: byte-identical decision replays idempotently.
    replay = await service.apply_review(_decision(result, "total_due", "accept"))
    replay_same = (
        replay.fields["total_due"].status == accepted.fields["total_due"].status
    )

    # Bypass probe: re-adjudicating the accepted field is refused.
    bypass_refused = False
    try:
        await service.apply_review(_decision(accepted, "total_due", "accept"))
    except ReviewError:
        bypass_refused = True

    # Stale probe: supersede the publication, then decide on the old one.
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header-v2", _header_doc(total="200.00"), "rev-h2"),
        ],
    )
    stale_refused = False
    try:
        await service.apply_review(_decision(result, "total_due", "accept"))
    except StaleReviewError:
        stale_refused = True

    head_after = await read_head(factory, WS)
    carried_after = await _load_carried_assessments(factory)
    regions_after, state_after = _region_view(carried_after, head_after)

    # Reload accounting purely from durable state.
    transitions = await load_review_transitions(factory, WS)
    metrics = derive_review_metrics(
        transitions,
        workspace_id=WS,
        schema_id=INVOICE_SCHEMA.schema_id,
        policy_id=RECONCILE_POLICY_ID,
        policy_version=RECONCILE_POLICY_VERSION,
    )

    return {
        "publication_set_id": publication,
        "run_status": result.run_status,
        "regions_before_review": regions_before,
        "regions_after_review": regions_after,
        "document_state_before_review": state_before,
        "document_state_after_review": state_after,
        "replay_idempotent": replay_same,
        "bypass_refused": bypass_refused,
        "stale_refused": stale_refused,
        "metrics": metrics,
    }


def build_report(scenario: dict, calibration: dict, git_sha: str) -> dict:
    metrics = scenario["metrics"]
    before = scenario["regions_before_review"]
    after = scenario["regions_after_review"]
    validate_review_ops_report(metrics)

    checks = {
        "accepted_region_usable_before_review": before["invoice_number"]["usable"]
        is True
        and before["invoice_number"]["usability_class"] == "usable_authority",
        "conflicted_regions_unresolved_before_review": before["total_due"][
            "usability_class"
        ]
        == "unresolved_unavailable"
        and before["currency"]["usability_class"] == "unresolved_unavailable",
        "no_region_poisoning": before["invoice_number"]["usable"] is True
        and after["invoice_number"]["usable"] is True
        and after["invoice_number"]["assessment_id"]
        == before["invoice_number"]["assessment_id"],
        "no_region_promotion": after["total_due"]["usability_class"]
        == "unresolved_unavailable",
        "corrected_region_human_sourced_not_source_authority": after["currency"][
            "usability_class"
        ]
        == "unresolved_unavailable"
        and after["currency_reviewed"]["usability_class"] == "usable_with_warning"
        and after["currency_reviewed"]["outcome"] == "accepted_with_warning",
        "document_state_preserves_regions": scenario["document_state_after_review"]
        == DOCUMENT_USABLE_WITH_UNRESOLVED_REGIONS,
        "review_required_accounted": metrics["required_review"] == 2,
        "review_coverage_complete": metrics["reviewed"] == 2
        and metrics["review_coverage_rate"]["value"] == 1.0,
        "dwell_measured_deterministically": metrics["dwell"]["status"] == "defined"
        and metrics["dwell"]["min_seconds"] == 300.0
        and metrics["dwell"]["max_seconds"] == 720.0,
        "outcomes_honest": metrics["outcomes"]
        == {"accepted": 1, "corrected": 1, "rejected": 0},
        "replay_idempotent": scenario["replay_idempotent"] is True,
        "bypass_refused_and_accounted": scenario["bypass_refused"] is True
        and metrics["bypass_refusals"] == 1,
        "stale_refused_and_accounted": scenario["stale_refused"] is True
        and metrics["stale_rejections"] == 1,
        **{f"calibration_{name}": value for name, value in calibration["checks"].items()},
    }
    if not all(checks.values()):
        failed = sorted(name for name, value in checks.items() if not value)
        raise SystemExit(f"tracer checks failed: {failed}")

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "git_sha": git_sha,
        "environment": {
            "database": "sqlite (aiosqlite, file-backed, alembic head)",
            "clock": "injected deterministic ISO timestamps (no wall-clock sleeps)",
            "authorities": "real kernel commit/publication/query/extraction/review seams",
        },
        "scenario_timeline": {
            "t0_required": T0,
            "t1_accept": T1,
            "t2_correct": T2,
            "t3_bypass_attempt": T3,
            "t4_stale_attempt": T4,
        },
        "region_status": {
            "before_review": before,
            "after_review": after,
            "document_state_after_review": scenario["document_state_after_review"],
        },
        "calibration_applicability": calibration["artifact"],
        "metrics": metrics,
        "checks": checks,
        "non_claims": [
            "fixture-scale operational accounting; no production staffing or queue claim",
            "dwell is the declared deterministic measure, not human review time",
            "calibration support is the committed conformance corpus, not production traffic",
        ],
    }


async def _main_async(args) -> int:
    with tempfile.TemporaryDirectory(prefix="pr88-ops-") as tmp:
        db_path = Path(tmp) / "kernel.db"
        url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
        await upgrade_database(url=url)
        engine = create_async_engine(url, connect_args={"check_same_thread": False})
        from sqlalchemy.ext.asyncio import AsyncSession

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            commit_service = KernelCommitService(factory)
            scenario = await run_scenario(factory, commit_service)
        finally:
            await engine.dispose()

    calibration = _calibration_section()
    report = build_report(scenario, calibration, _git_sha())

    summary = {
        "git_sha": report["git_sha"],
        "required_review": report["metrics"]["required_review"],
        "reviewed": report["metrics"]["reviewed"],
        "coverage": report["metrics"]["review_coverage_rate"]["value"],
        "dwell_seconds": [
            report["metrics"]["dwell"]["min_seconds"],
            report["metrics"]["dwell"]["max_seconds"],
        ],
        "bypass_refusals": report["metrics"]["bypass_refusals"],
        "stale_rejections": report["metrics"]["stale_rejections"],
        "document_state": report["region_status"]["document_state_after_review"],
        "checks_passed": sum(1 for value in report["checks"].values() if value),
        "checks_total": len(report["checks"]),
    }
    print(json.dumps(summary, indent=2))

    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the artifact")
    parser.add_argument(
        "--output",
        type=Path,
        default=BACKEND.parent / "docs" / "reference" / "measurements" / "pr88-review-policy-ops.json",
        help="artifact path",
    )
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
