# Operational As-Of Truth and Stale-State Rejection (PR90)

**Status:** merged on `markerui-v2`.
**Readiness invariant:** 56 — `stale-review-rejection` (group 23C.6).
**Scope statement:** the operational conversion surfaces (job status,
history, export, format regeneration) state which authoritative state a
representation refers to, and refuse to act as though a stale
observation were current. The extraction review subsystem keeps its own
publication-scoped stale rejection; this slice extends the truth
boundary outward to the product surfaces the user actually drives.

## Contracts

| Contract | Identity | Owner |
|---|---|---|
| Operational as-of envelope | `marker.operational.as_of.v1` | `backend/app/operational/as_of.py` |
| Status/history exposure | `JobStatusResponse.as_of` | `backend/app/models/schemas.py` |
| Export/regeneration precondition | `?as_of=<state_token>` | `backend/app/routes/convert.py` |
| Typed rejection | `409` + `detail.code = "stale_state"` | `backend/app/routes/convert.py` |
| Client boundary | `AsOfContract`, `ApiError` | `frontend/src/lib/api.ts` |
| Accessible exposure | `role="status"` as-of region | `frontend/src/components/features/as-of/AsOfStatus.tsx` |

## The envelope

`derive_as_of(job)` is a pure function of the durable `ConversionJob`
row — never of in-memory task-manager progress, so an ephemeral live
status cannot mint a token that the export boundary would then reject as
stale, and status/history/export always derive the same token for the
same row. Nothing is cached or persisted, so there is no second authority
that can drift from the row, and a process restart cannot erase the
information needed to detect staleness — the derivation *is* the
persistence proof.

```json
{
  "schema_version": "marker.operational.as_of.v1",
  "state_token": "sha256:…",
  "completeness": "complete",
  "result_digest": "sha256:…",
  "source_revision_id": null,
  "config_digest": "sha256:…",
  "artifacts_purged": false
}
```

`state_token` is a domain-separated identity hash
(`record_identity_hash`, same framing as kernel record identity) over
the material dimensions:

| Dimension | Meaning | Rotates when |
|---|---|---|
| `job_id` | semantic result identity/scope | never — binds the token to one job, so cross-result replay fails by construction |
| lifecycle status | completeness/operational outcome | the job advances/fails/cancels |
| `result_digest` | the exportable truth itself (cached formats, with the legacy `result_text` folded in exactly as the download route resolves it) | regeneration or any output change |
| `source_revision_id` | kernel content revision the source was acquired into | source acquisition commits a different revision |
| `config_digest` | the job's conversion configuration — its policy context (engine, OCR, image handling, every knob) | the stored config changes |
| `artifacts_purged` | whether the export package lost sidecar artifacts | `/purge-files` runs |

Deliberately **excluded** from the preimage: `updated_at`, lease
columns, progress, and every wall-clock value. They rotate on ordinary
operational writes and would manufacture false staleness; a mutable
display timestamp is not semantic state.

Publication/policy *rotation* is not a dimension here because a
conversion job is not bound to a kernel publication set. That authority
lives in extraction review (`backend/app/extraction/service.py`,
`apply_review` → `StaleReviewError`), which continues to reject review
commits whose publication has moved. This contract reports the
dimensions the convert surface genuinely has rather than fabricating a
meaningless policy field.

## Completeness is not freshness

`completeness` is its own vocabulary (`complete`, `incomplete`,
`failed`, `cancelled`) so a fresh-but-incomplete result and a
historically-complete-but-stale result stay distinguishable. Freshness
is the token comparison; completeness is the operational outcome. A
single ambiguous boolean could not explain either.

## Action semantics

| Caller behavior | Server behavior | Response |
|---|---|---|
| supplies `as_of` matching the current derivation | verifies, then serves | `200`, `X-Marker-As-Of-Mode: verified` |
| supplies `as_of` that no longer matches (stale, forged, cross-job, hostile input) | refuses before acting | `409`, `detail.code = "stale_state"` with `current_as_of` |
| omits `as_of` | serves the stored representation as an explicitly historical export | `200`, `X-Marker-As-Of-Mode: historical`, labeled with the **actual current** state |

Every export response carries `X-Marker-As-Of-State`,
`X-Marker-As-Of-Mode`, and `X-Marker-As-Of-Completeness`, so a response
can never be read as a current export when it is not. The `409` body
includes the refreshed envelope, so a client recovers without a second
round-trip.

`POST /{job_id}/regenerate` honors the same precondition and checks it
*before* mutating `formats_json`. Retrying a stale action never
launders it into freshness: the observed token is still stale, so the
same typed rejection repeats and no duplicate mutation occurs.

The server is the arbiter. A crafted request bypassing the UI is
evaluated identically; disabling a button is UX, not enforcement.

## Frontend exposure

`JobStatus.as_of` carries the envelope through the typed boundary.
`ApiError` (`status`, `code`, `currentAsOf`) makes a stale rejection
machine-distinguishable from a generic network failure, and callers
adopt `currentAsOf` from the payload so the recovery path uses the
current token.

`AsOfStatus` renders a `Badge` (`Current` / `Stale` / `Incomplete` /
`Failed` / `Cancelled`) inside a `role="status" aria-live="polite"`
region with a text statement of the state — WCAG 2.2 SC 4.1.3: the
transition is programmatically determinable without moving focus, and
never encoded by color alone. It appears on the live result preview
(`OutputViewer` via `ConvertPage`) and in expanded history rows
(`HistoryPage`), which offer `Refresh` / `Retry download` recovery.

## Evidence

| Surface | Proof |
|---|---|
| Server contract + adversarial cases | `backend/tests/test_as_of_contract.py` (15 tests) |
| Review-commit staleness (pre-existing authority) | `backend/tests/test_extraction_review.py` |
| Typed client boundary | `frontend/src/__tests__/api.test.ts` |
| Accessible as-of exposure | `frontend/src/components/features/as-of/AsOfStatus.test.tsx` |
| Result-surface rendering | `frontend/src/__tests__/OutputViewer.test.tsx` |
| History truthfulness + stale retry | `frontend/src/__tests__/HistoryPage.test.tsx` |
| End-to-end stale rejection + refresh | `frontend/src/__tests__/ConvertPageIntegration.test.tsx` |
| Combined execution artifact | `docs/reference/measurements/pr90-as-of-evidence.json` (generated by `backend/scripts/bench_pr90_as_of.py`) |

Regenerate the artifact with:

```bash
python backend/scripts/bench_pr90_as_of.py
```

The readiness auditor derives invariant 56 from those bindings; the
status is never hand-set.
