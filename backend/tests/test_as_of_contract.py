"""Invariant 56 — operational as-of truth + stale-state rejection.

This suite proves the convert status/history/export boundary carries a
server-derived as-of contract and refuses to act on stale observations:

* status/history expose the envelope (state token, completeness, digests);
* fresh observations download fine (verified mode);
* a REAL state change between observation and action (regenerate) turns the
  old token stale — the TOCTOU race is closed server-side;
* forged tokens, cross-job replay, and hostile input fail closed;
* tokenless downloads degrade to explicitly historical semantics, labeled
  with the actual current state — never an implied "current as observed";
* retrying a stale action never launders it into freshness;
* the token is a pure derivation of the durable row: restart-stable, and
  every material dimension (content, lifecycle, config, source revision,
  purge) rotates it, while operational noise (updated_at/lease writes)
  must NOT rotate it.

The extraction-review side of the invariant (stale publication rejection)
is proven separately in ``test_extraction_review.py`` and stays the
authority for that subsystem.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.job import ConversionJob

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed_job(job_id: str, *, formats: dict[str, str] | None = None,
                   config: dict | None = None, metadata: dict | None = None) -> ConversionJob:
    return ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name=f"{job_id.replace('job-', 'doc-')}.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# Converted output",
        config_json=json.dumps(config) if config is not None else None,
        result_metadata_json=json.dumps(metadata) if metadata is not None else None,
        formats_json=json.dumps(formats) if formats is not None else None,
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )


async def _add_job(db_session, job: ConversionJob) -> None:
    db_session.add(job)
    await db_session.commit()


async def _status_token(client: AsyncClient, job_id: str) -> dict:
    resp = await client.get(f"/api/convert/status/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    as_of = body.get("as_of")
    assert as_of, "status response must expose the as_of contract"
    return as_of


def _stub_render(monkeypatch, text: str = '{"blocks": ["rendered"]}') -> None:
    from app.main import _app_state

    fake_service = _app_state.conversion_service
    monkeypatch.setattr(
        fake_service,
        "convert_file_formats",
        lambda filepath, config, formats, device=None: {
            fmt: {"text": text, "extension": fmt, "images": {}, "metadata": {}}
            for fmt in formats
        },
    )
    monkeypatch.setattr(fake_service, "supports_multiple_formats", lambda filepath, config: True)


# ---------------------------------------------------------------------------
# Status / history exposure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_exposes_as_of_contract_for_completed_job(client: AsyncClient, db_session):
    job_id = "job-asof-complete"
    await _add_job(db_session, _completed_job(job_id, formats={"markdown": "# m", "json": "{}"}))

    resp = await client.get(f"/api/convert/status/{job_id}")
    assert resp.status_code == 200
    as_of = resp.json()["as_of"]
    assert as_of["schema_version"] == "marker.operational.as_of.v1"
    assert as_of["state_token"].startswith("sha256:")
    assert as_of["completeness"] == "complete"
    assert as_of["result_digest"] and as_of["result_digest"].startswith("sha256:")
    # No config/source acquisition on this row: dimensions honestly absent.
    assert as_of["config_digest"] is None
    assert as_of["source_revision_id"] is None
    assert as_of["artifacts_purged"] is False


@pytest.mark.asyncio
async def test_status_completeness_tracks_lifecycle_not_just_currency(client: AsyncClient, db_session):
    """Completeness is its own vocabulary: a fresh-but-incomplete job and a
    failed/cancelled job must be distinguishable from a complete one."""
    for status, expected in (
        ("pending", "incomplete"),
        ("processing", "incomplete"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ):
        job_id = f"job-asof-{status}"
        job = _completed_job(job_id)
        job.status = status
        await _add_job(db_session, job)
        as_of = await _status_token(client, job_id)
        assert as_of["completeness"] == expected, status


@pytest.mark.asyncio
async def test_history_rows_carry_the_same_as_of_contract(client: AsyncClient, db_session):
    await _add_job(db_session, _completed_job("job-hist-1", formats={"markdown": "# a"}))
    await _add_job(db_session, _completed_job("job-hist-2", formats={"markdown": "# b"}))

    resp = await client.get("/api/convert/history")
    assert resp.status_code == 200
    jobs = {j["job_id"]: j for j in resp.json()["jobs"]}
    for job_id in ("job-hist-1", "job-hist-2"):
        entry = jobs[job_id]
        assert entry["as_of"]["state_token"].startswith("sha256:")
        assert entry["as_of"]["completeness"] == "complete"

    # Cross-surface consistency: history envelope == status envelope for the
    # same unchanged row (no parallel freshness interpretations).
    status_as_of = await _status_token(client, "job-hist-1")
    assert jobs["job-hist-1"]["as_of"] == status_as_of


# ---------------------------------------------------------------------------
# Fresh baseline + historical semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_with_matching_token_is_verified_and_serves_content(client: AsyncClient, db_session):
    job_id = "job-dl-fresh"
    await _add_job(db_session, _completed_job(job_id, formats={"markdown": "# fresh"}))

    as_of = await _status_token(client, job_id)
    resp = await client.get(f"/api/convert/download/{job_id}", params={"as_of": as_of["state_token"]})
    assert resp.status_code == 200
    assert resp.headers["x-marker-as-of-mode"] == "verified"
    assert resp.headers["x-marker-as-of-state"] == as_of["state_token"]
    assert resp.headers["x-marker-as-of-completeness"] == "complete"
    assert "# fresh" in resp.text


@pytest.mark.asyncio
async def test_download_without_token_is_explicitly_historical_and_labeled(client: AsyncClient, db_session):
    """Unclaimed exports must carry the ACTUAL current state, never an
    implied 'current as observed' — historical semantics, truthfully labeled."""
    job_id = "job-dl-hist"
    await _add_job(db_session, _completed_job(job_id, formats={"markdown": "# hist"}))

    current = await _status_token(client, job_id)
    resp = await client.get(f"/api/convert/download/{job_id}")
    assert resp.status_code == 200
    assert resp.headers["x-marker-as-of-mode"] == "historical"
    # The tokenless response still tells the caller which state it served.
    assert resp.headers["x-marker-as-of-state"] == current["state_token"]


# ---------------------------------------------------------------------------
# The race: real state change between observation and action
# ---------------------------------------------------------------------------


async def _regenerate(client: AsyncClient, db_session, monkeypatch, job_id: str,
                      fmt: str = "json", token: str | None = None) -> dict:
    """Drive a REAL regenerate (mutates formats_json through the route)."""
    from app.core.config import UPLOAD_DIR

    uploads = Path(UPLOAD_DIR)
    uploads.mkdir(parents=True, exist_ok=True)
    src = uploads / f"{job_id}.pdf"
    src.write_bytes(b"%PDF-1.4 as-of source")

    _stub_render(monkeypatch)
    params = {"format": fmt}
    if token is not None:
        params["as_of"] = token
    try:
        resp = await client.post(f"/api/convert/{job_id}/regenerate", params=params)
    finally:
        src.unlink(missing_ok=True)
    db_session.expire_all()
    return resp


@pytest.mark.asyncio
async def test_download_rejects_stale_token_after_real_state_change(client: AsyncClient, db_session, monkeypatch):
    """TOCTOU: observe S -> regenerate moves the row to S2 -> act on S.

    The server must not silently serve S2 content to a caller whose observed
    representation is S."""
    job_id = "job-toctou"
    await _add_job(db_session, _completed_job(job_id, formats={"markdown": "# v1"}))

    observed = (await _status_token(client, job_id))["state_token"]

    resp = await _regenerate(client, db_session, monkeypatch, job_id)
    assert resp.status_code == 200, resp.text

    stale = await client.get(f"/api/convert/download/{job_id}", params={"as_of": observed})
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    # Machine-distinguishable stale error, not prose parsing.
    assert detail["code"] == "stale_state"
    assert detail["observed_state_token"] == observed
    assert detail["current_state_token"] != observed
    assert detail["current_as_of"]["state_token"] == detail["current_state_token"]

    # Refresh + retry against current state succeeds: stale safety must not
    # brick the happy path.
    refreshed = (await _status_token(client, job_id))["state_token"]
    ok = await client.get(f"/api/convert/download/{job_id}", params={"as_of": refreshed})
    assert ok.status_code == 200
    assert ok.headers["x-marker-as-of-mode"] == "verified"
    assert ok.headers["x-marker-as-of-state"] == refreshed


@pytest.mark.asyncio
async def test_retrying_a_stale_download_stays_stale(client: AsyncClient, db_session, monkeypatch):
    """Retry must not launder staleness: same old token, same typed 409, and
    no duplicate mutation side effect (regenerate already ran once)."""
    job_id = "job-retry-stale"
    await _add_job(db_session, _completed_job(job_id, formats={"markdown": "# v1"}))
    observed = (await _status_token(client, job_id))["state_token"]

    assert (await _regenerate(client, db_session, monkeypatch, job_id)).status_code == 200

    for _ in range(2):
        again = await client.get(f"/api/convert/download/{job_id}", params={"as_of": observed})
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "stale_state"

    # The regenerate mutation happened exactly once (json present once).
    row = (await db_session.execute(select(ConversionJob).where(ConversionJob.id == job_id))).scalar_one()
    formats = json.loads(row.formats_json)
    assert set(formats) == {"markdown", "json"}


@pytest.mark.asyncio
async def test_regenerate_honors_precondition_before_mutating(client: AsyncClient, db_session, monkeypatch):
    """Regenerate is itself a mutating action on an observed result: a stale
    precondition must refuse BEFORE the row changes; a fresh one passes."""
    job_id = "job-regen-pre"
    await _add_job(db_session, _completed_job(job_id, formats={"markdown": "# v1"}))
    observed = (await _status_token(client, job_id))["state_token"]

    # First regenerate WITH the matching token: fresh precondition passes.
    resp = await _regenerate(client, db_session, monkeypatch, job_id, token=observed)
    assert resp.status_code == 200

    # Second regenerate still pinned to the OLD token: refused, no mutation.
    refused = await _regenerate(client, db_session, monkeypatch, job_id, fmt="html", token=observed)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stale_state"
    row = (await db_session.execute(select(ConversionJob).where(ConversionJob.id == job_id))).scalar_one()
    assert set(json.loads(row.formats_json)) == {"markdown", "json"}


# ---------------------------------------------------------------------------
# Forged / cross-result / hostile input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forged_token_fails_closed(client: AsyncClient, db_session):
    job_id = "job-forged"
    await _add_job(db_session, _completed_job(job_id, formats={"markdown": "# f"}))

    resp = await client.get(
        f"/api/convert/download/{job_id}",
        params={"as_of": "sha256:" + "0" * 64},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "stale_state"


@pytest.mark.asyncio
async def test_cross_job_token_replay_fails_closed(client: AsyncClient, db_session):
    """A valid token from job A must not authorize acting on job B: the
    token preimage binds the job id, so replay fails by derivation."""
    await _add_job(db_session, _completed_job("job-cross-a", formats={"markdown": "# a"}))
    await _add_job(db_session, _completed_job("job-cross-b", formats={"markdown": "# b"}))
    token_a = (await _status_token(client, "job-cross-a"))["state_token"]
    assert token_a != (await _status_token(client, "job-cross-b"))["state_token"]

    resp = await client.get(f"/api/convert/download/job-cross-b", params={"as_of": token_a})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "stale_state"
    assert detail["observed_state_token"] == token_a
    assert detail["current_state_token"] != token_a


@pytest.mark.asyncio
async def test_hostile_non_ascii_token_fails_closed_not_500(client: AsyncClient, db_session):
    job_id = "job-hostile"
    await _add_job(db_session, _completed_job(job_id, formats={"markdown": "# h"}))

    resp = await client.get(f"/api/convert/download/{job_id}", params={"as_of": "tökén-≠-ascii"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "stale_state"


# ---------------------------------------------------------------------------
# Derivation integrity: dimensions, stability, noise immunity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_token_rotation_covers_every_material_dimension(db_session):
    """Every material dimension named by the contract rotates the token;
    without the dimension change the token is byte-identical (pure derivation,
    restart-stable — persistence proof is derivation itself, nothing cached)."""
    from app.operational.as_of import derive_as_of

    base = _completed_job("job-rotate", formats={"markdown": "# v1"}, config={"engine": "text"})
    token_base = derive_as_of(base).state_token

    # Identical row -> identical token (no clock in the preimage).
    assert derive_as_of(base).state_token == token_base

    # 1. Output content changes (regenerate-style).
    job = _completed_job("job-rotate", formats={"markdown": "# v1", "json": "{}"}, config={"engine": "text"})
    assert derive_as_of(job).state_token != token_base

    # 2. Content changes via the legacy single-format path: without a cached
    #    markdown entry, result_text IS the exportable markdown (mirrors the
    #    download route's fold), so moving it moves the token. With markdown
    #    already cached, result_text is not exportable and must NOT rotate it.
    job = _completed_job("job-rotate", formats={"json": "{}"}, config={"engine": "text"})
    before = derive_as_of(job).state_token
    job.result_text = "# v2"
    assert derive_as_of(job).state_token != before

    # 3. Lifecycle / completeness changes.
    job = _completed_job("job-rotate", formats={"markdown": "# v1"}, config={"engine": "text"})
    job.status = "cancelled"
    assert derive_as_of(job).state_token != token_base

    # 4. Policy context (conversion config) changes.
    job = _completed_job("job-rotate", formats={"markdown": "# v1"}, config={"engine": "ocr"})
    assert derive_as_of(job).state_token != token_base

    # 5. Kernel source revision binding appears when acquisition committed.
    job = _completed_job(
        "job-rotate",
        formats={"markdown": "# v1"},
        config={"engine": "text", "source_revision": {"content_revision_id": "rev-1"}},
    )
    with_rev = derive_as_of(job)
    assert with_rev.source_revision_id == "rev-1"
    assert with_rev.state_token != token_base

    # 6. Artifact purge state changes.
    job = _completed_job(
        "job-rotate", formats={"markdown": "# v1"}, config={"engine": "text"},
        metadata={"purged_artifacts": ["a.png"]},
    )
    assert derive_as_of(job).artifacts_purged is True
    assert derive_as_of(job).state_token != token_base

    # 7. Job identity itself: another job with identical everything else.
    twin = _completed_job("job-rotate-2", formats={"markdown": "# v1"}, config={"engine": "text"})
    assert derive_as_of(twin).state_token != token_base


@pytest.mark.asyncio
async def test_state_token_ignores_operational_noise(db_session, monkeypatch):
    """Lease/progress/updated_at churn must NOT manufacture false staleness —
    only material dimensions rotate the token."""
    from app.operational.as_of import derive_as_of

    job = _completed_job("job-noise", formats={"markdown": "# v1"}, config={"engine": "text"})
    token = derive_as_of(job).state_token

    job.updated_at = datetime.now(timezone.utc)
    job.lease_owner = f"worker-{uuid.uuid4()}"
    job.lease_expires_at = datetime.now(timezone.utc)
    job.progress = 100
    job.error_message = None

    assert derive_as_of(job).state_token == token


@pytest.mark.asyncio
async def test_as_of_derives_from_durable_row_not_live_progress(client: AsyncClient, db_session, monkeypatch):
    """Live task-manager progress must not mint a token the export boundary
    would then reject.

    The status route merges in-memory progress for non-terminal jobs, so a
    worker can report ``completed`` before the durable row is finalized. If
    the envelope derived from that ephemeral status, the token a client
    observed in that window could never verify at download (which reads the
    durable row) — false staleness on a perfectly honest client. Deriving
    from the row keeps status, history, and export on one derivation."""
    from app.main import _app_state
    from app.operational.as_of import derive_as_of

    job_id = "job-live-merge"
    job = _completed_job(job_id, formats={"markdown": "# pending"})
    job.status = "pending"
    job.progress = 0
    job.completed_at = None
    await _add_job(db_session, job)

    monkeypatch.setattr(
        _app_state.task_manager,
        "get_status",
        lambda jid: {"job_id": jid, "status": "completed", "progress": 100},
    )

    resp = await client.get(f"/api/convert/status/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    # The live status still drives the progress UX...
    assert body["status"] == "completed"
    # ...but the as-of envelope reports the durable truth, and matches the
    # derivation the download boundary will perform.
    assert body["as_of"]["completeness"] == "incomplete"
    db_session.expire_all()
    row = (await db_session.execute(select(ConversionJob).where(ConversionJob.id == job_id))).scalar_one()
    assert body["as_of"]["state_token"] == derive_as_of(row).state_token

    # History (no live merge at all) agrees with status for the same row.
    history = await client.get("/api/convert/history")
    entry = {j["job_id"]: j for j in history.json()["jobs"]}[job_id]
    assert entry["as_of"] == body["as_of"]

    # Once the row is durably completed, the token observed then verifies at
    # download — no spurious stale rejection.
    row.status = "completed"
    row.completed_at = datetime.now(timezone.utc)
    db_session.add(row)
    await db_session.commit()

    token = (await _status_token(client, job_id))["state_token"]
    ok = await client.get(f"/api/convert/download/{job_id}", params={"as_of": token})
    assert ok.status_code == 200
    assert ok.headers["x-marker-as-of-mode"] == "verified"


@pytest.mark.asyncio
async def test_failed_job_download_still_blocked_and_status_carries_as_of(client: AsyncClient, db_session):
    """The as-of contract does not weaken existing guards: a failed job is
    still not exportable, while its status honestly reports completeness."""
    job_id = "job-failed-guard"
    job = _completed_job(job_id)
    job.status = "failed"
    job.error_message = "boom"
    await _add_job(db_session, job)

    as_of = await _status_token(client, job_id)
    assert as_of["completeness"] == "failed"
    resp = await client.get(f"/api/convert/download/{job_id}", params={"as_of": as_of["state_token"]})
    assert resp.status_code == 400
