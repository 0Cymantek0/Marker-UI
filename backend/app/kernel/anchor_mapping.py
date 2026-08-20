"""Cross-revision source-anchor mapping contract (PR82A).

PR72 deliberately deferred the cross-revision mapping cascade and mapping
decisions. This module closes the minimal contract required by the V3.2
readiness gates:

* an anchor from source revision ``N`` receives an explicit, auditable
  disposition when revision ``N+1`` appears;
* exact identity is a high bar: only authoritative native-selector
  agreement can claim ``exact`` — paraphrases, near text, embedding
  neighbours and geometry can never promote themselves to exact;
* deterministic mappings are explainable: rule id, rule version, feature
  evidence and a canonical-decimal confidence travel inside the record;
* ambiguity is preserved: multiple plausible targets stay ranked
  ``mapped_semantic_candidate`` entries, never an arbitrary pick;
* ACL-only changes cannot remint content identity: mapping across equal
  content revisions is refused outright;
* failure is an allowed outcome: ``stale`` / ``unresolved`` are honest
  dispositions, not error paths.

Dispositions (closed vocabulary, masterplan 8C.6):

``exact``
    The same authoritative native identity carries into the new revision.
``mapped_deterministic``
    A declared deterministic rule (byte-exact unique quote with compatible
    context) maps the anchor; replay converges.
``mapped_reviewed``
    A semantic candidate promoted by an explicit human mapping decision.
    Only :class:`AnchorMappingDecisionRecord` with ``human_review`` can
    mint this disposition — the cascade never does.
``mapped_semantic_candidate``
    Plausible targets exist (duplicates, normalized/fuzzy/partial text
    agreement, geometry agreement) but identity is not provable.
``stale``
    The anchor carried usable quote/native evidence and no plausible
    target exists in the new revision (deletion or rewrite).
``unresolved``
    The anchor's selectors cannot support cross-revision mapping at all
    (positional/geometry-only without corroborating evidence).

Mapping decisions (masterplan 8C.7): the effective public mapping of an
anchor changes only through a new :class:`AnchorMappingDecisionRecord`;
mapping records themselves are append-only evidence, so historical
citations remain inspectable and re-running the same mapping on frozen
inputs converges on identical record identities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, ClassVar, Mapping, Sequence

from app.kernel.errors import KernelError
from app.kernel.records import KernelRecord, validate_record_ref
from app.utils.canonical import DecimalValue, record_identity_hash, to_json_ready

# ---------------------------------------------------------------------------
# Dispositions and rules
# ---------------------------------------------------------------------------

MAPPING_DISPOSITION_EXACT = "exact"
MAPPING_DISPOSITION_MAPPED_DETERMINISTIC = "mapped_deterministic"
MAPPING_DISPOSITION_MAPPED_REVIEWED = "mapped_reviewed"
MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE = "mapped_semantic_candidate"
MAPPING_DISPOSITION_STALE = "stale"
MAPPING_DISPOSITION_UNRESOLVED = "unresolved"

#: Closed disposition vocabulary. Anything else fails closed.
MAPPING_DISPOSITIONS: frozenset[str] = frozenset(
    {
        MAPPING_DISPOSITION_EXACT,
        MAPPING_DISPOSITION_MAPPED_DETERMINISTIC,
        MAPPING_DISPOSITION_MAPPED_REVIEWED,
        MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
        MAPPING_DISPOSITION_STALE,
        MAPPING_DISPOSITION_UNRESOLVED,
    }
)

#: Dispositions the deterministic cascade itself may mint. ``exact`` and
#: ``mapped_deterministic`` are machine-provable; ``stale`` /
#: ``unresolved`` / ``mapped_semantic_candidate`` are honest
#: non-promotions. ``mapped_reviewed`` is decision-only by construction.
CASCADE_DISPOSITIONS: frozenset[str] = MAPPING_DISPOSITIONS - {
    MAPPING_DISPOSITION_MAPPED_REVIEWED
}

RULE_NATIVE_IDENTITY = "native_identity_v1"
RULE_QUOTE_UNIQUE = "quote_unique_v1"
RULE_QUOTE_DUPLICATES = "quote_duplicates_v1"
RULE_QUOTE_NORMALIZED = "quote_normalized_v1"
RULE_QUOTE_FUZZY = "quote_fuzzy_v1"
RULE_QUOTE_PARTIAL = "quote_partial_v1"
RULE_GEOMETRY_APPROXIMATE = "geometry_approximate_v1"
RULE_NO_MATCH = "no_match_v1"
RULE_INSUFFICIENT_EVIDENCE = "insufficient_evidence_v1"

#: Version of the cascade implemented here. A change to any rule's
#: semantics is a new version — frozen inputs must always map identically
#: under a recorded (rule_id, rule_version) pair.
ANCHOR_MAPPING_CASCADE_VERSION = "1.0.0"

#: Deterministic similarity threshold for the fuzzy-quote candidate rule.
#: Below this, edited text is treated as a rewrite (stale), not a
#: candidate — the number is deliberately conservative.
_FUZZY_CANDIDATE_THRESHOLD = 0.8

_RULE_CONFIDENCE: dict[str, str] = {
    RULE_NATIVE_IDENTITY: "1",
    RULE_QUOTE_UNIQUE: "0.9",
    RULE_QUOTE_DUPLICATES: "0.5",
    RULE_QUOTE_NORMALIZED: "0.5",
    RULE_QUOTE_FUZZY: "0.4",
    RULE_QUOTE_PARTIAL: "0.4",
    RULE_GEOMETRY_APPROXIMATE: "0.3",
    RULE_NO_MATCH: "0",
    RULE_INSUFFICIENT_EVIDENCE: "0",
}

_REVISION_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


def _validate_revision_ref(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not _REVISION_REF_PATTERN.match(value):
        raise KernelError(
            f"invalid {field_name}: {value!r} must match {_REVISION_REF_PATTERN.pattern}"
        )
    return value


# ---------------------------------------------------------------------------
# Cascade outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorMappingOutcome:
    """Pure cascade result for one anchor across one revision pair.

    Deterministic function of frozen inputs: identical anchor sets
    (any order) always produce an identical outcome.
    """

    disposition: str
    rule_id: str
    confidence: DecimalValue
    target_anchor_id: str | None = None
    #: Ranked candidate anchor ids (best first); ordering is a recorded
    #: deterministic ranking, never a resolution.
    candidates: tuple[str, ...] = ()
    reason: str = ""
    #: Feature basis: what the rule actually compared, per target.
    rule_evidence: Mapping[str, Any] = field(default_factory=dict)


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _context_compatible(old: Any, new: Any) -> bool:
    """Context agreement check for the unique-quote rule.

    Equal contexts agree; a context dropped by the new producer agrees
    (editors trim context); contradictory non-empty contexts do not.
    """
    old_text = (old or "").strip()
    new_text = (new or "").strip()
    if not old_text or not new_text:
        return True
    return _normalize_text(old_text) == _normalize_text(new_text)


def _native_selector(anchor: Any) -> Any:
    return anchor.selectors.get("native")


def _quote_selector(anchor: Any) -> Any:
    return anchor.selectors.get("quote")


def _candidate_rank_key(anchor: Any, old: Any) -> tuple[int, int, int, str]:
    """Deterministic candidate ranking: features first, id last.

    Higher feature agreement sorts first; the anchor id is the stable
    tiebreak so frozen inputs always yield the same order regardless of
    input sequence order.
    """
    quote = _quote_selector(anchor)
    old_quote = _quote_selector(old)
    locator_agrees = int(
        old.locator is not None and anchor.locator == old.locator
    )
    quote_equal = int(
        quote is not None and old_quote is not None and quote.quote == old_quote.quote
    )
    context_agrees = int(
        quote is not None
        and old_quote is not None
        and _context_compatible(old_quote.prefix, quote.prefix)
        and _context_compatible(old_quote.suffix, quote.suffix)
    )
    return (-locator_agrees, -quote_equal, -context_agrees, anchor.anchor_id())


def _ranked(anchor_ids: Sequence[str], by_id: Mapping[str, Any], old: Any) -> tuple[str, ...]:
    ordered = sorted(
        anchor_ids,
        key=lambda anchor_id: _candidate_rank_key(by_id[anchor_id], old),
    )
    return tuple(ordered)


def map_anchor(
    old: Any,
    new_anchors: Sequence[Any],
    *,
    source_revision_ref: str,
    target_revision_ref: str,
) -> AnchorMappingOutcome:
    """Run the deterministic cross-revision mapping cascade.

    ``old`` is a :class:`~app.kernel.anchors.SourceAnchorRecord` bound to
    ``source_revision_ref``; every anchor in ``new_anchors`` must be bound
    to ``target_revision_ref``. Mapping a revision onto itself is refused:
    an ACL-only change remints authorization state, never content
    identity, so there is nothing to map.
    """
    _validate_revision_ref(source_revision_ref, field_name="source_revision_ref")
    _validate_revision_ref(target_revision_ref, field_name="target_revision_ref")
    if source_revision_ref == target_revision_ref:
        raise KernelError(
            "cannot map a content revision onto itself: equal content "
            "revisions mean unchanged content identity (an ACL-only "
            "change remints no anchors)"
        )
    if old.content_revision_ref != source_revision_ref:
        raise KernelError(
            "source anchor is not bound to the declared source revision: "
            f"{old.content_revision_ref!r} != {source_revision_ref!r}"
        )
    by_id: dict[str, Any] = {}
    for anchor in new_anchors:
        if anchor.content_revision_ref != target_revision_ref:
            raise KernelError(
                "new-revision anchor is not bound to the declared target "
                f"revision: {anchor.content_revision_ref!r} != "
                f"{target_revision_ref!r}"
            )
        by_id[anchor.anchor_id()] = anchor

    old_native = _native_selector(old)
    old_quote = _quote_selector(old)

    # Rule 1 — authoritative native identity: the same provider object
    # (bookmark, run, drawing) exists in the new revision. This is the
    # only rule that can mint ``exact``; text edits under a preserved
    # native id stay exact because native identity, not text, is the
    # authoritative address.
    if old_native is not None:
        for anchor_id, anchor in sorted(by_id.items()):
            new_native = _native_selector(anchor)
            if new_native is None:
                continue
            if (
                new_native.provider == old_native.provider
                and new_native.native_kind == old_native.native_kind
                and new_native.native_id == old_native.native_id
                and new_native.package_path == old_native.package_path
                and _context_compatible(old.locator, anchor.locator)
            ):
                return AnchorMappingOutcome(
                    disposition=MAPPING_DISPOSITION_EXACT,
                    rule_id=RULE_NATIVE_IDENTITY,
                    confidence=DecimalValue(_RULE_CONFIDENCE[RULE_NATIVE_IDENTITY]),
                    target_anchor_id=anchor_id,
                    reason="authoritative native identity carried",
                    rule_evidence={
                        "provider": old_native.provider,
                        "native_kind": old_native.native_kind,
                        "native_id": old_native.native_id,
                    },
                )

    # Quote cascade — only entered when a quote selector exists.
    if old_quote is not None:
        old_text = old_quote.quote
        byte_matches = [
            anchor_id
            for anchor_id, anchor in sorted(by_id.items())
            if (quote := _quote_selector(anchor)) is not None
            and quote.quote == old_text
        ]
        # Rule 2 — byte-exact unique quote with compatible context.
        if len(byte_matches) == 1:
            anchor_id = byte_matches[0]
            quote = _quote_selector(by_id[anchor_id])
            if _context_compatible(old_quote.prefix, quote.prefix) and _context_compatible(
                old_quote.suffix, quote.suffix
            ):
                return AnchorMappingOutcome(
                    disposition=MAPPING_DISPOSITION_MAPPED_DETERMINISTIC,
                    rule_id=RULE_QUOTE_UNIQUE,
                    confidence=DecimalValue(_RULE_CONFIDENCE[RULE_QUOTE_UNIQUE]),
                    target_anchor_id=anchor_id,
                    reason="byte-exact quote matched exactly one new anchor",
                    rule_evidence={"quote_bytes": True, "quote_length": len(old_text)},
                )
        # Rule 3 — duplicates: identical text in multiple places stays a
        # ranked candidate set; ambiguity is preserved, never resolved.
        if len(byte_matches) > 1:
            return AnchorMappingOutcome(
                disposition=MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
                rule_id=RULE_QUOTE_DUPLICATES,
                confidence=DecimalValue(_RULE_CONFIDENCE[RULE_QUOTE_DUPLICATES]),
                candidates=_ranked(byte_matches, by_id, old),
                reason="byte-exact quote matched multiple new anchors",
                rule_evidence={"match_count": len(byte_matches)},
            )
        # Rule 4 — whitespace/case-normalized agreement: an edit that
        # did not touch the words is a candidate, not a mapping.
        normalized_old = _normalize_text(old_text)
        normalized_matches = [
            anchor_id
            for anchor_id, anchor in sorted(by_id.items())
            if (quote := _quote_selector(anchor)) is not None
            and _normalize_text(quote.quote) == normalized_old
        ]
        if normalized_matches:
            return AnchorMappingOutcome(
                disposition=MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
                rule_id=RULE_QUOTE_NORMALIZED,
                confidence=DecimalValue(_RULE_CONFIDENCE[RULE_QUOTE_NORMALIZED]),
                candidates=_ranked(normalized_matches, by_id, old),
                reason="whitespace/case-normalized quote agreement only",
                rule_evidence={"normalized_quote": normalized_old},
            )
        # Rule 5 — partial containment (split/merge): the old quote is a
        # substring of a new quote or vice versa.
        partial_matches = [
            anchor_id
            for anchor_id, anchor in sorted(by_id.items())
            if (quote := _quote_selector(anchor)) is not None
            and (
                old_text in quote.quote or (quote.quote and quote.quote in old_text)
            )
        ]
        if partial_matches:
            return AnchorMappingOutcome(
                disposition=MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
                rule_id=RULE_QUOTE_PARTIAL,
                confidence=DecimalValue(_RULE_CONFIDENCE[RULE_QUOTE_PARTIAL]),
                candidates=_ranked(partial_matches, by_id, old),
                reason="quote containment (region split or merge)",
                rule_evidence={"old_quote_length": len(old_text)},
            )
        # Rule 6 — near text: deterministic similarity above threshold
        # is a candidate; below it the region was rewritten.
        fuzzy: list[tuple[float, str]] = []
        for anchor_id, anchor in sorted(by_id.items()):
            quote = _quote_selector(anchor)
            if quote is None:
                continue
            ratio = SequenceMatcher(None, old_text, quote.quote).ratio()
            if ratio >= _FUZZY_CANDIDATE_THRESHOLD:
                fuzzy.append((ratio, anchor_id))
        if fuzzy:
            ordered = tuple(
                anchor_id for _, anchor_id in sorted(fuzzy, key=lambda item: (-item[0], item[1]))
            )
            return AnchorMappingOutcome(
                disposition=MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
                rule_id=RULE_QUOTE_FUZZY,
                confidence=DecimalValue(_RULE_CONFIDENCE[RULE_QUOTE_FUZZY]),
                candidates=ordered,
                reason="near-text agreement above candidate threshold",
                rule_evidence={
                    "similarity_threshold": _FUZZY_CANDIDATE_THRESHOLD,
                    "best_ratio": round(max(ratio for ratio, _ in fuzzy), 6),
                },
            )

    # Rule 7 — geometry agreement is only ever candidate evidence:
    # identical integer boxes in a declared space still cannot prove
    # source identity (render-state dependent, approximate by contract).
    if old.selectors.get("geometry") is not None:
        old_geometry = old.selectors["geometry"]
        geometry_matches = [
            anchor_id
            for anchor_id, anchor in sorted(by_id.items())
            if (geometry := anchor.selectors.get("geometry")) is not None
            and geometry.canonical_value() == old_geometry.canonical_value()
        ]
        if geometry_matches:
            return AnchorMappingOutcome(
                disposition=MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
                rule_id=RULE_GEOMETRY_APPROXIMATE,
                confidence=DecimalValue(_RULE_CONFIDENCE[RULE_GEOMETRY_APPROXIMATE]),
                candidates=_ranked(geometry_matches, by_id, old),
                reason="geometry agreement is approximate evidence only",
                rule_evidence={"space_id": old_geometry.space.space_id},
            )

    # Rule 8 — usable evidence with no plausible target: the region is
    # gone (deletion or full rewrite). Honest staleness, not an error.
    if old_native is not None or old_quote is not None:
        return AnchorMappingOutcome(
            disposition=MAPPING_DISPOSITION_STALE,
            rule_id=RULE_NO_MATCH,
            confidence=DecimalValue(_RULE_CONFIDENCE[RULE_NO_MATCH]),
            reason="no plausible target in the new revision",
            rule_evidence={},
        )

    # Rule 9 — nothing that could support cross-revision mapping.
    return AnchorMappingOutcome(
        disposition=MAPPING_DISPOSITION_UNRESOLVED,
        rule_id=RULE_INSUFFICIENT_EVIDENCE,
        confidence=DecimalValue(_RULE_CONFIDENCE[RULE_INSUFFICIENT_EVIDENCE]),
        reason="positional-only anchors cannot be mapped across revisions",
        rule_evidence={"selector_families": tuple(sorted(old.selectors))},
    )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

_RECORD_CLASS_SOURCE_ANCHOR_MAPPING = "source_anchor_mapping"
RECORD_TYPE_SOURCE_ANCHOR_MAPPING = "marker.kernel.source_anchor_mapping.v1"

_RECORD_CLASS_ANCHOR_MAPPING_DECISION = "anchor_mapping_decision"
RECORD_TYPE_ANCHOR_MAPPING_DECISION = "marker.kernel.anchor_mapping_decision.v1"

_ALLOWED_MAPPING_KEYS = frozenset(
    {
        "source_revision_ref",
        "target_revision_ref",
        "source_anchor_id",
        "disposition",
        "rule_id",
        "rule_version",
        "target_anchor_id",
        "candidates",
        "confidence",
        "reason",
        "rule_evidence",
    }
)

_DECIDED_BY_DETERMINISTIC_CASCADE = "deterministic_cascade"
_DECIDED_BY_HUMAN_REVIEW = "human_review"
DECIDED_BY_VALUES: frozenset[str] = frozenset(
    {_DECIDED_BY_DETERMINISTIC_CASCADE, _DECIDED_BY_HUMAN_REVIEW}
)


def mapping_confidence(value: str) -> DecimalValue:
    """Validate a mapping confidence as a canonical decimal in [0, 1]."""
    decimal_value = DecimalValue(value)
    if not Decimal(0) <= Decimal(decimal_value.text) <= Decimal(1):
        raise KernelError(f"invalid mapping confidence {value!r}: must lie in [0, 1]")
    return decimal_value


@dataclass(kw_only=True)
class SourceAnchorMappingRecord(KernelRecord):
    """Append-only evidence: one computed cross-revision mapping.

    Identity covers every semantic field, so re-running the cascade on
    frozen inputs converges on the same record id instead of minting a
    competing truth. Supersession happens through a new
    :class:`AnchorMappingDecisionRecord`, never by overwriting.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_SOURCE_ANCHOR_MAPPING
    record_type: ClassVar[str] = RECORD_TYPE_SOURCE_ANCHOR_MAPPING
    schema_version: ClassVar[str] = "1.0.0"

    source_revision_ref: str
    target_revision_ref: str
    source_anchor_id: str
    disposition: str
    rule_id: str
    rule_version: str
    target_anchor_id: str | None = None
    candidates: tuple[str, ...] = ()
    confidence: str
    reason: str = ""
    rule_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_revision_ref(self.source_revision_ref, field_name="source_revision_ref")
        _validate_revision_ref(self.target_revision_ref, field_name="target_revision_ref")
        if self.source_revision_ref == self.target_revision_ref:
            raise KernelError(
                "source and target revisions are equal: an ACL-only change "
                "cannot remint content identity"
            )
        validate_record_ref(self.source_anchor_id, field_name="source_anchor_id")
        if self.disposition not in MAPPING_DISPOSITIONS:
            raise KernelError(
                f"unknown mapping disposition {self.disposition!r}; allowed: "
                f"{sorted(MAPPING_DISPOSITIONS)}"
            )
        if self.disposition == MAPPING_DISPOSITION_MAPPED_REVIEWED:
            raise KernelError(
                "mapped_reviewed is a decision-only disposition: confirm a "
                "candidate through an AnchorMappingDecisionRecord instead"
            )
        if self.disposition in (MAPPING_DISPOSITION_EXACT, MAPPING_DISPOSITION_MAPPED_DETERMINISTIC):
            if self.target_anchor_id is None:
                raise KernelError(
                    f"disposition {self.disposition!r} requires a target anchor id"
                )
            if self.candidates:
                raise KernelError(
                    f"disposition {self.disposition!r} cannot carry candidates"
                )
        if self.disposition == MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE and not self.candidates:
            raise KernelError("a candidate mapping requires at least one candidate")
        if self.target_anchor_id is not None:
            validate_record_ref(self.target_anchor_id, field_name="target_anchor_id")
        for candidate in self.candidates:
            validate_record_ref(candidate, field_name="candidate")
        if self.target_anchor_id is not None and self.target_anchor_id in self.candidates:
            raise KernelError("target anchor cannot also be listed as a candidate")
        if self.candidates != tuple(dict.fromkeys(self.candidates)):
            raise KernelError("candidate order must be duplicate-free")
        self.confidence = mapping_confidence(self.confidence).text
        if not isinstance(self.rule_evidence, Mapping):
            raise KernelError(f"rule_evidence must be a mapping, got {self.rule_evidence!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "source_revision_ref": self.source_revision_ref,
            "target_revision_ref": self.target_revision_ref,
            "source_anchor_id": self.source_anchor_id,
            "disposition": self.disposition,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "target_anchor_id": self.target_anchor_id,
            "candidates": list(self.candidates),
            "confidence": self.confidence,
            "reason": self.reason,
            "rule_evidence": to_json_ready(dict(self.rule_evidence)),
        }

    def mapping_id(self) -> str:
        """Deterministic identity of this mapping under the framing domain."""
        return record_identity_hash(
            record_type=self.record_type,
            schema_version=self.schema_version,
            payload=to_json_ready(self.identity_payload()),
        )

    @classmethod
    def from_outcome(
        cls,
        outcome: AnchorMappingOutcome,
        *,
        source_revision_ref: str,
        target_revision_ref: str,
        source_anchor_id: str,
    ) -> SourceAnchorMappingRecord:
        """Wrap a cascade outcome as a committable record."""
        return cls(
            source_revision_ref=source_revision_ref,
            target_revision_ref=target_revision_ref,
            source_anchor_id=source_anchor_id,
            disposition=outcome.disposition,
            rule_id=outcome.rule_id,
            rule_version=ANCHOR_MAPPING_CASCADE_VERSION,
            target_anchor_id=outcome.target_anchor_id,
            candidates=tuple(outcome.candidates),
            confidence=outcome.confidence.text,
            reason=outcome.reason,
            rule_evidence=dict(outcome.rule_evidence),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SourceAnchorMappingRecord:
        """Fail-closed reconstruction from a canonical payload."""
        unknown = set(payload) - _ALLOWED_MAPPING_KEYS
        if unknown:
            raise KernelError(
                f"unknown mapping payload fields {sorted(unknown)}; "
                "identity-bearing extensions must be declared, not ignored"
            )
        return cls(
            source_revision_ref=payload["source_revision_ref"],
            target_revision_ref=payload["target_revision_ref"],
            source_anchor_id=payload["source_anchor_id"],
            disposition=payload["disposition"],
            rule_id=payload["rule_id"],
            rule_version=payload["rule_version"],
            target_anchor_id=payload.get("target_anchor_id"),
            candidates=tuple(payload.get("candidates", ())),
            confidence=payload["confidence"],
            reason=payload.get("reason", ""),
            rule_evidence=dict(payload.get("rule_evidence", {})),
        )


@dataclass(kw_only=True)
class AnchorMappingDecisionRecord(KernelRecord):
    """The only way the effective public mapping of an anchor changes.

    A deterministic cascade decision restates a computed mapping; a
    human-review decision promotes a semantic candidate to
    ``mapped_reviewed``. Supersession is explicit: the decision that
    replaces this one must name it in ``supersedes_decision_ref``.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_ANCHOR_MAPPING_DECISION
    record_type: ClassVar[str] = RECORD_TYPE_ANCHOR_MAPPING_DECISION
    schema_version: ClassVar[str] = "1.0.0"

    mapping_ref: str
    effective_disposition: str
    decided_by: str
    supersedes_decision_ref: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.mapping_ref, field_name="mapping_ref")
        if self.effective_disposition not in MAPPING_DISPOSITIONS:
            raise KernelError(
                f"unknown effective disposition {self.effective_disposition!r}; "
                f"allowed: {sorted(MAPPING_DISPOSITIONS)}"
            )
        if self.decided_by not in DECIDED_BY_VALUES:
            raise KernelError(
                f"unknown decided_by {self.decided_by!r}; allowed: {sorted(DECIDED_BY_VALUES)}"
            )
        if self.supersedes_decision_ref is not None:
            validate_record_ref(
                self.supersedes_decision_ref, field_name="supersedes_decision_ref"
            )
        # The load-bearing invariant: reviewed mappings are the only
        # promotion path, and only humans take it.
        if self.effective_disposition == MAPPING_DISPOSITION_MAPPED_REVIEWED:
            if self.decided_by != _DECIDED_BY_HUMAN_REVIEW:
                raise KernelError(
                    "mapped_reviewed can only be minted by a human_review "
                    "decision; the deterministic cascade never promotes"
                )
        elif self.decided_by == _DECIDED_BY_HUMAN_REVIEW:
            raise KernelError(
                "a human_review decision must state an effective disposition "
                "of mapped_reviewed (confirm or reject candidates instead)"
            )
        if not isinstance(self.note, str):
            raise KernelError(f"note must be a string, got {self.note!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "mapping_ref": self.mapping_ref,
            "effective_disposition": self.effective_disposition,
            "decided_by": self.decided_by,
            "supersedes_decision_ref": self.supersedes_decision_ref,
            "note": self.note,
        }

    def decision_id(self) -> str:
        return record_identity_hash(
            record_type=self.record_type,
            schema_version=self.schema_version,
            payload=to_json_ready(self.identity_payload()),
        )


def effective_disposition(
    mapping: SourceAnchorMappingRecord,
    decisions: Sequence[AnchorMappingDecisionRecord],
) -> str:
    """Resolve the effective public mapping from a decision chain.

    Without decisions the computed disposition stands as-is — a candidate
    stays a candidate. Supersession is resolved by chain order: each
    decision naming a predecessor replaces it; conflicting parallel
    decisions (two decisions superseding the same predecessor, or a
    decision superseding nothing while others exist) fail closed instead
    of resolving by accident.
    """
    for decision in decisions:
        if decision.mapping_ref not in (mapping.record_id, mapping.mapping_id()):
            raise KernelError(
                f"decision {decision.record_id!r} does not belong to mapping "
                f"{mapping.mapping_id()!r}"
            )
    if not decisions:
        return mapping.disposition
    by_ref = {decision.record_id: decision for decision in decisions}
    superseded = {
        decision.supersedes_decision_ref
        for decision in decisions
        if decision.supersedes_decision_ref is not None
    }
    roots = [decision for decision in decisions if decision.record_id not in superseded]
    if len(roots) != 1:
        raise KernelError(
            f"decision chain for mapping {mapping.mapping_id()!r} is not a "
            f"single linear history ({len(roots)} roots); conflicting "
            "decisions fail closed"
        )
    current = roots[0]
    seen: set[str] = set()
    while True:
        if current.record_id in seen:
            raise KernelError("decision chain contains a cycle")
        seen.add(current.record_id)
        next_refs = [
            decision
            for decision in decisions
            if decision.supersedes_decision_ref == current.record_id
        ]
        if not next_refs:
            return current.effective_disposition
        if len(next_refs) > 1:
            raise KernelError(
                "decision chain forks: two decisions supersede the same "
                "predecessor; conflicting decisions fail closed"
            )
        current = next_refs[0]
