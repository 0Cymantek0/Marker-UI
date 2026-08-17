"""Access-domain record identity semantics (PR78).

Pure identity-matrix tests over the two PR78 record classes:
``SecurityDomainRecord`` (source → security-domain assignment, policy
only, never content) and ``AccessDenialRecord`` (live-deny event chain
keyed to stable identity). The separations the masterplan demands —
policy-only changes minting policy records while content revisions stay
untouched, and denial being an append-only event chain rather than a
mutation of history — must be observable in the identity hashes.
"""

from __future__ import annotations

import pytest

from app.kernel.errors import KernelError
from app.kernel.records import (
    ACCESS_DENIAL_TARGET_DOMAIN,
    ACCESS_DENIAL_TARGET_RECORD,
    ACCESS_DENIAL_TARGET_SOURCE,
    AccessDenialRecord,
    ContentRevisionRecord,
    SecurityDomainRecord,
    SourceIdentityRecord,
)
from app.utils.canonical import record_identity_hash, to_json_ready


def identity_of(record) -> str:
    return record_identity_hash(
        record_type=record.record_type,
        schema_version=record.schema_version,
        payload=to_json_ready(record.identity_payload()),
    )


def make_assignment(
    source_ref: str = "src-1", domain_key: str = "dom-alpha", **overrides
) -> SecurityDomainRecord:
    fields = dict(record_id="assign-1", source_ref=source_ref, domain_key=domain_key)
    fields.update(overrides)
    return SecurityDomainRecord(**fields)


class TestSecurityDomainRecord:
    def test_same_assignment_converges_regardless_of_event_id(self):
        a = make_assignment()
        b = make_assignment(record_id="assign-other-event")
        assert identity_of(a) == identity_of(b)

    def test_reassignment_to_another_domain_is_a_new_identity(self):
        assert identity_of(make_assignment(domain_key="dom-alpha")) != identity_of(
            make_assignment(domain_key="dom-beta")
        )

    def test_assignment_is_per_source(self):
        assert identity_of(make_assignment(source_ref="src-1")) != identity_of(
            make_assignment(source_ref="src-2")
        )

    def test_assignment_basis_is_self_describing_identity(self):
        a = make_assignment(assignment_basis={"operator": "alice"})
        b = make_assignment(assignment_basis={"operator": "bob"})
        assert identity_of(a) != identity_of(b)

    def test_policy_only_reassignment_leaves_content_identity_untouched(self):
        """A4 separation: moving a source between domains must not mint
        a new content revision — the content record's identity payload
        does not contain the domain."""
        source = SourceIdentityRecord(
            record_id="src-1", source_kind="local_path", source_key="C:/docs/a.pdf"
        )
        revision = ContentRevisionRecord(
            record_id="rev-1",
            source_ref=source.record_id,
            blob_key="sha256:" + "0" * 64,
            byte_length=1,
            media_type="application/pdf",
            consistency_class="native_atomic",
            suffix=".pdf",
        )
        before = identity_of(revision)
        make_assignment(source_ref=source.record_id, domain_key="dom-alpha")
        make_assignment(source_ref=source.record_id, domain_key="dom-beta")
        assert identity_of(revision) == before

    def test_invalid_domain_key_rejected(self):
        with pytest.raises(KernelError, match="domain_key"):
            make_assignment(domain_key="Dom-Upper")
        with pytest.raises(KernelError, match="domain_key"):
            make_assignment(domain_key="")
        with pytest.raises(KernelError, match="domain_key"):
            make_assignment(domain_key="-leading-dash")

    def test_invalid_source_ref_rejected(self):
        with pytest.raises(KernelError, match="source_ref"):
            make_assignment(source_ref="bad ref with spaces")


class TestAccessDenialRecord:
    def test_same_deny_event_converges_regardless_of_event_id(self):
        a = AccessDenialRecord(
            record_id="deny-1",
            target_kind=ACCESS_DENIAL_TARGET_RECORD,
            target_ref="view-1",
            denied=True,
        )
        b = AccessDenialRecord(
            record_id="deny-other-event",
            target_kind=ACCESS_DENIAL_TARGET_RECORD,
            target_ref="view-1",
            denied=True,
        )
        assert identity_of(a) == identity_of(b)

    def test_deny_and_explicit_lift_are_distinct_identities(self):
        deny = AccessDenialRecord(
            record_id="d", target_kind=ACCESS_DENIAL_TARGET_RECORD,
            target_ref="view-1", denied=True,
        )
        lift = AccessDenialRecord(
            record_id="l", target_kind=ACCESS_DENIAL_TARGET_RECORD,
            target_ref="view-1", denied=False, supersedes="d",
        )
        assert identity_of(deny) != identity_of(lift)

    def test_redeny_after_lift_never_collides_with_first_deny(self):
        """The deny→allow→deny cycle must be committable: the chained
        event is semantically distinct from the first denial."""
        first = AccessDenialRecord(
            record_id="d1", target_kind=ACCESS_DENIAL_TARGET_SOURCE,
            target_ref="src-1", denied=True,
        )
        lift = AccessDenialRecord(
            record_id="d2", target_kind=ACCESS_DENIAL_TARGET_SOURCE,
            target_ref="src-1", denied=False, supersedes="d1",
        )
        second = AccessDenialRecord(
            record_id="d3", target_kind=ACCESS_DENIAL_TARGET_SOURCE,
            target_ref="src-1", denied=True, supersedes="d2",
        )
        identities = {identity_of(first), identity_of(lift), identity_of(second)}
        assert len(identities) == 3

    def test_denial_basis_is_self_describing_identity(self):
        a = AccessDenialRecord(
            record_id="d", target_kind=ACCESS_DENIAL_TARGET_DOMAIN,
            target_ref="dom-alpha", denied=True,
            denial_basis={"reason": "revoked by operator"},
        )
        b = AccessDenialRecord(
            record_id="d", target_kind=ACCESS_DENIAL_TARGET_DOMAIN,
            target_ref="dom-alpha", denied=True,
            denial_basis={"reason": "other audit context"},
        )
        assert identity_of(a) != identity_of(b)

    def test_target_kinds_are_separate_scopes(self):
        deny_record = AccessDenialRecord(
            record_id="d", target_kind=ACCESS_DENIAL_TARGET_RECORD,
            target_ref="some-id", denied=True,
        )
        deny_source = AccessDenialRecord(
            record_id="d", target_kind=ACCESS_DENIAL_TARGET_SOURCE,
            target_ref="some-id", denied=True,
        )
        assert identity_of(deny_record) != identity_of(deny_source)

    def test_invalid_target_kind_rejected(self):
        with pytest.raises(KernelError, match="target_kind"):
            AccessDenialRecord(
                record_id="d", target_kind="workspace", target_ref="x", denied=True
            )

    def test_domain_target_must_be_valid_domain_key(self):
        with pytest.raises(KernelError, match="domain target_ref"):
            AccessDenialRecord(
                record_id="d", target_kind=ACCESS_DENIAL_TARGET_DOMAIN,
                target_ref="not a domain", denied=True,
            )

    def test_record_and_source_targets_must_be_record_refs(self):
        with pytest.raises(KernelError, match="target_ref"):
            AccessDenialRecord(
                record_id="d", target_kind=ACCESS_DENIAL_TARGET_RECORD,
                target_ref="bad ref", denied=True,
            )

    def test_denied_must_be_bool(self):
        with pytest.raises(KernelError, match="denied"):
            AccessDenialRecord(
                record_id="d", target_kind=ACCESS_DENIAL_TARGET_RECORD,
                target_ref="view-1", denied="yes",
            )

    def test_supersedes_must_be_record_ref_when_present(self):
        with pytest.raises(KernelError, match="supersedes"):
            AccessDenialRecord(
                record_id="d", target_kind=ACCESS_DENIAL_TARGET_RECORD,
                target_ref="view-1", denied=True, supersedes="bad ref",
            )
