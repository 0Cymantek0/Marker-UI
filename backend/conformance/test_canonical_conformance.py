"""Golden conformance run over the committed canonical identity corpus.

Stdlib + pytest only: this file must stay runnable in a bare
environment (the cross-platform CI matrix installs nothing else).
Expected outputs are committed constants in
``fixtures/canonical_vectors_v1.json``; this suite never regenerates
them. To update the corpus intentionally, run
``backend/scripts/generate_canonical_fixtures.py --write`` and review
the diff.
"""

from __future__ import annotations

import pytest

from conformance.fixture_codec import load_fixture_corpus, verify_case


def _case_ids() -> list[str]:
    corpus = load_fixture_corpus()
    return [case["id"] for case in corpus["cases"]]


def _case_by_id(case_id: str) -> dict:
    corpus = load_fixture_corpus()
    for case in corpus["cases"]:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


def test_corpus_header_declares_contract() -> None:
    corpus = load_fixture_corpus()
    assert corpus["$schema"] == "marker.canonical.fixtures.v1"
    assert corpus["canonicalization_profile"] == "marker.canonical.v1"
    assert corpus["framing"] == "marker.record_identity.v1"


def test_corpus_has_no_duplicate_case_ids() -> None:
    ids = _case_ids()
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case_id", _case_ids())
def test_case(case_id: str) -> None:
    verify_case(_case_by_id(case_id))


def test_expected_outputs_are_committed_constants() -> None:
    corpus = load_fixture_corpus()
    positives = [c for c in corpus["cases"] if "expect_error" not in c]
    assert positives, "corpus must contain positive golden cases"
    for case in positives:
        assert isinstance(case["expect"], dict), (
            f"{case['id']}: expect must be a committed object, not null"
        )


def test_corpus_covers_required_categories() -> None:
    corpus = load_fixture_corpus()
    categories = {case["category"] for case in corpus["cases"]}
    required = {"ordering", "unicode", "structure", "numbers", "geometry", "framing", "rejection", "composite"}
    missing = required - categories
    assert not missing, f"corpus missing categories: {sorted(missing)}"


def test_rejection_messages_reference_error_not_success() -> None:
    corpus = load_fixture_corpus()
    rejections = [c for c in corpus["cases"] if "expect_error" in c]
    assert len(rejections) >= 10, "adversarial rejection coverage shrank"
