"""Benchmark PR78 authorization-first retrieval evidence.

Run from repository root::

    python backend/scripts/bench_pr78_authorization.py --write

Deterministic two-domain corpus (authorized dom-alpha, forbidden
dom-beta with overlapping terms, plus one unattributed view) driven
through the real commit spine, publication service, and bounded query
executor. Proves structurally — not by wall-clock alone — that:

* unauthorized exact reads share the caller-visible shape of missing
  ones (no existence disclosure);
* forbidden lexical matches never become candidates, counts, or
  more-matches signals, while authorized recall survives a forbidden
  crowd at the top of the shared ranking;
* a live deny refuses delivery while the stale FTS rows are still
  physically present (revocation without reindex);
* EvidencePacket identity invalidates on policy revision, epoch
  advance, and deny revision, and does not churn on unrelated commits;
* the high-assurance partition's candidate order and bm25 score basis
  are invariant to forbidden-corpus growth that visibly shifts the
  shared index's ranking;
* a missing high-assurance partition fails closed.

Wall-clock numbers (allowed vs unauthorized vs nonexistent exact
reads; lexical before/after denial) are recorded as timing
characterization evidence with environment caveats — they are not a
constant-time claim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.context_runtime import (  # noqa: E402
    QUERY_SCHEMA_VERSION,
    execute_query,
    parse_query_request,
)
from app.context_runtime.errors import (  # noqa: E402
    QueryAuthorizationError,
    QueryContractError,
)
from app.db_migration import upgrade_database  # noqa: E402
from app.kernel.commit import KernelCommitBatch, KernelCommitService  # noqa: E402
from app.kernel.generations import GenerationService  # noqa: E402
from app.kernel.models import KernelLexicalRow  # noqa: E402
from app.kernel.patches import ViewDocumentRecord  # noqa: E402
from app.kernel.publications import (  # noqa: E402
    PublicationService,
    fts_table_name,
)
from app.kernel.records import (  # noqa: E402
    SOURCE_CONSISTENCY_NATIVE_ATOMIC,
    AccessPolicyRevisionRecord,
    AuthorizationEpochRecord,
    ContentRevisionRecord,
    SecurityDomainRecord,
    SourceIdentityRecord,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph  # noqa: E402
from app.kernel.snapshots import resolve_snapshot  # noqa: E402
from app.services.query_policy import QueryPolicyService  # noqa: E402

MEASUREMENTS_PATH = (
    BACKEND.parent
    / "docs"
    / "reference"
    / "measurements"
    / "pr78-authorization-retrieval.json"
)

WORKSPACE = "ws-pr78"
REPEATS = 20
ALPHA_DOCS = 4
ALPHA_NODES = 6
BETA_DOCS = 12
BETA_FLOOD_DOCS = 30


def _blob(value: int) -> str:
    return f"sha256:{value:064x}"


async def _seed_doc(
    service: KernelCommitService,
    *,
    tag: str,
    domain: str,
    texts: dict[str, str],
) -> str:
    source = SourceIdentityRecord(
        record_id=f"src.{tag}",
        source_kind="local_path",
        source_key=f"C:/bench/{tag}.md",
    )
    revision = ContentRevisionRecord(
        record_id=f"rev.{tag}",
        source_ref=source.record_id,
        blob_key=_blob(abs(int.from_bytes(tag.encode(), "big")) % (1 << 256)),
        byte_length=sum(len(v) for v in texts.values()),
        media_type="text/markdown",
        consistency_class=SOURCE_CONSISTENCY_NATIVE_ATOMIC,
        suffix=".md",
    )
    assignment = SecurityDomainRecord(
        record_id=f"assign.{tag}",
        source_ref=source.record_id,
        domain_key=domain,
    )
    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in texts), ()
    )
    view = ViewDocumentRecord(
        record_id=f"view.{tag}",
        content_revision_ref=revision.record_id,
        graph=graph,
        texts=dict(texts),
        view_id=f"doc-{tag}",
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id=WORKSPACE,
            records=(source, revision, assignment, view),
        )
    )
    return view.record_id


def _alpha_texts(tag: str) -> dict[str, str]:
    texts = {
        f"n{i}": f"needle alpha {tag} common ground prose block {i} with "
        f"surrounding authorized filler words {i}"
        for i in range(ALPHA_NODES - 1)
    }
    # One deliberately low-ranked long authorized hit.
    texts[f"n{ALPHA_NODES - 1}"] = (
        "needle appears once here inside a much longer authorized passage "
        "dense with unrelated vocabulary that dilutes every frequent term "
        "and pushes this row down any shared ranking of the corpus"
    )
    return texts


def _beta_texts(tag: str) -> dict[str, str]:
    return {
        f"n{i}": f"needle beta {tag} forbidden {i}" for i in range(ALPHA_NODES)
    }


async def _seed_corpus(service, *, beta_docs: int = BETA_DOCS):
    alpha_ids = []
    for a in range(ALPHA_DOCS):
        alpha_ids.append(
            await _seed_doc(
                service, tag=f"alpha{a}", domain="dom-alpha", texts=_alpha_texts(f"alpha{a}")
            )
        )
    beta_ids = []
    for b in range(beta_docs):
        beta_ids.append(
            await _seed_doc(
                service, tag=f"beta{b}", domain="dom-beta", texts=_beta_texts(f"beta{b}")
            )
        )
    bare = ViewDocumentRecord(
        record_id="view-bare",
        content_revision_ref="rev-bare",
        graph=ReadingOrderGraph.build((OrderNode(node_id="n1"),), ()),
        texts={"n1": "needle unattributed local document"},
        view_id="doc-bare",
    )
    await service.commit(
        KernelCommitBatch(workspace_id=WORKSPACE, records=(bare,))
    )
    return alpha_ids, beta_ids, "view-bare"


def _request(operations: list[dict], **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": WORKSPACE,
        "operations": operations,
    }
    base.update(overrides)
    return base


def _get(record_id: str) -> dict:
    return {"op": "record_get", "record_id": record_id}


def _lexical(text: str, **overrides) -> dict:
    op: dict = {"op": "lexical_search", "text": text}
    op.update(overrides)
    return op


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)

    def pct(fraction: float) -> float:
        index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 3)

    return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}


async def _timed_query(factory, request_data: dict, repeats: int):
    request = parse_query_request(request_data)
    samples: list[float] = []
    packet = None
    for _ in range(repeats):
        started = time.perf_counter()
        packet = await execute_query(factory, request)
        samples.append((time.perf_counter() - started) * 1000.0)
    return packet, {
        "latency_ms": _percentiles(samples),
        "mean_ms": round(statistics.fmean(samples), 3),
    }


async def _fts_beta_rows_still_present(factory, lexical_generation_id: str) -> int:
    table = fts_table_name(lexical_generation_id)
    db_path = Path(factory.kw["bind"].url.database)
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE text LIKE ?', ("%forbidden%",)
            ).fetchone()[0]
        )


async def _locator_count(factory, lexical_generation_id: str) -> int:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(KernelLexicalRow.row_index).where(
                        KernelLexicalRow.lexical_generation_id
                        == lexical_generation_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


async def _publish(factory, *, partition: bool):
    pubs = PublicationService(factory)
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, WORKSPACE)
    )
    shared = await pubs.publish(materialized_generation_id=gen.generation_id)
    part = None
    if partition:
        part = await pubs.publish_high_assurance(
            materialized_generation_id=gen.generation_id,
            partition_domains=frozenset({"dom-alpha"}),
        )
    return pubs, gen, shared, part


async def run(repeats: int) -> dict[str, Any]:
    results: dict[str, Any] = {
        "benchmark": "pr78-authorization-retrieval",
        "query_schema_version": QUERY_SCHEMA_VERSION,
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "repeats_per_query": repeats,
        "method": (
            "deterministic two-domain corpus through the real commit spine "
            "and publication service; acceptance is structural, wall-clock "
            "is characterization evidence only (no constant-time claim)"
        ),
    }
    try:
        results["commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(BACKEND.parent),
        ).stdout.strip()
    except Exception:
        results["commit"] = None

    with tempfile.TemporaryDirectory(prefix="marker-pr78-bench-") as tmp_dir:
        tmp = Path(tmp_dir)
        await upgrade_database(url=f"sqlite+aiosqlite:///{(tmp / 'mig.db').as_posix()}")
        url = f"sqlite+aiosqlite:///{(tmp / 'pr78.db').as_posix()}"
        await upgrade_database(url=url)
        engine = create_async_engine(url, connect_args={"check_same_thread": False})
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        service = KernelCommitService(factory)
        policy = QueryPolicyService(factory, service, workspace_id=WORKSPACE)

        alpha_ids, beta_ids, bare_id = await _seed_corpus(service)
        _pubs, gen1, shared1, part1 = await _publish(factory, partition=True)
        results["corpus"] = {
            "alpha_docs": ALPHA_DOCS,
            "beta_docs": BETA_DOCS,
            "nodes_per_doc": ALPHA_NODES,
            "authorized_domain": "dom-alpha",
            "forbidden_domain": "dom-beta",
            "publication_set_id": shared1.publication_set_id,
            "materialized_generation_id": gen1.generation_id,
            "lexical_generation_id": shared1.lexical_generation_id,
            "high_assurance_profile": part1.profile if part1 else None,
        }

        # -- 1. exact retrieval shape comparison + timings ----------------
        allowed_packet, allowed_timing = await _timed_query(
            factory, _request([_get(alpha_ids[0])]), repeats
        )
        nonexistent_packet, nonexistent_timing = await _timed_query(
            factory, _request([_get("no-such-record")]), repeats
        )
        await policy.deny_record(beta_ids[0], basis={"reason": "bench revoke"})
        unauthorized_packet, unauthorized_timing = await _timed_query(
            factory, _request([_get(beta_ids[0])]), repeats
        )
        template = "record {!r} is not present in the pinned materialized generation"
        shape_identical = (
            unauthorized_packet.omitted[0].reason
            == nonexistent_packet.omitted[0].reason
            == "not_found"
            and unauthorized_packet.omitted[0].detail
            == template.format(beta_ids[0])
            and nonexistent_packet.omitted[0].detail
            == template.format("no-such-record")
            and unauthorized_packet.status == nonexistent_packet.status
        )
        results["exact"] = {
            "allowed_returns_record": len(allowed_packet.evidence) == 1,
            "unauthorized_shape_matches_nonexistent": shape_identical,
            "unauthorized_leaks_no_content": not any(
                "forbidden" in (u.text or "") for u in unauthorized_packet.evidence
            ),
            "timing_ms": {
                "allowed": allowed_timing,
                "unauthorized": unauthorized_timing,
                "nonexistent": nonexistent_timing,
            },
            "timing_note": (
                "unauthorized and nonexistent follow the same code path by "
                "design; residuals are characterized, not claimed zero"
            ),
        }

        # -- 2. lexical authorization-first competition -------------------
        lexical_request = _request([_lexical("needle", limit=200)])
        open_packet, open_timing = await _timed_query(
            factory, lexical_request, repeats
        )
        open_records = {u.locator.record_id for u in open_packet.evidence}
        await policy.deny_domain("dom-beta", basis={"reason": "bench domain deny"})
        denied_packet, denied_timing = await _timed_query(
            factory, lexical_request, repeats
        )
        denied_records = {u.locator.record_id for u in denied_packet.evidence}
        forbidden_leak = any(
            rid in denied_records for rid in beta_ids
        ) or any("forbidden" in (u.text or "") for u in denied_packet.evidence)
        # Authorized recall: every alpha needle node plus the bare view.
        authorized_expected = ALPHA_DOCS * ALPHA_NODES + 1
        results["lexical"] = {
            "open_matches_include_both_domains": (
                any(rid in open_records for rid in beta_ids)
                and any(rid in open_records for rid in alpha_ids)
            ),
            "no_forbidden_units_returned_after_deny": not forbidden_leak,
            "no_forbidden_units_counted_for_caller": not any(
                rid in beta_ids for rid in denied_records
            ),
            "authorized_recall_preserved": (
                len(denied_packet.evidence) == authorized_expected
                and all(rid in denied_records for rid in alpha_ids)
                and bare_id in denied_records
            ),
            "candidates_considered_describe_authorized_universe": (
                denied_packet.budget.candidates_considered == authorized_expected
            ),
            "timing_ms": {"before_deny": open_timing, "after_deny": denied_timing},
        }

        # -- 3. revocation without reindex -------------------------------
        stale_rows = await _fts_beta_rows_still_present(
            factory, shared1.lexical_generation_id
        )
        revoked_exact, _ = await _timed_query(
            factory, _request([_get(beta_ids[0])]), 1
        )
        revoked_lexical, _ = await _timed_query(
            factory, _request([_lexical("forbidden 0")]), 1
        )
        identity_before_deny = open_packet.identity_id
        identity_after_deny = denied_packet.identity_id
        results["revocation"] = {
            "stale_fts_rows_still_present": stale_rows,
            "revocation_effective_without_reindex": (
                stale_rows > 0
                and revoked_exact.evidence == ()
                and revoked_lexical.evidence == ()
            ),
            "packet_identity_changed_on_deny": (
                identity_before_deny != identity_after_deny
            ),
        }

        # -- 4. identity invalidation matrix ------------------------------
        policy_packet, _ = await _timed_query(factory, lexical_request, 1)
        await service.commit(
            KernelCommitBatch(
                workspace_id=WORKSPACE,
                records=(
                    AccessPolicyRevisionRecord(
                        record_id="policy.bench.1",
                        source_ref="src.alpha0",
                        policy_profile="local_v1",
                        policy_facts={"basis": "workspace_roots"},
                    ),
                ),
            )
        )
        after_policy, _ = await _timed_query(factory, lexical_request, 1)
        await service.commit(
            KernelCommitBatch(
                workspace_id=WORKSPACE,
                records=(
                    AuthorizationEpochRecord(
                        record_id="epoch.bench.1",
                        epoch_number=1,
                        fingerprint=_blob(41),
                        domain_facts={"profile": "local_v1"},
                    ),
                ),
            )
        )
        after_epoch, _ = await _timed_query(factory, lexical_request, 1)
        late_view = ViewDocumentRecord(
            record_id="view-late",
            content_revision_ref="rev-late",
            graph=ReadingOrderGraph.build((OrderNode(node_id="n1"),), ()),
            texts={"n1": "late unrelated words"},
            view_id="doc-late",
        )
        await service.commit(
            KernelCommitBatch(workspace_id=WORKSPACE, records=(late_view,))
        )
        after_unrelated, _ = await _timed_query(factory, lexical_request, 1)
        results["identity"] = {
            "stable_under_unrelated_commit": (
                after_unrelated.identity_id == after_epoch.identity_id
            ),
            "changed_on_access_policy_revision": (
                after_policy.identity_id != policy_packet.identity_id
            ),
            "changed_on_authorization_epoch": (
                after_epoch.identity_id != after_policy.identity_id
            ),
            "changed_on_deny_revision": (
                identity_after_deny != identity_before_deny
            ),
        }

        # -- 5. high-assurance rank isolation -----------------------------
        await policy.deny_domain("dom-beta", basis={"reason": "bench ha deny"})
        ha_request = _request([_lexical("needle", limit=200)], assurance="high")
        std_request = _request([_lexical("needle", limit=200)])
        ha_before, _ = await _timed_query(factory, ha_request, 1)
        std_before, _ = await _timed_query(factory, std_request, 1)
        ha_order_before = [(u.locator.record_id, u.locator.node_id) for u in ha_before.evidence]
        ha_ranks_before = [u.rank for u in ha_before.evidence]
        std_ranks_before = [u.rank for u in std_before.evidence]

        # Grow ONLY the forbidden corpus, rebuild derived state, republish.
        for f in range(BETA_FLOOD_DOCS):
            await _seed_doc(
                service, tag=f"flood{f}", domain="dom-beta", texts=_beta_texts(f"flood{f}")
            )
        _pubs2, gen2, shared2, part2 = await _publish(factory, partition=True)
        ha_after, _ = await _timed_query(factory, ha_request, 1)
        std_after, _ = await _timed_query(factory, std_request, 1)
        ha_order_after = [(u.locator.record_id, u.locator.node_id) for u in ha_after.evidence]
        ha_ranks_after = [u.rank for u in ha_after.evidence]
        std_ranks_after = [u.rank for u in std_after.evidence]

        ha_pressure_request = _request(
            [_lexical("needle", limit=1)], assurance="high"
        )
        ha_pressure, _ = await _timed_query(factory, ha_pressure_request, 1)

        # Missing partition fails closed: a fresh unpublished HA profile.
        fail_closed = False
        try:
            await execute_query(
                factory,
                parse_query_request(
                    _request(
                        [_lexical("needle")], assurance="high", profile="default"
                    )
                ),
            )
            await policy.deny_domain("dom-alpha", basis={"reason": "bench move"})
            # dom-alpha denied -> derived partition becomes empty-set,
            # whose profile was never published: must refuse.
            await execute_query(
                factory,
                parse_query_request(
                    _request([_lexical("needle")], assurance="high")
                ),
            )
        except QueryAuthorizationError:
            fail_closed = True
        await policy.allow_domain("dom-alpha", basis={"reason": "bench restore"})

        caller_named_partition_rejected = False
        try:
            parse_query_request(
                _request([_lexical("needle")], profile="ha.0123456789ab")
            )
        except QueryContractError:
            caller_named_partition_rejected = True

        results["high_assurance"] = {
            "partition_profile": part2.profile if part2 else None,
            "single_set_attribution": (
                len({u.locator.publication_set_id for u in ha_after.evidence}) <= 1
                and ha_after.publication["publication_set_id"]
                == part2.publication_set_id
            ),
            "rank_order_invariant_to_forbidden_growth": (
                ha_order_after == ha_order_before
            ),
            "score_basis_invariant_to_forbidden_growth": (
                ha_ranks_after == ha_ranks_before
            ),
            "shared_index_rank_shifted_by_forbidden_growth": (
                std_ranks_after != std_ranks_before
            ),
            "forbidden_top_k_pressure_cannot_evict_allowed_hit": (
                len(ha_pressure.evidence) == 1
                and ha_pressure.evidence[0].locator.record_id in alpha_ids
            ),
            "missing_partition_fails_closed": fail_closed,
            "caller_named_partition_rejected": caller_named_partition_rejected,
        }

        # -- acceptance roll-up -------------------------------------------
        lexical = results["lexical"]
        acceptance = {
            "unauthorized_exact_nondisclosing": (
                results["exact"]["unauthorized_shape_matches_nonexistent"]
                and results["exact"]["unauthorized_leaks_no_content"]
            ),
            "no_forbidden_units_returned": lexical[
                "no_forbidden_units_returned_after_deny"
            ],
            "no_forbidden_units_counted_for_caller": lexical[
                "no_forbidden_units_counted_for_caller"
            ],
            "authorized_recall_preserved": lexical["authorized_recall_preserved"],
            "authorized_only_counts": lexical[
                "candidates_considered_describe_authorized_universe"
            ],
            "revocation_effective_without_reindex": results["revocation"][
                "revocation_effective_without_reindex"
            ],
            "packet_identity_changed_on_policy_revision": results["identity"][
                "changed_on_access_policy_revision"
            ],
            "packet_identity_changed_on_authorization_epoch": results["identity"][
                "changed_on_authorization_epoch"
            ],
            "packet_identity_changed_on_deny_revision": results["identity"][
                "changed_on_deny_revision"
            ],
            "packet_identity_stable_under_unrelated_commit": results["identity"][
                "stable_under_unrelated_commit"
            ],
            "high_assurance_rank_invariant": results["high_assurance"][
                "rank_order_invariant_to_forbidden_growth"
            ],
            "high_assurance_score_basis_invariant": results["high_assurance"][
                "score_basis_invariant_to_forbidden_growth"
            ],
            "high_assurance_top_k_invariant": results["high_assurance"][
                "forbidden_top_k_pressure_cannot_evict_allowed_hit"
            ],
            "missing_partition_fails_closed": results["high_assurance"][
                "missing_partition_fails_closed"
            ],
            "caller_cannot_name_partition": results["high_assurance"][
                "caller_named_partition_rejected"
            ],
            "single_publication_attribution": results["high_assurance"][
                "single_set_attribution"
            ],
            "residual_risk_note": (
                "standard mode serves from the shared index: bm25 values a "
                "caller sees can shift when hidden-domain content changes "
                "(declared residual); high assurance removes the channel by "
                "physical corpus isolation. Timing residuals on a shared "
                "machine are characterized, not claimed constant-time."
            ),
        }
        results["acceptance"] = acceptance
        await engine.dispose()

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the JSON artifact")
    parser.add_argument("--repeat", type=int, default=REPEATS, help="repeats per query")
    parser.add_argument("--output", type=Path, default=MEASUREMENTS_PATH)
    args = parser.parse_args(argv)

    report = asyncio.run(run(args.repeat))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(rendered)
    ok = all(
        value is True
        for key, value in report["acceptance"].items()
        if isinstance(value, bool)
    )
    if not ok:
        print("ACCEPTANCE FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
