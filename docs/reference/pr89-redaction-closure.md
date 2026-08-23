# PR89 — End-to-End Redaction Closure Across Derived Serving Paths

**Date:** 2026-08-24
**Branch base:** `f5c40a3336216016a77c2f94fb9e9071c3bded4c` (branch `markerui-v2`)
**Scope:** masterplan §8C.14 (redaction is an end-to-end security transformation), §9C.3 (deny overlay before release), §9C.4 (epoch change invalidates cursor), §16A.2 (every derived artifact resolves an effective access policy); readiness invariant **18** (`redaction-all-paths`).
**Migration:** none — redaction policy is kernel records (`redaction_profile` class in the existing `kernel_records` table), mirroring `security_domain`/`access_denial` storage.

---

## 1. The decision — release-time projection over retained derived state

Before PR89, `redaction_profile_id` was a caller-supplied string that
participated in EvidencePacket *identity* only. No code path inspected it;
nothing filtered, masked, or withheld content. The readiness ledger recorded
invariant 18 as `no_evidence` (gap type B).

PR89 makes redaction **server-authoritative and release-time**:

1. **Policy truth** — a `redaction_profile` kernel record per
   `(workspace, profile_id)`, committed through the kernel spine
   (`RedactionPolicyService`). Rules are a bounded literal/pattern grammar
   validated at the commit boundary; placeholders can never echo redacted
   material. Latest revision per profile is effective; supersession is a new
   record, never a mutation.
2. **Resolution** — `resolve_effective_redaction` derives the effective state
   from committed records only. A caller *names* a serving context; an
   unknown name fails closed (`QueryAuthorizationError`, policy_fail_closed
   at the surface). An omitted name resolves the committed `default` profile
   — omission is not an escape.
3. **Identity binding** — the resolved redaction state rides
   `EffectiveAuthorization` and its `identity_view()` (profile, revision,
   rules digest only — never the rules). Because packet identity and cursor
   binding already key on that view, every policy transition rotates packet
   identities and invalidates live cursors exactly like a deny/epoch change.
4. **Release projection** — the executor and continuation pager project the
   current rules over every releasable text: lexical hits are masked after
   the live-deny check, exact reads resolve to masked content, and a lexical
   hit that matched *only* redacted material is dropped (a placeholder row
   would confirm the redacted content's existence). Locator text hashes stay
   over the raw indexed bytes: provenance identity remains bound to the
   immutable generation, not to policy state.

**Effective boundary (the linearizable event):** the kernel commit of the
`redaction_profile` record. Redaction is resolved at initial authorization,
re-resolved per operation, and re-checked after each continuation page —
a commit that lands before a read is observed by that read; a commit that
lands mid-query linearizes before the next operation; a commit that lands
mid-chain invalidates the cursor before protected output.

## 2. Why this design wins

| | chosen: release-time projection | alternative: new redacted materialization per policy | simplest: whole-record deny overlay only |
|---|---|---|---|
| correctness | masks spans; drops existence-leaks; public content flows | same end state, but only after rebuild completes | denies whole records — destroys legitimate neighboring content, fixture-level selectivity impossible |
| immediacy | effective at commit; outruns any rebuild | effective only when the new generation is published | immediate |
| compatibility | no schema change, no PublicationSet field, no migration | new generation protocol, staging, acceptance per transition | reuses deny overlay but conflates redaction with denial semantics |
| stale safety | old generations remain releasable-safe (projected) | old generations must be fenced from release | same as deny overlay |
| testability | projection + identity rotation directly assertable | requires rebuild orchestration in every test | trivially testable but proves the wrong property |

The deny-overlay analogy (masterplan §9C.3: revocation must *outrun*
background reindexing) is the load-bearing precedent: immediate release
safety is a property of the release gate, not of physical derived-state
replacement. Physical cleanup of superseded generations continues to follow
existing pin-expiry/retention semantics.

**Simplest baseline that would pass the suite:** whole-record denial via
`AccessDenialRecord` (identity rotation and cursor invalidation come free).
Rejected: it cannot express selective masking, and the plan's sentinel
fixture requires public material to keep flowing from an affected corpus.

**Falsification/rollback condition:** if a future surface materializes that
bypasses `EffectiveAuthorization` resolution (e.g. a direct byte route keyed
only by scope), this design is unsafe for that surface and must be extended
there — the surface matrix is the regression check. Rollback is trivial and
non-destructive: redaction records are additive kernel records; removing the
projection restores prior behavior without touching source/evidence history.

## 3. Release-surface census

Machine-readable matrix:
[`docs/reference/readiness/pr89-redaction-surface-matrix.json`](readiness/pr89-redaction-surface-matrix.json)
— every reachable family (lexical, exact reads, packet/reuse, cursors,
disclosures, REST convert outputs, dense/visual operators, direct source
blobs) with authority, redaction binding, stale-object risk, post-change
behavior, and the proving test. Unsupported families (dense, visual, raw
blobs) carry explicit negative proof; operator-scope surfaces (convert job
outputs) are documented as outside the publication-serving domain with a
disjointness test.

## 4. Adversarial sentinel suite

`backend/tests/test_redaction_closure.py` — sentinel `MU_RED_7f3a9c2e4b`
published as ordinary content beside public material, then restricted by a
committed `default`-profile revision. Covered scenarios (plan §13.2
numbering): baseline retrieval (1), lexical/exact closure with selectivity
(2), packet/reuse identity rotation with content-level non-disclosure (4),
cursor closure + post-change chain walk (5), disclosure recording (6b),
cross-profile isolation (7), fail-closed unknown profiles (8), mid-query and
concurrent transition (9), failed re-materialization never reopens stale
content (10), restart recovery (11), unsupported visual/vector negative
proof (12), secondary-leakage sweep of every service-contributed packet
field (13), and policy relaxation without stale resurrection (14).
Dense retrieval (3) is not a supported path — covered by the same negative
proof as (12).

## 5. Verification record

Recorded in the regression record commit and the regenerated readiness
artifacts (`docs/reference/readiness/pr84a-evidence-run.json`,
`pr84a-readiness-report.{json,md}`). Focused suites: redaction closure,
resolution, records, context-runtime (representation, packets, service,
authorization, authz-retrieval), kernel publication. Full gate:
`python backend/scripts/readiness_audit.py --mode run-evidence` then
`--mode integrity`.

## 6. Residuals (explicitly not this phase)

- Disclosure-row retention/erasure after policy change (PR85 §7 residual;
  historical evidence stays inspectable under audit scopes).
- Physical deletion of sentinel bytes from superseded FTS generations
  (retention/GC owns derived-state cleanup; release safety does not wait
  for it).
- No general policy language, no visual-retrieval productization, no
  frontend work — unchanged scope boundaries from the focused plan.

## 7. Regression record

Session environment: Windows, CPython 3.11.9, SQLite (aiosqlite),
`python -X utf8 -m pytest` from `backend`.

- Focused PR89 suites (closure sentinel suite, resolution, profile
  records, context-runtime representation/packets/service/authorization/
  authz-retrieval, kernel publication, answer evidence + boundary,
  agent query adapter, continuation, cursor migration, pr82 agent):
  all green before each commit in this session.
- Readiness evidence run at the evidence head (`4eb9f22`): every bound
  binding executed — 786 node outcomes passed, 7 `skipped_env_gated`
  (docker/postgres industrial partial bindings, never backing a proven
  claim), 0 failed; auditor derives **48/62 proven** (invariant 18
  `redaction-all-paths` proven), integrity passes.
- Full backend regression (`tests` + `conformance`):
  **3663 passed, 208 skipped, 0 failed** in 51m49s. The 208 skips are
  the branch's known environment-gated lanes (GPU/CUDA, docker
  postgres/S3, source-ingress timeouts); no lane was intentionally left
  unexecuted beyond those gates.
- Two integration findings surfaced mid-session and were closed inside
  it: the pr82 hostile-document eval's authorization-view key contract
  needed the new digest-only redaction dimension (`d1adbb0`), and a
  first full-regression attempt executed against a tree edited mid-run
  (stale evidence scope blobs) — the recorded numbers above are from
  the final frozen-tree run only.
