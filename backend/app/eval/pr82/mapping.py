"""Adversarial cross-revision mapping corpus and evaluator (PR82A).

Twelve revision-pair fixtures covering the hardest identity cases from
the PR82A plan (section 8): unchanged content under irrelevant edits,
position-shifting insertions, moved targets, duplicate text, slight
edits, paraphrase, deletion, split/merge, table edits, geometry/layout
change, policy-only revisions, and multi-candidate ambiguity.

The evaluator answers preregistered questions Q1 (citations move
without false identity claims) and Q2 (similarity never promotes to
exact identity). A corpus run that reports zero violations is only
trusted because the evaluator also proves it can fail: the test suite
feeds it corrupted expectations and requires violations to be reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.kernel.anchor_mapping import (
    MAPPING_DISPOSITION_EXACT,
    MAPPING_DISPOSITION_MAPPED_DETERMINISTIC,
    MAPPING_DISPOSITION_MAPPED_REVIEWED,
    MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
    MAPPING_DISPOSITION_STALE,
    MAPPING_DISPOSITION_UNRESOLVED,
    SourceAnchorMappingRecord,
    map_anchor,
)
from app.kernel.anchors import (
    COORDINATE_SPACE_OFFICE_EMU,
    COORDINATE_SPACE_PDF_PAGE_POINTS,
    GeometrySelector,
    NativeSelector,
    PositionSelector,
    SourceAnchorRecord,
    TextQuoteSelector,
)
from app.utils.canonical import CanonicalBox

SOURCE_REV = "content.rev-n"
TARGET_REV = "content.rev-n1"

PDF_BOX = GeometrySelector(
    geometry=CanonicalBox.from_bbox([72, 640, 272, 660]),
    space=COORDINATE_SPACE_PDF_PAGE_POINTS,
    boundary_convention="region_inclusive",
)
PDF_BOX_MOVED = GeometrySelector(
    geometry=CanonicalBox.from_bbox([72, 540, 272, 560]),
    space=COORDINATE_SPACE_PDF_PAGE_POINTS,
    boundary_convention="region_inclusive",
)
EMU_BOX = GeometrySelector(
    geometry=CanonicalBox.from_bbox([72000, 640000, 272000, 660000]),
    space=COORDINATE_SPACE_OFFICE_EMU,
    boundary_convention="region_inclusive",
)

BOOKMARK_TOTAL = NativeSelector(
    provider="ooxml",
    native_kind="bookmark",
    native_id="bm-total",
    package_path="word/document.xml",
)
BOOKMARK_TOTAL_REMINTED = NativeSelector(
    provider="ooxml",
    native_kind="bookmark",
    native_id="bm-total-2",
    package_path="word/document.xml",
)


def _anchor(
    revision: str,
    *,
    anchor_id: str = "anchor",
    locator: str | None = "pdf:page:1",
    **selectors,
) -> SourceAnchorRecord:
    if not selectors:
        raise ValueError("corpus anchors always carry explicit selectors")
    return SourceAnchorRecord(
        record_id=anchor_id,
        content_revision_ref=revision,
        locator=locator,
        selectors=dict(selectors),
    )


@dataclass(frozen=True)
class MappingCase:
    """One adversarial revision pair with pre-declared expectations."""

    case_id: str
    description: str
    attack: str
    old: SourceAnchorRecord
    new_anchors: tuple[SourceAnchorRecord, ...]
    expected_dispositions: frozenset[str]
    #: True when exact/mapped_deterministic must never appear (Q2).
    exact_forbidden: bool
    #: Policy-only revision pairs must be refused outright.
    expect_refused: bool = False
    source_revision_ref: str = SOURCE_REV
    target_revision_ref: str = TARGET_REV


@dataclass
class CaseResult:
    case_id: str
    disposition: str | None
    rule_id: str | None
    refused: bool
    violations: tuple[str, ...] = ()
    outcome: Any = None


@dataclass
class MappingCorpusResult:
    results: tuple[CaseResult, ...]
    replay_stable: bool
    violations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for result in self.results:
            key = "refused" if result.refused else (result.disposition or "none")
            counts[key] = counts.get(key, 0) + 1
        return {
            "cases": self.case_count,
            "disposition_counts": dict(sorted(counts.items())),
            "violations": list(self.violations),
            "replay_stable": self.replay_stable,
        }


def _case(
    case_id: str,
    description: str,
    attack: str,
    old: SourceAnchorRecord,
    new_anchors: tuple[SourceAnchorRecord, ...],
    expected: frozenset[str],
    *,
    exact_forbidden: bool = False,
    expect_refused: bool = False,
) -> MappingCase:
    return MappingCase(
        case_id=case_id,
        description=description,
        attack=attack,
        old=old,
        new_anchors=new_anchors,
        expected_dispositions=expected,
        exact_forbidden=exact_forbidden,
        expect_refused=expect_refused,
    )


_EX = frozenset({MAPPING_DISPOSITION_EXACT})
_DET = frozenset({MAPPING_DISPOSITION_MAPPED_DETERMINISTIC})
_CAND = frozenset({MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE})
_STALE = frozenset({MAPPING_DISPOSITION_STALE})
_UNRES = frozenset({MAPPING_DISPOSITION_UNRESOLVED})
_DET_OR_CAND = _DET | _CAND
_CAND_OR_STALE = _CAND | _STALE


def build_mapping_corpus() -> tuple[MappingCase, ...]:
    """The frozen adversarial corpus (twelve cases, plan section 8)."""
    return (
        _case(
            "unchanged-surrounding-edits",
            "Target text untouched while surrounding regions receive edits",
            "irrelevant edits must not break or forge identity",
            _anchor(SOURCE_REV, quote=TextQuoteSelector(quote="Quarterly revenue was 4.2m")),
            (
                _anchor(TARGET_REV, anchor_id="a-new-intro", locator="pdf:page:1", quote=TextQuoteSelector(quote="Rewritten introduction")),
                _anchor(TARGET_REV, anchor_id="a-target", locator="pdf:page:2", quote=TextQuoteSelector(quote="Quarterly revenue was 4.2m")),
                _anchor(TARGET_REV, anchor_id="a-new-outro", locator="pdf:page:9", quote=TextQuoteSelector(quote="Appended conclusions")),
            ),
            _DET,
        ),
        _case(
            "insertion-shifts-positions",
            "A large insertion before the target shifts positional facts",
            "position shifts must not silently remap positional anchors",
            _anchor(
                SOURCE_REV,
                locator=None,
                position=PositionSelector(scope="content_bytes", start=120, end=160),
                quote=TextQuoteSelector(quote="Stable paragraph under insertion pressure"),
            ),
            (
                _anchor(TARGET_REV, anchor_id="a-inserted", locator="pdf:page:1", quote=TextQuoteSelector(quote="Large inserted preface block")),
                _anchor(TARGET_REV, anchor_id="a-target", locator="pdf:page:2", quote=TextQuoteSelector(quote="Stable paragraph under insertion pressure")),
            ),
            _DET,
        ),
        _case(
            "target-moved-pages",
            "Target moved to another page of the same document",
            "relocation must map via content, not location",
            _anchor(SOURCE_REV, locator="pdf:page:2", quote=TextQuoteSelector(quote="Relocated methodology section")),
            (
                _anchor(TARGET_REV, anchor_id="a-target", locator="pdf:page:17", quote=TextQuoteSelector(quote="Relocated methodology section")),
            ),
            _DET,
        ),
        _case(
            "duplicate-identical-text",
            "Identical text appears twice; only one instance is the true continuation",
            "ambiguity must stay a ranked candidate, never an arbitrary pick",
            _anchor(SOURCE_REV, locator="pdf:page:4", quote=TextQuoteSelector(quote="Standard disclaimer text")),
            (
                _anchor(TARGET_REV, anchor_id="a-dup-1", locator="pdf:page:4", quote=TextQuoteSelector(quote="Standard disclaimer text")),
                _anchor(TARGET_REV, anchor_id="a-dup-2", locator="pdf:page:11", quote=TextQuoteSelector(quote="Standard disclaimer text")),
            ),
            _CAND,
            exact_forbidden=True,
        ),
        _case(
            "slight-edit-meaning-preserved",
            "One digit edited; meaning preserved",
            "near text must not claim deterministic identity",
            _anchor(SOURCE_REV, quote=TextQuoteSelector(quote="Growth was 12 percent year over year")),
            (
                _anchor(TARGET_REV, quote=TextQuoteSelector(quote="Growth was 13 percent year over year")),
            ),
            _CAND,
            exact_forbidden=True,
        ),
        _case(
            "paraphrase-not-exact",
            "Text fully reworded with the same meaning",
            "semantic similarity must never become exact identity",
            _anchor(SOURCE_REV, quote=TextQuoteSelector(quote="The quick brown fox jumps over the lazy dog")),
            (
                _anchor(TARGET_REV, quote=TextQuoteSelector(quote="A swift auburn fox leaps above the idle hound")),
            ),
            _CAND_OR_STALE,
            exact_forbidden=True,
        ),
        _case(
            "deleted-target",
            "Target paragraph deleted outright",
            "deleted targets become stale, never reassigned",
            _anchor(SOURCE_REV, quote=TextQuoteSelector(quote="Deprecated budget narrative that is now gone")),
            (
                _anchor(TARGET_REV, quote=TextQuoteSelector(quote="Replacement narrative about forecasts")),
            ),
            _STALE,
            exact_forbidden=True,
        ),
        _case(
            "merged-region",
            "Two regions merged; old quote is a prefix of the merged quote",
            "split/merge stays a candidate (containment evidence)",
            _anchor(SOURCE_REV, quote=TextQuoteSelector(quote="Liabilities discussion paragraph")),
            (
                _anchor(TARGET_REV, quote=TextQuoteSelector(quote="Liabilities discussion paragraph together with new hedging notes")),
            ),
            _CAND,
            exact_forbidden=True,
        ),
        _case(
            "table-value-edit",
            "Table row keeps its bookmark but a cell value changed",
            "native identity survives value edits as exact (identity is the row, not the value)",
            _anchor(
                SOURCE_REV,
                native=BOOKMARK_TOTAL,
                quote=TextQuoteSelector(quote="Total 1,234"),
            ),
            (
                _anchor(
                    TARGET_REV,
                    native=BOOKMARK_TOTAL,
                    quote=TextQuoteSelector(quote="Total 1,235"),
                ),
            ),
            _EX,
        ),
        _case(
            "table-row-moved",
            "Table row moved to another sheet location; native id reminted",
            "a reminted native id must fall back to quote evidence",
            _anchor(
                SOURCE_REV,
                native=BOOKMARK_TOTAL_REMINTED,
                quote=TextQuoteSelector(quote="Row total 42"),
            ),
            (
                _anchor(TARGET_REV, locator="ooxml:sheet:2", quote=TextQuoteSelector(quote="Row total 42")),
            ),
            _DET,
        ),
        _case(
            "geometry-render-drift",
            "Rerender shifts geometry; quote evidence absent",
            "geometry alone can never prove cross-revision identity",
            _anchor(SOURCE_REV, geometry=PDF_BOX),
            (
                _anchor(TARGET_REV, geometry=PDF_BOX_MOVED),
                _anchor(TARGET_REV, anchor_id="a-emu", locator="ooxml:word/document.xml", geometry=EMU_BOX),
            ),
            _UNRES,
            exact_forbidden=True,
        ),
        _case(
            "policy-only-revision",
            "Revision changes access policy only; content identity unchanged",
            "ACL-only changes must not remint anchors or mint mappings",
            _anchor(SOURCE_REV, quote=TextQuoteSelector(quote="Unchanged body text")),
            (
                _anchor(SOURCE_REV, quote=TextQuoteSelector(quote="Unchanged body text")),
            ),
            frozenset(),
            exact_forbidden=True,
            expect_refused=True,
        ),
    )


def evaluate_mapping_corpus(
    corpus: tuple[MappingCase, ...] | None = None,
) -> MappingCorpusResult:
    """Run the corpus and collect honest violations (Q1/Q2 evidence)."""
    cases = corpus if corpus is not None else build_mapping_corpus()
    results: list[CaseResult] = []
    violations: list[str] = []

    for case in cases:
        result = _run_case(case)
        results.append(result)
        violations.extend(result.violations)

    replay_stable = _replay_check(cases)
    if not replay_stable:
        violations.append("replay: mapping identities differ across identical reruns")

    return MappingCorpusResult(
        results=tuple(results),
        replay_stable=replay_stable,
        violations=tuple(violations),
    )


def _run_case(case: MappingCase) -> CaseResult:
    if case.expect_refused:
        try:
            map_anchor(
                case.old,
                case.new_anchors,
                source_revision_ref=case.source_revision_ref,
                target_revision_ref=case.target_revision_ref,
            )
        except Exception:
            return CaseResult(
                case_id=case.case_id, disposition=None, rule_id=None, refused=True
            )
        return CaseResult(
            case_id=case.case_id,
            disposition=None,
            rule_id=None,
            refused=False,
            violations=(f"{case.case_id}: policy-only revision pair was not refused",),
        )

    outcome = map_anchor(
        case.old,
        case.new_anchors,
        source_revision_ref=case.source_revision_ref,
        target_revision_ref=case.target_revision_ref,
    )
    violations: list[str] = []
    if outcome.disposition not in case.expected_dispositions:
        violations.append(
            f"{case.case_id}: disposition {outcome.disposition!r} outside expected "
            f"{sorted(case.expected_dispositions)}"
        )
    if case.exact_forbidden and outcome.disposition in (
        MAPPING_DISPOSITION_EXACT,
        MAPPING_DISPOSITION_MAPPED_DETERMINISTIC,
        MAPPING_DISPOSITION_MAPPED_REVIEWED,
    ):
        violations.append(
            f"{case.case_id}: similarity/deletion evidence promoted to "
            f"{outcome.disposition!r}"
        )
    # Q1 core: identity never silently moves — the mapped target is
    # always a *new* anchor id bound to the new revision.
    if outcome.target_anchor_id is not None:
        target_ids = {anchor.anchor_id() for anchor in case.new_anchors}
        if outcome.target_anchor_id not in target_ids:
            violations.append(
                f"{case.case_id}: target {outcome.target_anchor_id!r} is not an "
                "anchor of the new revision"
            )
        if outcome.target_anchor_id == case.old.anchor_id():
            violations.append(f"{case.case_id}: target equals the source anchor id")
    return CaseResult(
        case_id=case.case_id,
        disposition=outcome.disposition,
        rule_id=outcome.rule_id,
        refused=False,
        violations=tuple(violations),
        outcome=outcome,
    )


def _replay_check(cases: tuple[MappingCase, ...]) -> bool:
    """Deterministic replay: two independent runs mint identical ids."""
    for case in cases:
        if case.expect_refused:
            continue
        first = map_anchor(
            case.old,
            case.new_anchors,
            source_revision_ref=case.source_revision_ref,
            target_revision_ref=case.target_revision_ref,
        )
        second = map_anchor(
            case.old,
            tuple(reversed(case.new_anchors)),
            source_revision_ref=case.source_revision_ref,
            target_revision_ref=case.target_revision_ref,
        )
        record_a = SourceAnchorMappingRecord.from_outcome(
            first,
            source_revision_ref=case.source_revision_ref,
            target_revision_ref=case.target_revision_ref,
            source_anchor_id=case.old.anchor_id(),
        )
        record_b = SourceAnchorMappingRecord.from_outcome(
            second,
            source_revision_ref=case.source_revision_ref,
            target_revision_ref=case.target_revision_ref,
            source_anchor_id=case.old.anchor_id(),
        )
        if record_a.mapping_id() != record_b.mapping_id():
            return False
    return True
