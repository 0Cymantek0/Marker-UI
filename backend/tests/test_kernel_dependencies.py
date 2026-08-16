"""PR73 dependency completeness & conservative invalidation tests."""

from __future__ import annotations

import pytest

from app.kernel.dependencies import (
    COMPLETENESS_CONSERVATIVE_SCOPE,
    COMPLETENESS_EXACT_NATIVE,
    COMPLETENESS_EXACT_OPERATOR,
    COMPLETENESS_SEMANTIC_CANDIDATE,
    DependencyDeclarationRecord,
    DependencyInput,
    compute_invalidation,
)
from app.kernel.errors import KernelError


def declaration(
    subject: str,
    inputs,
    *,
    operator="derived.renderer",
    operator_version="1.0.0",
    scope=None,
    record_id="decl-1",
) -> DependencyDeclarationRecord:
    return DependencyDeclarationRecord(
        record_id=record_id,
        subject_ref=subject,
        inputs=tuple(inputs),
        scope_ref=scope,
        operator=operator,
        operator_version=operator_version,
    )


def tracer_declarations() -> list[DependencyDeclarationRecord]:
    """The tracer dependency graph used across these tests.

    * pdf-run-a / pdf-run-b: exact_native per-node derived values;
    * office-summary: exact_operator over a declared input set;
    * doc-summary: conservative over the whole document scope;
    * similar-docs: semantic_candidate recall hint.
    """
    return [
        declaration(
            "derived:pdf-run-a",
            [DependencyInput("fact:pdf-run-a", COMPLETENESS_EXACT_NATIVE)],
            record_id="decl-pdf-a",
        ),
        declaration(
            "derived:pdf-run-b",
            [DependencyInput("fact:pdf-run-b", COMPLETENESS_EXACT_NATIVE)],
            record_id="decl-pdf-b",
        ),
        declaration(
            "derived:office-summary",
            [
                DependencyInput("fact:docx-bookmark-1", COMPLETENESS_EXACT_OPERATOR),
                DependencyInput("fact:docx-bookmark-2", COMPLETENESS_EXACT_OPERATOR),
            ],
            operator="office.summarizer",
            record_id="decl-office",
        ),
        declaration(
            "derived:doc-summary",
            [
                DependencyInput("anything:in-document", COMPLETENESS_CONSERVATIVE_SCOPE)
            ],
            scope="document",
            operator="doc.summarizer",
            record_id="decl-doc",
        ),
        declaration(
            "recall:similar-docs",
            [DependencyInput("fact:pdf-run-a", COMPLETENESS_SEMANTIC_CANDIDATE)],
            operator="recall.similarity",
            record_id="decl-similar",
        ),
    ]


# ---------------------------------------------------------------------------
# record contract
# ---------------------------------------------------------------------------


def test_declaration_identity_order_insensitive_and_version_sensitive():
    a = declaration(
        "derived:x",
        [
            DependencyInput("fact:1", COMPLETENESS_EXACT_NATIVE),
            DependencyInput("fact:2", COMPLETENESS_EXACT_OPERATOR),
        ],
    )
    b = declaration(
        "derived:x",
        [
            DependencyInput("fact:2", COMPLETENESS_EXACT_OPERATOR),
            DependencyInput("fact:1", COMPLETENESS_EXACT_NATIVE),
        ],
        record_id="decl-2",
    )
    assert a.declaration_id() == b.declaration_id()
    bumped = declaration(
        "derived:x",
        [
            DependencyInput("fact:1", COMPLETENESS_EXACT_NATIVE),
            DependencyInput("fact:2", COMPLETENESS_EXACT_OPERATOR),
        ],
        operator_version="2.0.0",
    )
    assert bumped.declaration_id() != a.declaration_id()
    remat = DependencyDeclarationRecord.from_payload(
        a.identity_payload(), record_id="decl-3"
    )
    assert remat.declaration_id() == a.declaration_id()


def test_declaration_fails_closed():
    with pytest.raises(KernelError, match="unknown completeness"):
        declaration("derived:x", [DependencyInput("fact:1", "vibes")])
    with pytest.raises(KernelError, match="duplicate input"):
        declaration(
            "derived:x",
            [
                DependencyInput("fact:1", COMPLETENESS_EXACT_NATIVE),
                DependencyInput("fact:1", COMPLETENESS_EXACT_OPERATOR),
            ],
        )
    with pytest.raises(KernelError, match="scope_ref"):
        declaration(
            "derived:x",
            [DependencyInput("fact:1", COMPLETENESS_CONSERVATIVE_SCOPE)],
        )
    with pytest.raises(KernelError, match="unknown declaration payload fields"):
        DependencyDeclarationRecord.from_payload(
            {**declaration("derived:x", []).identity_payload(), "oops": 1},
            record_id="decl-x",
        )


# ---------------------------------------------------------------------------
# invalidation semantics
# ---------------------------------------------------------------------------


def test_exact_native_change_localizes_invalidation():
    result = compute_invalidation(["fact:pdf-run-a"], tracer_declarations())
    assert result.invalidated == frozenset({"derived:pdf-run-a"})
    assert result.widened is False
    assert result.pending_inputs["derived:pdf-run-a"] == frozenset({"fact:pdf-run-a"})
    assert result.reasons["derived:pdf-run-a"] == ("exact",)


def test_unrelated_exact_change_invalidates_nothing_else():
    result = compute_invalidation(["fact:pdf-run-b"], tracer_declarations())
    assert result.invalidated == frozenset({"derived:pdf-run-b"})


def test_multi_input_subject_pending_until_all_reconciled():
    result = compute_invalidation(["fact:docx-bookmark-1"], tracer_declarations())
    assert result.invalidated == frozenset({"derived:office-summary"})
    # Only the changed input is pending; the untouched input stays valid.
    assert result.pending_inputs["derived:office-summary"] == frozenset(
        {"fact:docx-bookmark-1"}
    )


def test_conservative_input_change_widens_whole_scope():
    result = compute_invalidation(["anything:in-document"], tracer_declarations())
    assert result.widened is True
    assert result.widened_scopes == frozenset({"document"})
    assert "derived:doc-summary" in result.invalidated
    assert result.reasons["derived:doc-summary"] == ("conservative_scope",)


def test_unknown_change_widens_every_conservative_boundary():
    # No exact declaration anywhere covers this input: correctness
    # knowledge is absent, so every conservative scope invalidates.
    result = compute_invalidation(["fact:nobody-knows"], tracer_declarations())
    assert result.uncovered_changes == frozenset({"fact:nobody-knows"})
    assert result.widened is True
    assert result.invalidated == frozenset({"derived:doc-summary"})
    assert result.reasons["derived:doc-summary"] == ("conservative_scope",)


def test_semantic_candidate_never_narrows_or_corrects():
    # The change IS semantically similar to recall:similar-docs, and IS
    # exactly known for derived:pdf-run-a.
    result = compute_invalidation(["fact:pdf-run-a"], tracer_declarations())
    assert result.recall_candidates == frozenset({"recall:similar-docs"})
    assert "recall:similar-docs" not in result.invalidated

    # A change covered ONLY by a semantic edge is still an uncovered
    # correctness change: semantic knowledge cannot narrow or localize.
    semantic_only = [
        declaration(
            "recall:similar",
            [DependencyInput("fact:only-semantic", COMPLETENESS_SEMANTIC_CANDIDATE)],
            operator="recall.similarity",
        ),
        declaration(
            "derived:scoped",
            [DependencyInput("fact:scoped-input", COMPLETENESS_CONSERVATIVE_SCOPE)],
            scope="document",
            operator="doc.summarizer",
        ),
    ]
    result = compute_invalidation(["fact:only-semantic"], semantic_only)
    assert "recall:similar" not in result.invalidated
    assert result.uncovered_changes == frozenset({"fact:only-semantic"})
    assert result.widened is True
    assert "derived:scoped" in result.invalidated


def test_no_changes_no_invalidation():
    result = compute_invalidation([], tracer_declarations())
    assert result.invalidated == frozenset()
    assert result.widened is False
    assert result.recall_candidates == frozenset()


def test_explain_reports_exact_vs_conservate_truthfully():
    result = compute_invalidation(
        ["fact:pdf-run-a", "anything:in-document"], tracer_declarations()
    )
    explanation = result.explain()
    assert explanation["reasons"]["derived:pdf-run-a"] == ["exact"]
    assert explanation["reasons"]["derived:doc-summary"] == ["conservative_scope"]
    assert explanation["widened"] is True
    assert explanation["widened_scopes"] == ["document"]


@pytest.mark.asyncio
async def test_declarations_are_durable_kernel_records(kernel_env):
    from json import loads

    from sqlalchemy import select

    from app.kernel.commit import KernelCommitBatch, KernelCommitService
    from app.kernel.models import KernelRecord

    decl = tracer_declarations()[0]
    service = KernelCommitService(kernel_env)
    await service.commit(
        KernelCommitBatch(workspace_id="ws-deps", records=(decl,))
    )
    async with kernel_env() as session:
        row = (
            await session.execute(
                select(KernelRecord).where(
                    KernelRecord.workspace_id == "ws-deps",
                    KernelRecord.record_class == "dependency_declaration",
                )
            )
        ).scalar_one()
        assert row.identity_hash == decl.declaration_id()
        remat = DependencyDeclarationRecord.from_payload(
            loads(row.payload_json), record_id=row.id
        )
        assert remat.declaration_id() == decl.declaration_id()
        # The invalidation contract runs identically over rematerialized
        # declarations: restart changes nothing about scope decisions.
        assert compute_invalidation(
            ["fact:pdf-run-a"], [remat]
        ).invalidated == frozenset({"derived:pdf-run-a"})
