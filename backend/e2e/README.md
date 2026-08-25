# Backend E2E Launcher

Boots the real Marker UI backend for Playwright suites: real FastAPI app,
real routes, real SQLite database (Alembic-migrated), real auth middleware,
real as-of enforcement — served over actual HTTP by Uvicorn. Exactly one
seam is stubbed: the conversion render path.

## Run

```bash
# from repo root
python backend/e2e/launch.py
# or from backend/
python e2e/launch.py
```

The launcher prints a single ready line once the server is about to bind
(wait for it before driving the API):

```
MARKER_E2E_READY host=127.0.0.1 port=8917 job_id=job-e2e-seeded db_dir=<scratch dir>
```

## Environment variables

| Variable            | Default                              | Meaning                                        |
| ------------------- | ------------------------------------ | ---------------------------------------------- |
| `MARKER_E2E_PORT`   | `8917`                               | TCP port to serve on.                          |
| `MARKER_E2E_DB_DIR` | fresh `tempfile.mkdtemp("marker-e2e-")` | Scratch dir for DB + uploads. Reuse is idempotent. |

## What is real vs stubbed

**Real (unchanged app code):**

- All routes (`/api/health`, `/api/convert/status|download|regenerate|history`, ...).
- The as-of contract end to end: token derivation, verified/historical
  download modes, stale-state 409s, regenerate preconditions.
- Auth middleware (`RestAuthMiddleware`); with no token env vars configured
  it is open by design (closed-by-configuration), so plain requests pass.
- Database: temp SQLite file migrated to the Alembic head via
  `app.db_migration.upgrade_database` — the app's own runtime gate then
  verifies it at startup.
- Kernel runtime, task manager, startup reconciliation.

**Stubbed (the only seam):**

- `_app_state.conversion_service.convert_file_formats` and
  `.supports_multiple_formats` are replaced with deterministic in-process
  functions. This is the exact seam the backend test suite patches
  (`tests/conftest.py` FakeMarkerService swap;
  `tests/test_as_of_contract.py::_stub_render`). The rendered text embeds a
  monotonically increasing render counter (seeded past every number already
  cached in the DB), so every regenerate produces new content and rotates the
  derived state token — the staleness the E2E suite needs to observe is always
  reachable, including the first regenerate on a reused scratch dir.

**Seed data:** one completed job `job-e2e-seeded` (markdown format cached,
source PDF present in the scratch uploads dir so regenerate works), shaped
identically to the canonical builder in `tests/test_as_of_contract.py`.

## How Playwright uses it

1. Spawn `python backend/e2e/launch.py` (optionally with
   `MARKER_E2E_PORT`/`MARKER_E2E_DB_DIR`), capture stdout, wait for the
   `MARKER_E2E_READY` line.
2. Drive `http://127.0.0.1:<port>` over real HTTP — observe a token via
   `GET /api/convert/status/job-e2e-seeded`, download with
   `?as_of=<token>`, regenerate via
   `POST /api/convert/job-e2e-seeded/regenerate?format=markdown&as_of=<token>`,
   and assert the old token now yields a typed `409 stale_state`.
3. Tear the process down (kill) when the suite finishes. The scratch
   directory holds the DB/uploads; delete it unless `MARKER_E2E_DB_DIR`
   pinned it for reuse.
