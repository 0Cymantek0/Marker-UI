# AGENTS.md

These are repository-wide quality defaults for coding agents. Apply them unless the task, specification, or an existing authoritative project contract says otherwise. Preserve established behavior and architecture unless the requested change requires altering them.

## Working approach

- Read the nearby implementation and tests before editing. Identify the existing owner of the behavior you are changing.
- Make the smallest coherent change that solves the requested problem. Keep unrelated refactors, renames, formatting churn, and cleanup out of the patch.
- Reuse existing domain types, services, helpers, and authority boundaries before introducing new ones.
- Keep one source of truth for business rules, statuses, schemas, validation, and state transitions. Translate at boundaries instead of duplicating authority.
- Add an abstraction when it clarifies ownership, enforces an invariant, or removes real duplication. Prefer direct code over speculative generalization.
- Keep adapters thin. They may validate or translate transport concerns, but should not silently reimplement authoritative domain behavior.

## Correctness and contracts

- Preserve observable semantics unless the task explicitly changes them. Avoid silent coercions, fallback behavior, or compatibility aliases that alter meaning.
- Put important invariants near the boundary that owns them and make invalid states explicit where practical.
- Preserve authorization, provenance, revision, snapshot, and persistence boundaries already established by the codebase.
- Handle expected failures narrowly. When translating an exception at a boundary, preserve useful context and the original cause.
- Prefer fail-closed behavior for authorization, integrity, migration, and persistence checks.
- Keep identity-affecting serialization deterministic. Use explicit encodings for persisted or fixture text when platform defaults could change behavior.

## Tests and verification

- A behavior change should have executable evidence. Add or adjust a focused regression test when a meaningful defect or new behavior is introduced.
- Test observable outcomes and invariants rather than the exact internal implementation shape.
- Use the lowest practical public seam for tests, and include integration coverage when correctness depends on persistence, concurrency, process lifecycle, or transport behavior.
- Treat flaky tests as a diagnosis problem. Prefer fixing lifecycle, synchronization, or isolation over increasing sleeps/timeouts, skipping tests, or weakening assertions.
- Tests must leave owned resources clean: tasks, threads, executors, sessions, engines, subprocesses, temporary files/databases, monkeypatches, and mutable global state.
- Run focused checks while iterating, then the relevant broader suite before declaring the work complete. If a broader failure appears, determine whether it is caused by the change or demonstrably pre-existing before concluding.

## Async and resource ownership

- Every acquired resource needs a clear owner and a matching cleanup path, including exceptional and cancellation paths.
- Prefer scope-owned dependencies and explicit lifecycle management over mutable process-global state when both are practical.
- When existing global state must be changed in a test, restore it reliably even if the test fails.
- Prefer observable synchronization primitives or state transitions over timing assumptions and arbitrary sleeps.
- Background work must have an owner, a completion/failure observation path, and a shutdown path.

## Maintainability

- Keep functions and modules focused on one coherent responsibility. When adding substantial logic to an already-large module, first look for an existing natural seam; extract only when ownership becomes clearer rather than for size alone.
- Use precise types at public and domain boundaries. Avoid widening a useful type to `Any` merely to make integration easier.
- Comments should explain intent, invariants, tradeoffs, or surprising constraints; let straightforward code explain mechanics.
- Prefer names from the existing domain vocabulary. Do not invent near-synonyms for concepts that already have an established name.
- Do not replace working code solely for stylistic preference. Refactoring should make the requested change safer, simpler, or easier to verify.

## Completion standard

Before calling a change complete, verify that the requested behavior is implemented, relevant invariants still hold, owned resources are cleaned up, targeted tests pass, and no known regression introduced by the patch remains unexplained.
