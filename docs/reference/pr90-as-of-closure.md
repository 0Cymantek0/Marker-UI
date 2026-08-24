# Operational As-Of Truth / Stale-State Rejection Closure (PR90)

**Status:** merged on `markerui-v2`.
**Readiness invariant:** 56 — `stale-review-rejection` (group 23C.6), the
last remaining 23C.6 gap.
**Movement:** 48/62 proven → **49/62 proven** (0 failed, 13 with no
acceptable evidence; overall verdict remains NOT_READY).
**Focused plan:** `planning/v2/2026-08-24-marker-ui-v2-invariant-56-operational-as-of-plan.md`.

## 1. What changed

The convert operational surfaces — job status, history, export/download,
and format regeneration — now carry a server-derived **as-of contract**
(`marker.operational.as_of.v1`). Every representation states the
authoritative state it refers to via a `state_token`, a domain-separated
identity hash over exactly the material dimensions of that surface: job
identity, lifecycle/completeness, output content digest, kernel source
revision, config digest, and artifact-purge state. The token is derived,
never persisted: there is no second authority to drift, derivation is
restart-stable by construction, and it reads only the durable row so
ephemeral task-manager progress can never mint a token the export
boundary would reject.

Actions honor the observed state. A download or regeneration submitted
with a previously observed token is re-derived and verified server-side;
a mismatch — real staleness after regeneration/lifecycle change, forged
token, cross-result replay, hostile input — returns a typed
`409 stale_state` body carrying the refreshed envelope. Tokenless
downloads degrade to explicitly historical exports labeled with the
actual current state via `X-Marker-As-Of-*` headers. The frontend typed
boundary (`AsOfContract`, `ApiError`) carries the envelope and makes the
rejection machine-distinguishable; `AsOfStatus` exposes current/stale/
incomplete/failed/cancelled as an accessible `role="status"` region
(WCAG 2.2 SC 4.1.3) on the result preview and history details, with
refresh/retry paths that adopt the refreshed token from the 409 payload.

Extraction review keeps its pre-existing publication-scoped stale
rejection (`StaleReviewError`) as the authority for review commits; this
slice extended the truth boundary outward instead of duplicating it.

## 2. Why this design

The plan left representation open. Chosen: pure derivation from durable
row state, because the alternative designs each create a failure mode
the invariant forbids — persisted `is_stale` drifts from truth (duplicate
authority), HTTP ETags would have needed a server-side identity anyway
(the row has no version column), and wall-clock freshness manufactures
false staleness on every operational write. Deriving over the same
canonical framing used by kernel record identity (`record_identity_hash`)
keeps one identity discipline across the repository. Publication/policy
rotation was deliberately not fabricated for this surface: conversion
jobs are not bound to publication sets, and the plan required proving
why rather than inventing a meaningless dimension. Completeness stays a
separate vocabulary (`complete/incomplete/failed/cancelled`) so
freshness and operational outcome never collapse into one ambiguous
boolean.

## 3. Files

| File | Role |
|---|---|
| `backend/app/operational/as_of.py` | new — envelope derivation + constant-time verification |
| `backend/app/models/schemas.py` | `JobStatusResponse.as_of` |
| `backend/app/routes/convert.py` | status/history exposure; download/regenerate precondition; 409 body; as-of response headers |
| `frontend/src/lib/api.ts` | `AsOfContract`; `ApiError(status, code, currentAsOf)`; token params |
| `frontend/src/components/features/as-of/AsOfStatus.tsx` | accessible as-of region |
| `frontend/src/hooks/useConversionQueue.tsx` | envelope capture; stale flag; refresh/recovery |
| `frontend/src/components/features/OutputViewer.tsx` | preview exposure |
| `frontend/src/pages/ConvertPage.tsx` | wiring + refresh action |
| `frontend/src/pages/HistoryPage.tsx` | details-panel exposure; stale retry flow |
| `docs/reference/operational-as-of-truth.md` | reference contract doc |
| `docs/api/convert.md` | API docs for `as_of` + precondition semantics |

## 4. Behavior proof

Backend (`backend/tests/test_as_of_contract.py`, 15 tests): envelope
exposure + completeness vocabulary; history/status cross-surface
equality; fresh verified download; explicitly historical tokenless
download; **TOCTOU** — a real regenerate between observation and action
turns the old token into a typed 409 and refresh recovers the happy
path; retry never launders staleness and causes no duplicate mutation;
regenerate checks its precondition before mutating; forged, cross-job,
and non-ASCII tokens fail closed; the rotation matrix proves every
material dimension moves the token while lease/`updated_at` churn does
not; the live-progress window reports durable truth; failed jobs stay
unexportable.

Frontend (69 tests across api/AsOfStatus/OutputViewer/HistoryPage/
ConvertPageIntegration): param + typed-error behavior of the client;
accessible badge labels and announcements; end-to-end stale rejection on
the live result surface with Refresh adopting the refreshed token; the
history stale-retry flow using the token patched from the 409 payload.

Combined execution artifact:
`docs/reference/measurements/pr90-as-of-evidence.json`
(22 backend + 69 frontend green; named scenarios: TOCTOU stale download,
forged token, cross-job replay, extraction stale review, frontend stale
retry, history stale retry — all passed).

## 5. Commands and results

Session environment: Windows, CPython 3.11.9, SQLite (aiosqlite),
pytest-asyncio 0.24; frontend Node/pnpm, vitest 4.

- Focused backend suites (`test_as_of_contract.py`,
  `test_convert.py`, `test_extraction_review.py`): all green before
  each commit.
- Frontend gates on the final tree: `pnpm run lint` clean;
  `pnpm run test` → 22 files, **176 passed / 0 failed**; `pnpm run build`
  → success.
- Readiness evidence run at the closure tree: 86 bindings executed —
  **801 bound node outcomes passed**, 7 environment-gated skips, 0
  failed; auditor derives **49/62 proven** (invariant 56 proven in both
  `sqlite-dev` and `sqlite-dev + vitest jsdom`);
  `readiness_audit.py --mode integrity` passes with no report drift.
- Full backend regression (`tests` + `conformance`) on the final tree:
  see §7.

## 6. Integration findings closed inside the session

- The status route originally derived the envelope from the live-merged
  status; a worker reporting `completed` before row finalization could
  mint a token the export boundary would reject (false staleness against
  an honest client) and made status/history disagree. Fixed to derive
  from the durable row only, with a dedicated regression test.
- The subagent's first integration-test draft consumed download mocks in
  the wrong order (the completion auto-download's status fetch never ran,
  so the viewer never rendered); rewritten around the real hook flow.
- `regenerateFormat` errors flowed through the generic `request()` path,
  so the hook's stale branch was unreachable; `request()` now throws the
  typed `ApiError` (legacy message formats preserved).

## 7. Regression record

Full backend regression on the final frozen tree:

```text
3696 passed, 208 skipped, 0 failed in 5m06s (exit 0)
```

The skips are the branch's known environment-gated lanes (GPU/CUDA,
docker postgres/S3, source-ingress timeouts); no lane intentionally left
unexecuted beyond those gates.

## 8. Residuals (explicitly not this phase)

- No review/approval UI product exists yet; per the focused plan the
  supported surfaces were closed honestly rather than inventing one.
- Regeneration without a token mutates current state by design (it reads
  current state additively; there is no observed representation to
  substitute) — documented in the route contract.
- Remaining readiness gaps are separate focused sessions (59–62 release
  governance, 25/56 routing evidence, 30 CUDA, 37/38 industrial labs,
  43 SLO declaration).
