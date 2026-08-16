"""Source-truth record identity semantics (PR70 local slice, plan §13.1).

Pure identity-matrix tests over the five new kernel record classes: the
same logical state must converge to one semantic identity, and the
separations the masterplan demands (content vs access vs logical source)
must be observable in the identity hashes themselves.
"""

from __future__ import annotations

import hashlib

import pytest

from app.kernel.errors import KernelError
from app.kernel.records import (
    SOURCE_CONSISTENCY_BEST_EFFORT,
    SOURCE_CONSISTENCY_STABLE_HANDLE,
    AccessPolicyRevisionRecord,
    AuthorizationEpochRecord,
    ContentRevisionRecord,
    SourceIdentityRecord,
    SourceObservationRecord,
)
from app.utils.canonical import record_identity_hash, to_json_ready


def identity_of(record) -> str:
    return record_identity_hash(
        record_type=record.record_type,
        schema_version=record.schema_version,
        payload=to_json_ready(record.identity_payload()),
    )


def _blob(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def make_source(kind: str = "local_path", key: str = "C:/docs/report.pdf"):
    return SourceIdentityRecord(record_id="src-1", source_kind=kind, source_key=key)


def make_revision(source_ref: str, data: bytes = b"bytes-a", **overrides):
    fields = dict(
        record_id="rev-1",
        source_ref=source_ref,
        blob_key=_blob(data),
        byte_length=len(data),
        media_type="application/pdf",
        consistency_class=SOURCE_CONSISTENCY_STABLE_HANDLE,
        suffix=".pdf",
    )
    fields.update(overrides)
    return ContentRevisionRecord(**fields)


class TestSourceIdentity:
    def test_same_logical_source_converges(self):
        a = make_source()
        b = SourceIdentityRecord(
            record_id="src-different-event-id", source_kind="local_path",
            source_key="C:/docs/report.pdf",
        )
        assert identity_of(a) == identity_of(b)

    def test_different_logical_sources_do_not_merge(self):
        assert identity_of(make_source(key="C:/docs/a.pdf")) != identity_of(
            make_source(key="C:/docs/b.pdf")
        )

    def test_identical_bytes_across_kinds_stay_distinct(self):
        # An upload occurrence and a local path are two logical sources
        # even if their content hash will be identical.
        assert identity_of(make_source(kind="upload", key="upload:j1")) != identity_of(
            make_source(kind="local_path", key="C:/docs/a.pdf")
        )

    def test_registration_context_is_not_identity(self):
        a = make_source()
        b = SourceIdentityRecord(
            record_id="src-2", source_kind="local_path",
            source_key="C:/docs/report.pdf",
            registered_context={"submitted_via": "rest"},
        )
        assert identity_of(a) == identity_of(b)

    def test_invalid_kind_and_key_rejected(self):
        with pytest.raises(KernelError):
            make_source(kind="ftp")
        with pytest.raises(KernelError):
            make_source(key="")
        with pytest.raises(KernelError):
            make_source(key="x" * 600)


class TestContentRevision:
    def test_same_source_same_bytes_converge(self):
        assert identity_of(make_revision("src-1")) == identity_of(
            make_revision("src-1", record_id="rev-2")
        )

    def test_same_source_changed_bytes_is_new_revision(self):
        assert identity_of(make_revision("src-1", b"bytes-a")) != identity_of(
            make_revision("src-1", b"bytes-b")
        )

    def test_identical_bytes_on_two_sources_are_two_revisions(self):
        # Content identity is scoped to the logical source: a shared
        # blob_key must not merge the two revisions.
        assert identity_of(make_revision("src-1", b"shared")) != identity_of(
            make_revision("src-2", b"shared")
        )

    def test_rejected_consistency_class_cannot_mint_revision(self):
        with pytest.raises(KernelError):
            make_revision("src-1", consistency_class="incoherent_rejected")
        with pytest.raises(KernelError):
            make_revision("src-1", consistency_class="made_up_class")

    def test_field_validation(self):
        with pytest.raises(KernelError):
            make_revision("src-1", blob_key="deadbeef")
        with pytest.raises(KernelError):
            make_revision("src-1", byte_length=-1)
        with pytest.raises(KernelError):
            make_revision("src-1", suffix="PDF")
        with pytest.raises(KernelError):
            make_revision("bad ref with spaces")

    def test_consistency_class_and_media_type_participate_in_identity(self):
        assert identity_of(make_revision("src-1")) != identity_of(
            make_revision("src-1", consistency_class=SOURCE_CONSISTENCY_BEST_EFFORT)
        )
        assert identity_of(make_revision("src-1")) != identity_of(
            make_revision("src-1", media_type="application/vnd...")
        )


class TestAccessPolicyRevision:
    def test_policy_only_change_is_new_access_identity(self):
        base = dict(record_id="pol-1", source_ref="src-1", policy_profile="local_v1")
        tight = AccessPolicyRevisionRecord(
            **base, policy_facts={"permitted_root": "C:/docs", "unrestricted": False}
        )
        loose = AccessPolicyRevisionRecord(
            **base, policy_facts={"permitted_root": "C:/", "unrestricted": False}
        )
        assert identity_of(tight) != identity_of(loose)

    def test_same_policy_converges(self):
        facts = {"permitted_root": "C:/docs", "unrestricted": False}
        a = AccessPolicyRevisionRecord(
            record_id="pol-1", source_ref="src-1", policy_profile="local_v1", policy_facts=facts
        )
        b = AccessPolicyRevisionRecord(
            record_id="pol-2", source_ref="src-1", policy_profile="local_v1", policy_facts=dict(facts)
        )
        assert identity_of(a) == identity_of(b)


class TestAuthorizationEpoch:
    def _fingerprint(self, facts: dict) -> str:
        return _blob(repr(sorted(facts.items())).encode())

    def test_same_domain_same_epoch_converges(self):
        facts = {"roots": ["C:/docs"], "unrestricted": False}
        a = AuthorizationEpochRecord(
            record_id="ep-1", epoch_number=1, fingerprint=self._fingerprint(facts),
            domain_facts=facts,
        )
        b = AuthorizationEpochRecord(
            record_id="ep-2", epoch_number=1, fingerprint=self._fingerprint(dict(facts)),
            domain_facts=dict(facts),
        )
        assert identity_of(a) == identity_of(b)

    def test_epoch_number_advances_identity(self):
        facts = {"roots": ["C:/docs"], "unrestricted": False}
        a = AuthorizationEpochRecord(
            record_id="ep-1", epoch_number=1, fingerprint=self._fingerprint(facts),
            domain_facts=facts,
        )
        b = AuthorizationEpochRecord(
            record_id="ep-2", epoch_number=2, fingerprint=self._fingerprint(facts),
            domain_facts=facts,
        )
        assert identity_of(a) != identity_of(b)

    def test_invalid_epoch_rejected(self):
        with pytest.raises(KernelError):
            AuthorizationEpochRecord(
                record_id="ep-0", epoch_number=0, fingerprint=self._fingerprint({}),
                domain_facts={},
            )


class TestSourceObservation:
    def test_accepted_observation_requires_revision_ref(self):
        with pytest.raises(KernelError):
            SourceObservationRecord(
                record_id="obs-1", observer="acq", source_ref="src-1", outcome="accepted"
            )

    def test_rejected_observation_has_no_revision_ref(self):
        obs = SourceObservationRecord(
            record_id="obs-1", observer="acq", source_ref="src-1",
            outcome="rejected_incoherent",
            evidence={"reason": "size mismatch"},
        )
        assert obs.identity_payload()["content_revision_ref"] is None

    def test_evidence_participates_in_identity(self):
        base = dict(
            record_id="obs-1", observer="acq", source_ref="src-1",
            outcome="accepted", content_revision_ref="rev-1",
        )
        a = SourceObservationRecord(**base, evidence={"observed_at": "2026-08-16T10:00:00Z"})
        b = SourceObservationRecord(**base, evidence={"observed_at": "2026-08-16T11:00:00Z"})
        assert identity_of(a) != identity_of(b)

    def test_invalid_outcome_rejected(self):
        with pytest.raises(KernelError):
            SourceObservationRecord(
                record_id="obs-1", observer="acq", source_ref="src-1", outcome="maybe"
            )
