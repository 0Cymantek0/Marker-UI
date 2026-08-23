"""Server-derived operational "as-of" contract for conversion jobs.

Readiness invariant 56 (``stale-review-rejection``) requires operational
status, history, and export surfaces to state which authoritative state a
representation refers to, and to refuse silent substitution when that state
moves between observation and action.

The contract is deliberately **derived, never persisted**: the state token
is a pure function of the durable ``ConversionJob`` row. There is no second
authority that can drift — when the row moves (regenerate changes cached
formats, lifecycle status advances, artifacts are purged), the derived token
moves with it. Nothing is cached, so a process restart cannot erase the
evidence needed to detect staleness; derivation *is* the persistence proof.

Material dimensions of the token preimage — every dimension named by the
invariant that genuinely applies to this surface:

* ``job_id``             — semantic result identity/scope. A token minted for
                           one job can never verify against another, so
                           cross-result replay fails closed by construction.
* lifecycle status       — completeness/operational outcome state.
* ``result_digest``      — digest over the cached per-format output content,
                           i.e. the exported truth itself.
* ``source_revision_id`` — kernel content revision the source was acquired
                           into, when source acquisition committed one.
* ``config_digest``      — digest over the stored conversion configuration.
                           A convert job's config is its policy context
                           (engine, OCR, image handling, every knob); it is
                           pinned at creation and never rotates, so config
                           is a stable policy dimension here rather than a
                           rotating one.
* ``artifacts_purged``   — whether the export package lost sidecar artifacts.

Publication/policy rotation does not apply to conversion jobs: a job is not
bound to a kernel publication set. That authority lives in the extraction
review subsystem, which keeps its own proven stale rejection
(``test_extraction_review.py``). This contract honestly reports the
dimensions the convert surface actually has instead of fabricating
meaningless ones.

Deliberately excluded from the preimage: ``updated_at``/lease/timestamp
columns (rotate on every write and would manufacture false staleness) and
any wall-clock value (a mutable display timestamp is not semantic state).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.services.format_store import parse_formats as parse_cached_formats
from app.services.source_acquisition import SOURCE_CONFIG_KEY
from app.utils.canonical import (
    canonical_json_bytes,
    payload_byte_hash,
    record_identity_hash,
    to_json_ready,
)

AS_OF_SCHEMA_VERSION = "marker.operational.as_of.v1"

# Verified mode: the caller presented an observed state token and the server
# compared it against the current derivation before acting.
MODE_VERIFIED = "verified"
# Historical mode: the caller made no currency claim, so the action proceeds
# against the stored representation and the response is labeled with the
# actual current state (never with an implied "current as observed").
MODE_HISTORICAL = "historical"

# Lifecycle status -> operational outcome state. A fresh-but-incomplete job
# and a historically-complete-but-stale job must stay distinguishable, so
# completeness is its own vocabulary instead of a boolean freshness flag.
COMPLETENESS_BY_STATUS = {
    "pending": "incomplete",
    "processing": "incomplete",
    "completed": "complete",
    "failed": "failed",
    "cancelled": "cancelled",
}


class AsOfContract(BaseModel):
    """Machine-visible as-of envelope returned by status/history responses.

    ``state_token`` is the only field clients need to round-trip; every
    other field exists so a UI can explain the represented state without
    reverse-engineering internal hashes. The token itself is an opaque,
    server-minted digest — a browser cannot manufacture freshness.
    """

    schema_version: str
    state_token: str
    completeness: str
    result_digest: str | None = None
    source_revision_id: str | None = None
    config_digest: str | None = None
    artifacts_purged: bool = False


@dataclass(frozen=True)
class AsOfVerification:
    """Result of comparing an observed token against current derivation."""

    mode: str  # MODE_VERIFIED | MODE_HISTORICAL
    fresh: bool  # True iff the observed token matches the current state
    observed: str | None
    current: AsOfContract


def _parse_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _source_revision_id(config: dict[str, Any] | None) -> str | None:
    block = (config or {}).get(SOURCE_CONFIG_KEY)
    if not isinstance(block, dict):
        return None
    revision_id = block.get("content_revision_id")
    if isinstance(revision_id, str) and revision_id:
        return revision_id
    return None


def _artifacts_purged(metadata_json: str | None) -> bool:
    metadata = _parse_json_object(metadata_json)
    if metadata is None:
        return False
    purged = metadata.get("purged_artifacts")
    return bool(purged)


def _output_content(job: Any, formats: dict[str, str]) -> dict[str, str]:
    """The exportable truth of a job: cached formats, with the legacy
    single-format ``result_text`` folded in exactly as the download route
    resolves it, so status and download can never disagree about content."""

    content = dict(formats)
    if "markdown" not in content and job.result_text:
        content["markdown"] = job.result_text
    return content


def derive_as_of(job: Any, *, effective_status: str | None = None) -> AsOfContract:
    """Derive the as-of envelope for a job row.

    Pure function of durable row state; safe to call on every request. The
    ``job`` argument is any object carrying the ``ConversionJob`` columns
    used below (the ORM row, or a test double).
    """

    status = effective_status or job.status
    config = _parse_json_object(job.config_json)
    formats = parse_cached_formats(job.formats_json) or {}
    content = _output_content(job, formats)
    completeness = COMPLETENESS_BY_STATUS.get(status, "incomplete")

    result_digest = (
        payload_byte_hash(canonical_json_bytes(to_json_ready(content)))
        if content or status == "completed"
        else None
    )
    config_digest = (
        payload_byte_hash(canonical_json_bytes(to_json_ready(config)))
        if config is not None
        else None
    )
    source_revision_id = _source_revision_id(config)
    artifacts_purged = _artifacts_purged(job.result_metadata_json)

    state_token = record_identity_hash(
        record_type="marker.operational.as_of",
        schema_version=AS_OF_SCHEMA_VERSION,
        payload={
            "job_id": job.id,
            "status": status,
            "result_digest": result_digest,
            "source_revision_id": source_revision_id,
            "config_digest": config_digest,
            "artifacts_purged": artifacts_purged,
        },
    )

    return AsOfContract(
        schema_version=AS_OF_SCHEMA_VERSION,
        state_token=state_token,
        completeness=completeness,
        result_digest=result_digest,
        source_revision_id=source_revision_id,
        config_digest=config_digest,
        artifacts_purged=artifacts_purged,
    )


def verify_as_of(
    job: Any,
    observed_token: str | None,
    *,
    effective_status: str | None = None,
) -> AsOfVerification:
    """Compare a caller-observed state token against the current state.

    A missing/empty token is not an error: the action falls into explicitly
    historical semantics (mode ``historical``). A supplied token is verified
    in constant time; any mismatch — stale observation, cross-job replay, or
    outright forgery — yields ``fresh=False`` for the route to reject.
    """

    current = derive_as_of(job, effective_status=effective_status)
    cleaned = observed_token.strip() if isinstance(observed_token, str) else None
    if not cleaned:
        return AsOfVerification(MODE_HISTORICAL, False, None, current)
    # Byte comparison: compare_digest rejects non-ASCII str, and a hostile
    # token must fail verification, not crash the route with a 500.
    fresh = secrets.compare_digest(
        cleaned.encode("utf-8"), current.state_token.encode("utf-8")
    )
    return AsOfVerification(MODE_VERIFIED, fresh, cleaned, current)
