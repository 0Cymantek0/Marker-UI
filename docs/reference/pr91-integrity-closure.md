# Review-Integrity Browser Proof / Readiness Evidence Closure (PR91)

**Status:** prepared on `markerui-v2` (review-integrity slice merged in
`7d5b9d8`/`10679e2`/`2bd30d7`; this evidence phase is staged alongside).
**Readiness invariant:** 56 — `stale-review-rejection` (group 23C.6).
**Movement:** evidence deepened only — invariant 56 gains a **third
binding** (browser-level measurement); overall counts stay **49/62
proven**, 0 failed, 13 with no acceptable evidence (verdict NOT_READY).

## 1. What changed

Three executable halves now back invariant 56:

1. **Backend launcher** (`backend/e2e/launch.py`): boots the REAL FastAPI
   application — real routes, real SQLite database, real as-of enforcement,
   real auth middleware — on a throwaway scratch database seeded with one
   deterministic completed job, served over real HTTP by Uvicorn. The ONLY
   swapped seam is the conversion render path
   (`conversion_service.convert_file_formats` + its
   `supports_multiple_formats` gate), the same seam the backend test suite
   patches; the stub's output embeds a monotonically increasing render
   counter so every regenerate rotates `result_digest` and therefore the
   derived `state_token`, even across launcher sessions on a reused
   scratch dir.
2. **Dedicated review-integrity surface** (`/integrity`): a
   server-authoritative page — `JobPicker` (recent completed jobs + manual
   Job ID form), `RevisionContextCard` (full as-of envelope with
   truncation tooltips and copy-to-clipboard), `StaleBanner` (pinned vs
   current token comparison + Refresh current state), `ExportPanel`
   (verified download, extension derived from the response Blob's content
   type, never from configuration), with `IntegrityPage` owning the
   lifecycle state machine. The typed API boundary (`AsOfContract`,
   `ApiError` with `stale_state` + refreshed envelope) is the single
   client-side authority for what the server said.
3. **Real-browser Playwright suite** (`frontend/e2e/`, chromium): boots
   the launcher + the real vite dev server (proxying `/api`), then drives
   the full current → stale → rejected → reconciled → recovered lifecycle
   with real downloads materialized on disk, the picker/deep-link/manual
   entry round-trips, a conservative backend-unreachable failure with
   retry recovery, and a legacy smoke over the pre-existing surfaces.

## 2. Why this design

The as-of contract was already proven server-side (pytest) and
component-side (vitest jsdom); the remaining honest gap was whether a real
browser, against the real HTTP stack, actually exhibits the lifecycle the
invariant demands: verified export only against current state, stale
rejection that can never fake success, and recovery that adopts server
truth rather than cached optimism. Stopping short of a real browser would
have left the strongest claim (zero false success in the UI) resting on
mocked fetch. The launcher deliberately stubs only the render seam —
routes, DB, token derivation, and 409 precondition checks all stay real —
because the behavior under proof lives there, not in the converter.
Serialization (`workers: 1`) is mandatory: every spec shares the single
seeded job and rotates its token, so specs always re-read the CURRENT
token from the API before regenerating.

## 3. Files

| File | Role |
|---|---|
| `backend/e2e/launch.py` + `backend/e2e/README.md` | real-app E2E launcher (scratch SQLite, seeded job, render-seam stub) |
| `frontend/src/pages/IntegrityPage.tsx` | integrity surface lifecycle (load/stale/reconcile/recover) |
| `frontend/src/components/features/integrity/*` | `JobPicker`, `RevisionContextCard`, `StaleBanner`, `ExportPanel`, `CopyableValue`, `token.ts` |
| `frontend/src/lib/api.ts`, `frontend/src/lib/download.ts` | typed as-of boundary; content-type-derived file extension |
| `frontend/playwright.config.ts` + `frontend/e2e/*.spec.ts` + `frontend/e2e/support.ts` | chromium suite (lifecycle, entry, failure, legacy smoke) |
| `backend/scripts/bench_pr91_integrity_e2e.py` | PR91 measurement generator (this phase) |
| `backend/scripts/bench_pr90_as_of.py` | extended: integrity suites added to `FRONTEND_SPEC_FILES` + 2 pinned scenarios |
| `docs/reference/measurements/pr91-integrity-e2e-evidence.json` | new measurement artifact (invariant 56 binding 3) |
| `docs/reference/measurements/pr90-as-of-evidence.json` | regenerated (frontend 69 → 87 tests: +3 api, +11 IntegrityPage, +4 RevisionContextCard) |
| `docs/reference/readiness/readiness-ledger.json` | invariant 56: binding 2 extended, binding 3 appended |

## 4. Behavior proof

- **vitest integrity suites** (`IntegrityPage.test.tsx` 11 +
  `RevisionContextCard.test.tsx` 4): envelope rendering with human labels
  and tooltips; bookmarked-token staleness on load; 409 reconciliation
  without false success; conservative handling of a 409 missing the
  envelope; refresh-adopts-token then retry-verifies recovery; response
  without a verified mode treated as failure; picker round-trips;
  network-failure error with retry recovery.
- **Playwright chromium** (11 specs): current verified export with real
  downloaded markdown bytes; stale-after-load race (server rotates the
  pinned token → 409 → reconciling banner, download button disabled, zero
  browser downloads); stale-before-load bookmark detected immediately;
  recovery loop re-stales after a successful export (no cached optimism);
  picker deep-link + manual form + Change-job round-trip; unreachable
  backend → conservative `role=alert` with Retry recovery; legacy smoke
  (`/`, `/history`, `/settings`).
- **Artifacts**:
  `docs/reference/measurements/pr91-integrity-e2e-evidence.json` —
  vitest 15/15, e2e 11/11, 0 failed/skipped, 10 named scenarios all
  `passed`, verdict `integrity_surface_browser_proven`;
  `docs/reference/measurements/pr90-as-of-evidence.json` — backend 22/22,
  frontend 87/87, 8 named scenarios all `passed`, verdict unchanged.
- **Ledger binding 3** pins every scenario `passed`, `e2e.passed=11`,
  `vitest.passed=15`, zero failed/skipped, and the verdict string;
  binding 2 additionally pins the two new vitest scenarios
  (`integrity_page_stale_reconcile`, `revision_context_card`).

## 5. Commands and results

Session environment: Windows, CPython 3.11.9, SQLite (aiosqlite),
Playwright 1.62.1 (chromium), pnpm 10.33.2 / Node 24.

```text
python backend/scripts/bench_pr90_as_of.py
  → backend 22 passed / 0 failed, frontend 87 passed / 0 failed,
    8 scenarios passed, verdict as_of_operational_contract_proven (exit 0)

python backend/scripts/bench_pr91_integrity_e2e.py   (run twice, both green)
  → vitest 15 passed / 0 failed, e2e 11 passed / 0 failed,
    10 scenarios passed, verdict integrity_surface_browser_proven (exit 0)

pnpm run test   (frontend, full suite)
  → 24 files, 195 passed / 0 failed

python backend/scripts/readiness_audit.py --mode run-evidence
  → 91 bindings executed (was 90; +1 measurement binding),
    808 bound node outcomes: 87 bindings passed, 4 environment-gated
    skips, 0 failed; wall ~4m45s

python backend/scripts/readiness_audit.py --mode integrity
  → no findings, no report drift; proven=49 failed=0 no_evidence=13 (of 62)
```

Invariant 56 is now proven in three environments: `sqlite-dev`,
`sqlite-dev + vitest jsdom`, and `sqlite-dev + chromium`.

## 6. Residuals (explicitly not this phase)

- The browser suite exercises one seeded job in one output format
  (markdown); the multi-format/multi-job export matrix remains covered by
  the backend contract suite, not by chromium.
- The E2E render seam is deterministically stubbed (same seam as the
  backend suite); real model rendering under a browser is out of scope
  for readiness evidence.
- Other V2 screens (Convert, History) are not migrated to the
  `/integrity` pattern; they keep their existing as-of exposure and
  vitest coverage.
- The approvals clause of invariant 56 remains backend-authority only
  where no REST surface exists yet; no review/approval UI product beyond
  this integrity surface.
- Playwright specs are serialized (`workers: 1`) against the shared
  seeded job by design; parallelization would require per-worker job
  fixtures.
