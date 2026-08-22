<!-- Release-governing extract. DO NOT EDIT BY HAND.
     Regenerate with:
       python backend/scripts/readiness_governing_extract.py \
         --source planning/v2/marker-ui-v2-Masterplan.md --write
     Everything below the provenance block is the VERBATIM governing
     section from the master plan; integrity audits parse only that
     section, so any hand edit shows up as inventory drift. -->

<!-- extracted-items: 62 -->

## V3.2 amendment 23C - Adversarially hardened readiness definition

V3.2 readiness is additional to every V2, V3, and V3.1 gate. A release is not hardened because the document contains answers; each item below requires executable evidence from the declared topology and workload envelope.

### 23C.1 Truth and persistence

1. One transactional commit authority defines every live kernel mutation in each topology; per-document JSONL files are not the serving authority.
2. Every KernelSnapshot is identified by a committed sequence boundary and required payload state; timestamps alone do not establish membership or causality.
3. Multi-record mutations, decisions, publication pointers, source cursors, and effects survive crash injection without partial state masquerading as complete.
4. Blob identity and Observation/evidence identity remain separate under deduplication.
5. ClaimAssertion, ClaimAssessment, NativeObject/NativeFact, Decision, Invalidation, Patch, and ArtifactRevision semantics are implemented and versioned.
6. Canonical IDs pass cross-language/x86_64/ARM64 fixtures; raw Unicode, semantic normalization, decimal precision, and fixed-point geometry are not conflated.
7. Materialized reads meet the local SLA without replaying raw logical streams; every generation can be rebuilt from its pinned snapshot.
8. Proof-closure GC cannot delete inspectable/replayable dependencies required by live proofs, legal hold, audit, or active readers.
9. Schema evolution preserves historical field meaning and old-record readability; production startup performs no opportunistic schema self-heal.

### 23C.2 Geometry, patches, and incrementality

10. Source anchors use layered native/text/structure/geometry/state selectors and report exact, deterministic/reviewed mapped, semantic candidate, stale, or unresolved honestly.
11. Semantic similarity alone never establishes exact identity for a paraphrased/moved source.
12. Reading order is a partial-order graph with bounded contextual re-evaluation; specialist crop output cannot be inserted blindly.
13. Cross-page continuations and multi-page tables preserve all source fragments, alternatives, and provenance.
14. Non-commutative concurrent patches conflict or rebase under a tested rule; patch order cannot silently choose truth.
15. Reversal reconstructs a prior derived revision; no claim is made that a lossy view recreates discarded source information.
16. Dependency completeness is declared. Unknown semantic dependencies widen invalidation scope.
17. Randomized incremental updates produce the same declared outputs as clean rebuilds, or incrementality is disabled for that format/operator.
18. Redaction removes or denies every affected text, image, cache, index, visual vector, export, and cursor path required by policy.

### 23C.3 Verification and routing

19. `dependency_risk_profile` is treated as disclosure/risk metadata, not proof of model independence; unknown lineage is not counted as independent.
20. High-risk verification cannot be established by model agreement alone.
21. Proof graphs reject cycles/self-support and verify source/crop/topology integrity before accepting downstream validators.
22. Verification status is claim/region/workflow/policy/snapshot-relative and does not make an entire document unusable because one region is unresolved.
23. Calibration/risk artifacts name their population, sample, assumptions, confidence interval, shift tests, and expiry; zero observed catastrophic failures are not reported as zero risk.
24. Bounded continuation terminates under cycles, budget exhaustion, or repeated failure and emits a deterministic outcome.
25. EVC-style routing remains shadow/offline until it beats fixed rules and best-single-engine baselines under held-out shift and catastrophic utility.
26. Review coverage, queue time, and bypass behavior demonstrate that the verification policy is operationally usable.
27. The no-training constraint does not block transparent routing to user-supplied or managed trained specialists; the extraction displacement report compares direct specialist use.

### 23C.4 Runtime and jobs

28. Isolation occurs at runtime-family boundaries and passes a measured locality/copy/latency comparison against the monolithic baseline.
29. Large page/tensor data moves through verified artifact handles or an equally efficient fallback, not repeated JSON/gRPC serialization.
30. Admission uses the pinned preprocessor's actual/upper-bound visual-token and memory envelope; dynamic-resolution stress does not create uncontrolled OOM cascades.
31. Active model leases prevent eviction; cold-start/queue/load cost appears in routing and user-visible outcomes.
32. At-least-once duplicate execution is safe; exactly one fenced accepted publication occurs.
33. Heartbeat renewal requires a responsive control loop and active request evidence, not a blind detached timestamp updater.
34. Parent coordinators hold no scarce child slots; mixed large/small workloads meet fairness and bounded-fan-out gates.
35. Slow/disconnected event clients cannot block execution or terminal state; durable semantic events replay by authoritative sequence.
36. Progressive artifacts are provisional until a complete PublicationSet passes required gates; a late failure cannot expose a mixed stable generation.
37. External-effect semantics are declared as exactly-once, at-least-once, at-most-once, or reconciliation-required based on real destination primitives.
38. Cancellation, failover, disk/WAL/shared-memory pressure, database outage, and model-service crash produce truthful terminal/recovery outcomes.

### 23C.5 Source, authorization, and retrieval

39. ContentRevision and AccessPolicyRevision are separate; ACL-only changes preserve content/citation identity.
40. Every source revision declares `native_atomic`, `version_pinned`, `stable_handle`, `best_effort_consistent`, or rejected consistency.
41. Probe/hash/parse use the same stable handle or staged immutable copy; local path/URL/object TOCTOU tests pass.
42. Connector events are idempotent and gap-aware; source state and cursor advancement commit together locally; token expiry/reset triggers reconciliation.
43. AuthorizationEpoch/group/inheritance changes meet the declared revocation SLO even without a document content event.
44. High-assurance search does not rank over forbidden lexical/vector/visual content; standard profiles disclose their weaker leakage boundary.
45. Every query is pinned to one PublicationSet and compatible index/claim generations.
46. Authorization is rechecked on every cursor/stream/asset delivery; epoch change invalidates continuation before protected output.
47. Unauthorized resources do not leak through counts, ranks, errors, diagnostic traces, or unsupported timing assumptions in the declared profile.
48. Marker UI documentation states that already disclosed external-agent context cannot be revoked.

### 23C.6 Agent and product behavior

49. `marker_query`/EvidencePacket completes priority tasks through bounded server-side planning; cursor traversal is a short inspection mechanism.
50. Tool outcomes preserve partial, review-required, abstained, policy-denied, stale, cursor-invalidated, and failed states across supported clients.
51. Natural-language query planning is schema/policy/budget validated and cannot create authorization, identity, joins, or effects.
52. Structural token limits never emit malformed tables, JSON, formulas, or code units.
53. EvidencePacket reuse is invalidated by source, ACL, policy, verification, redaction, citation, or renderer/tokenizer changes.
54. AnswerContextTrace is not represented as proof that the answer is entailed; material answer claims can be assessed separately.
55. Revocation, revision change, and late contradiction during an agent task invalidate Marker UI state/cursors without falsely promising deletion from external context.
56. UI screens, approvals, exports, and operational status expose their as-of revision/policy/completeness and reject stale review commits.

### 23C.7 Economics and claim language

57. Local/industrial scale envelopes report database rows, payload/object count, WAL/write amplification, retained generations, FTS/vector/visual storage, copy bytes, cold starts, review, and reprocessing—not inference alone.
58. Visual retrieval is selective and can be disabled; its downstream gain pays measured storage/update/ACL complexity.
59. Every model/capability and architecture subsystem passes complexity-adjusted utility and has a support owner, rollback, expiry, and kill condition.
60. Every leadership claim names workflow, source/policy/hardware profile, competitors, date, catastrophic budget, review burden, and unresolved limits.
61. A negative result that removes routing, marketplace, visual, or generalized-language complexity is accepted as successful research.
62. The final displacement test asks whether a rational user can achieve a better accepted end-to-end outcome by leaving Marker UI; any remaining reason is integrated, measured, or explicitly conceded.

---
