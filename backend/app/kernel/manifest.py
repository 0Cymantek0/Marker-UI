"""Deterministic commit-manifest construction (V3.2 PR63A).

Pure functions shared by the commit path (write side) and the
replay/verification reader (read side). Both sides must derive identical
roots and manifest identity from identical inputs; tamper tests rely on
that equivalence. Stdlib + PR61 canonical utilities only — no ad-hoc
serialization, no timestamps in any identity input.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Mapping

from app.kernel.models import (
    KERNEL_SCHEMA_VERSION,
    MANIFEST_RECORD_TYPE,
    MANIFEST_SCHEMA_VERSION,
)
from app.utils.canonical import canonical_json_bytes, record_identity_hash

#: Entry used when a record carries no payload byte hash.
NO_PAYLOAD_MARKER = "-"


def record_root_entry(identity_hash: str, payload_byte_hash: str | None) -> str:
    """One per-record entry of the manifest record root."""
    return f"{identity_hash}|{payload_byte_hash or NO_PAYLOAD_MARKER}"


def edge_root_entry(source_ref: str, target_ref: str, edge_kind: str) -> str:
    """One per-edge entry of the manifest edge root."""
    return f"{source_ref}|{target_ref}|{edge_kind}"


def _root(entries: Iterable[str]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(sorted(entries))).hexdigest()


def compute_record_root(entries: Iterable[str]) -> str:
    """Aggregate record root: sha256 over sorted canonical entry list."""
    return _root(entries)


def compute_edge_root(entries: Iterable[str]) -> str:
    """Aggregate edge root: sha256 over sorted canonical entry list."""
    return _root(entries)


def manifest_identity_payload(
    *,
    workspace_id: str,
    kernel_commit_id: int,
    parent_kernel_commit_id: int,
    record_count: int,
    edge_count: int,
    record_class_counts: Mapping[str, int],
    record_identity_root: str,
    edge_identity_root: str,
    kernel_schema_version: str = KERNEL_SCHEMA_VERSION,
    canonicalization_profile: str,
) -> dict:
    """Semantic payload hashed into the manifest identity.

    Excludes the audit timestamp and any server-generated id: the
    manifest identity is a pure function of committed truth.
    """
    return {
        "workspace_id": workspace_id,
        "kernel_commit_id": kernel_commit_id,
        "parent_kernel_commit_id": parent_kernel_commit_id,
        "record_count": record_count,
        "edge_count": edge_count,
        "record_class_counts": dict(record_class_counts),
        "record_identity_root": record_identity_root,
        "edge_identity_root": edge_identity_root,
        "kernel_schema_version": kernel_schema_version,
        "canonicalization_profile": canonicalization_profile,
    }


def compute_manifest_identity_hash(payload: Mapping) -> str:
    """Identity hash of a manifest payload under the manifest framing domain."""
    return record_identity_hash(
        record_type=MANIFEST_RECORD_TYPE,
        schema_version=MANIFEST_SCHEMA_VERSION,
        payload=payload,
    )
