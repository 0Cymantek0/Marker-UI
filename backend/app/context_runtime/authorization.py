"""Trusted server-side authorization resolution (PR78).

The PR77 contract carries caller-supplied identity seams
(``QuerySecurityContext``) that are explicitly *not* authorization
proof. This module is the other half: effective authorization is
derived here from committed kernel truth only — the workspace's current
``AuthorizationEpochRecord``, the latest committed security-domain
assignment per source, the latest live-deny event per target, and the
latest PR70 access-policy revision per source. Nothing a caller sends
can influence the outcome, and anything malformed in the policy
lineage fails closed as :class:`QueryAuthorizationError` instead of
degrading to unrestricted reads.

Base grant model (honest for the local single-user profile): the
workspace boundary is the base grant — every record in the workspace's
publication is readable unless a live deny (record, source, or domain)
excludes it. This slice has no identity provider, so there is no
richer per-principal ACL to pretend to. The ``local_v1`` profile
records exactly that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime.errors import QueryAuthorizationError
from app.kernel.models import KernelRecord
from app.kernel.publications import high_assurance_profile
from app.utils.canonical import (
    canonical_json_bytes,
    payload_byte_hash,
    to_json_ready,
)

__all__ = [
    "AUTHORIZATION_PROFILE_LOCAL",
    "ASSURANCE_STANDARD",
    "ASSURANCE_HIGH",
    "EffectiveAuthorization",
    "resolve_effective_authorization",
]

#: The one authorization profile this slice implements. It states the
#: truth the local product can actually defend: workspace isolation
#: plus the live deny overlay, with declared residuals per assurance
#: mode (see docs/reference/authorization-retrieval.md).
AUTHORIZATION_PROFILE_LOCAL = "local_v1"

ASSURANCE_STANDARD = "standard"
ASSURANCE_HIGH = "high"

_EPOCH_FINGERPRINT_PATTERN_PREFIX = "sha256:"


@dataclass(frozen=True)
class EffectiveAuthorization:
    """One query's effective authorization, resolved from trusted state.

    ``epoch_number``/``epoch_fingerprint`` come from the latest
    committed workspace epoch (0/None when the workspace never advanced
    one). ``domain_assignments`` maps source record id → security
    domain from the latest assignment per source. The three deny sets
    are the *current* overlay state: latest event per target, where a
    ``denied=False`` event lifts an earlier deny. ``deny_revision`` is
    the causal commit id of the newest denial event (0 when none) — a
    monotonic marker that changes on every overlay transition, including
    lifts that restore an earlier set state.
    """

    profile: str
    assurance: str
    workspace_id: str
    epoch_number: int
    epoch_fingerprint: str | None
    domain_assignments: Mapping[str, str] = field(default_factory=dict)
    denied_domains: frozenset[str] = frozenset()
    denied_sources: frozenset[str] = frozenset()
    denied_records: frozenset[str] = frozenset()
    deny_revision: int = 0
    policy_digest: str = ""

    def allows(
        self,
        record_id: str,
        *,
        source_ref: str | None = None,
        domain_key: str | None = None,
    ) -> bool:
        """Live visibility decision for one record. Deny wins over the
        base grant at every granularity: record, source, then domain."""
        if record_id in self.denied_records:
            return False
        if source_ref is not None and source_ref in self.denied_sources:
            return False
        if domain_key is not None and domain_key in self.denied_domains:
            return False
        return True

    def domain_of(self, source_ref: str | None) -> str | None:
        if source_ref is None:
            return None
        return self.domain_assignments.get(source_ref)

    def partition_domains(self) -> tuple[str, ...]:
        """Security domains visible to this authorization: every
        assigned domain minus live-denied domains. This is the trusted
        derivation — a caller never names the partition."""
        return tuple(
            sorted((set(self.domain_assignments.values()) - self.denied_domains))
        )

    def partition_profile(self) -> str:
        """Publication profile of the high-assurance corpus this
        authorization may read."""
        return high_assurance_profile(self.partition_domains())

    def identity_view(self) -> dict[str, Any]:
        """Caller-safe identity dimensions: enough to invalidate packet
        reuse on any authorization change, never enough to reveal the
        domain/denial topology behind the digest."""
        return {
            "profile": self.profile,
            "assurance": self.assurance,
            "epoch_number": self.epoch_number,
            "epoch_fingerprint": self.epoch_fingerprint,
            "deny_revision": self.deny_revision,
            "policy_digest": self.policy_digest,
        }


def _load_payload(record_id: str, record_class: str, payload_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except Exception as exc:
        raise QueryAuthorizationError(
            f"record={record_id!r} class={record_class!r}: policy payload "
            f"unreadable: {exc}; refusing to resolve authorization from "
            "corrupt state"
        ) from exc
    if not isinstance(payload, dict):
        raise QueryAuthorizationError(
            f"record={record_id!r} class={record_class!r}: policy payload is "
            "not an object; refusing to resolve authorization from corrupt "
            "state"
        )
    return payload


async def resolve_effective_authorization(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    assurance: str = ASSURANCE_STANDARD,
) -> EffectiveAuthorization:
    """Resolve the current effective authorization for one workspace.

    Every read here is trusted-state-derived: committed kernel records
    only, latest-per-target by causal commit order. Malformed or
    inconsistent policy lineage raises instead of defaulting to
    permissive behavior.
    """
    if assurance not in (ASSURANCE_STANDARD, ASSURANCE_HIGH):
        raise QueryAuthorizationError(f"unknown assurance mode {assurance!r}")

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(
                        KernelRecord.id,
                        KernelRecord.record_class,
                        KernelRecord.kernel_commit_id,
                        KernelRecord.identity_hash,
                        KernelRecord.payload_json,
                    ).where(
                        KernelRecord.workspace_id == workspace_id,
                        KernelRecord.record_class.in_(
                            (
                                "authorization_epoch",
                                "security_domain",
                                "access_denial",
                                "access_policy_revision",
                            )
                        ),
                    )
                )
            )
            .all()
        )

    epoch_number = 0
    epoch_fingerprint: str | None = None
    epoch_commit = -1
    epoch_id = ""
    assignment_commit: dict[str, int] = {}
    assignment_order: dict[str, str] = {}
    domain_assignments: dict[str, str] = {}
    denial_state: dict[tuple[str, str], bool] = {}
    denial_commit: dict[tuple[str, str], int] = {}
    denial_order: dict[tuple[str, str], str] = {}
    deny_revision = 0
    policy_identity_commit: dict[str, int] = {}
    policy_identities: dict[str, str] = {}

    for record_id, record_class, commit_id, identity_hash, payload_json in rows:
        payload = _load_payload(record_id, record_class, payload_json)
        if record_class == "authorization_epoch":
            number = payload.get("epoch_number")
            fingerprint = payload.get("fingerprint")
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
                or not isinstance(fingerprint, str)
                or not fingerprint.startswith(_EPOCH_FINGERPRINT_PATTERN_PREFIX)
            ):
                raise QueryAuthorizationError(
                    f"record={record_id!r}: malformed authorization epoch "
                    "(epoch_number/fingerprint); refusing to resolve "
                    "authorization from corrupt state"
                )
            if (commit_id, record_id) > (epoch_commit, epoch_id):
                epoch_commit, epoch_id = commit_id, record_id
                epoch_number = number
                epoch_fingerprint = fingerprint
        elif record_class == "security_domain":
            source_ref = payload.get("source_ref")
            domain_key = payload.get("domain_key")
            if not (
                isinstance(source_ref, str)
                and source_ref
                and isinstance(domain_key, str)
                and domain_key
            ):
                raise QueryAuthorizationError(
                    f"record={record_id!r}: malformed security-domain "
                    "assignment (source_ref/domain_key); refusing to resolve "
                    "authorization from corrupt state"
                )
            key = (commit_id, record_id)
            if key > (assignment_commit.get(source_ref, -1), assignment_order.get(source_ref, "")):
                assignment_commit[source_ref] = commit_id
                assignment_order[source_ref] = record_id
                domain_assignments[source_ref] = domain_key
        elif record_class == "access_denial":
            target_kind = payload.get("target_kind")
            target_ref = payload.get("target_ref")
            denied = payload.get("denied")
            if (
                target_kind not in ("domain", "source", "record")
                or not isinstance(target_ref, str)
                or not target_ref
                or not isinstance(denied, bool)
            ):
                raise QueryAuthorizationError(
                    f"record={record_id!r}: malformed access-denial event "
                    "(target_kind/target_ref/denied); refusing to resolve "
                    "authorization from corrupt state"
                )
            target = (target_kind, target_ref)
            key = (commit_id, record_id)
            if key > (denial_commit.get(target, -1), denial_order.get(target, "")):
                denial_commit[target] = commit_id
                denial_order[target] = record_id
                denial_state[target] = denied
            if commit_id > deny_revision:
                deny_revision = commit_id
        else:  # access_policy_revision
            source_ref = payload.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref:
                raise QueryAuthorizationError(
                    f"record={record_id!r}: malformed access-policy revision "
                    "(source_ref); refusing to resolve authorization from "
                    "corrupt state"
                )
            if commit_id >= policy_identity_commit.get(source_ref, -1):
                policy_identity_commit[source_ref] = commit_id
                policy_identities[source_ref] = identity_hash

    denied_domains = frozenset(
        ref for (kind, ref), denied in denial_state.items()
        if kind == "domain" and denied
    )
    denied_sources = frozenset(
        ref for (kind, ref), denied in denial_state.items()
        if kind == "source" and denied
    )
    denied_records = frozenset(
        ref for (kind, ref), denied in denial_state.items()
        if kind == "record" and denied
    )

    policy_view = {
        "profile": AUTHORIZATION_PROFILE_LOCAL,
        "epoch_number": epoch_number,
        "epoch_fingerprint": epoch_fingerprint,
        "domain_assignments": dict(sorted(domain_assignments.items())),
        "denied_domains": sorted(denied_domains),
        "denied_sources": sorted(denied_sources),
        "denied_records": sorted(denied_records),
        "access_policy_identities": dict(sorted(policy_identities.items())),
    }
    policy_digest = payload_byte_hash(
        canonical_json_bytes(to_json_ready(policy_view))
    )

    return EffectiveAuthorization(
        profile=AUTHORIZATION_PROFILE_LOCAL,
        assurance=assurance,
        workspace_id=workspace_id,
        epoch_number=epoch_number,
        epoch_fingerprint=epoch_fingerprint,
        domain_assignments=dict(domain_assignments),
        denied_domains=denied_domains,
        denied_sources=denied_sources,
        denied_records=denied_records,
        deny_revision=deny_revision,
        policy_digest=policy_digest,
    )
