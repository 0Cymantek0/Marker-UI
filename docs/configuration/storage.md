# Storage & Directory Architecture

Marker UI relies on a persistent storage directory on the host machine (or in a Docker volume) to cache neural model weights, persist SQLite records, encrypt settings, and store uploaded documents and converted results.

---

## Directory Structure

By default, the application maps all runtime storage under the `data/` directory:

```text
marker-ui/
└── data/
    ├── uploads/           # Temporary folder for received files
    ├── output/            # Converted files, organized by Job ID
    │   └── {job_id}/
    │       ├── output.md  # Converted Markdown file
    │       └── images/    # Extracted PNG/JPG images
    ├── kernel_payloads/   # Truth Kernel immutable payload store (PR64)
    │   ├── objects/       # Content-addressed final objects (never rewritten)
    │   ├── tmp/           # Staging scratch (never referenced by truth)
    │   └── quarantine/    # Tampered objects displaced by verified re-staging
    ├── marker_ui.db       # SQLite database file
    └── .secret_key        # Auto-generated 32-byte Fernet key
```

---

## Storage Components

### 1. Uploads Folder (`data/uploads/`)
- When a document is uploaded via `POST /api/convert/upload`, the raw file is written here first.
- Once the conversion job starts, the `TaskManager` references this file.
- The file remains here until the job is explicitly deleted by the user (via the UI history dashboard or the `DELETE /api/convert/{job_id}` endpoint), at which point it is unlinked to reclaim space.

### 2. Output Folder (`data/output/`)
- Converted results are saved into a folder named after the job UUID.
- If images are extracted, they are placed in `data/output/{job_id}/images/`.
- The contents of this folder are packaged into a ZIP file when a download is requested if the folder contains extracted image folders.
- Deleting a job from history purges this folder.

### 3. SQLite Database (`data/marker_ui.db`)
- Houses metadata for all jobs and system settings.
- Alembic is the sole persistent schema authority: all supported launch paths
  (`start.sh`, `start.ps1`, the container) run migrations to head before the
  backend starts; application startup validates compatibility and never
  creates or repairs schema. See [Database Schema & Migrations](../development/database.md).

### 4. Fernet Key File (`data/.secret_key`)
- Auto-generated on the first system start.
- If you lose this file, you will be unable to decrypt any previously saved API keys in SQLite, and will need to re-enter them in the UI.

### 5. Truth Kernel Payload Store (`data/kernel_payloads/`)
- Content-addressed immutable blob store backing committed Truth Kernel
  payload references (V3.2 PR64). Objects are named by the sha256 of
  their exact bytes, published via write-to-scratch + fsync + atomic
  rename + read-back verification, and never rewritten; tampered objects
  are quarantined rather than silently replaced. The store is not a
  second truth authority — the SQLite commit remains the only
  linearization point (see [Truth Kernel reference](../reference/truth-kernel.md)).
- Location override: `MARKER_KERNEL_PAYLOAD_ROOT` environment variable
  (default `data/kernel_payloads`).
- Physical retirement (V3.2 PR65B): a garbage-collection pass may unlink
  objects whose hashes no live retention root (current generation,
  declared hold, or reader pin) requires, but only after the database
  recorded a durable tombstone authorizing it. The registry row stays,
  and retired bytes surface as an explicit `retired` availability state —
  history never pretends the bytes were never referenced. Re-supplying
  the exact bytes through normal staging always re-publishes them.
