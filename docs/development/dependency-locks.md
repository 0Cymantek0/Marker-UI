# Backend Dependency Lock Contract

Phase 0 dependency truth: what each lock artifact represents, how to
regenerate and verify it, which platforms it is valid for, and how
Docker/CI/launchers select the correct artifact.

## Artifacts

| Artifact | Resolution semantics | Valid platforms |
|---|---|---|
| `backend/requirements-cpu.lock` | Universal (`uv pip compile --universal --python-version 3.11`), environment markers preserved | Linux (CI, Docker, dev), Windows (dev), macOS (dev) |
| `backend/requirements-gpu.lock` | Explicit target (`--python-platform x86_64-unknown-linux-gnu --python-version 3.11`) | Linux x86_64 only (GPU Docker image) |

The CPU lock is **marker-preserving**: platform-only transitive dependencies
are gated, never unconditional. For example `pywin32==312 ; sys_platform == 'win32'`
(pulled in via `mcp`) installs on Windows and is skipped on Linux. An
unconditional platform-only pin is a defect: it made the previous lock
uninstallable on Linux (CI run `31879237585`, Docker smoke run `31879237578`).

The GPU lock targets Linux x86_64 because CUDA wheels (`torch==2.7.0+cu126`)
exist only for that platform; it is consumed exclusively by the GPU Docker
image (`docker-compose.gpu.yml`, `VARIANT=gpu`).

## Regenerate

```bash
python backend/scripts/lock_dependencies.py            # regenerate both locks
python backend/scripts/lock_dependencies.py --variant cpu
```

Compilation always resolves into a fresh temporary output with `--refresh`,
so neither a pre-existing lockfile (uv prefers previously pinned versions)
nor a stale local index cache can influence the result. Generation and
checking therefore share an identical resolution basis, on any host OS.

## Verify (drift gate, fail-closed)

```bash
python backend/scripts/lock_dependencies.py --check
```

Recompiles from the requirement inputs and compares **marker-aware pins**
(`name`, `version`, `environment marker`). Removing or widening a marker is
drift. Changing any declared dependency without committing the regenerated
lock fails CI before tests run. Publish workflows call this gate via
`.github/workflows/ci.yml` (`workflow_call`), so a drifted revision cannot
produce a published image.

## Installation (no silent fallback)

- **CI**: `pip install -r backend/requirements-cpu.lock` (pytorch CPU index first).
- **Docker (VARIANT=cpu|gpu)**: installs the matching lock; no ad-hoc resolve.
- **Launchers (`start.sh` / `start.ps1`)**: install the CPU lock. If the
  install fails, the launcher exits with regeneration instructions — it never
  falls back to an unconstrained `requirements.txt` resolve.

## Runtime verification

```bash
cd backend && python -m app.cli provenance --verify
```

Compares installed distributions against the active lock, evaluating each
pin's environment marker against the current runtime (a `win32` pin is not
"missing" on Linux). Exits non-zero on any mismatch or missing applicable pin.
