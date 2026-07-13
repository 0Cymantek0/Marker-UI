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
- Initialized automatically at startup with SQLAlchemy metadata checks.
- Existing local databases self-heal additive column changes at startup. Alembic
  migrations are available for developer-managed schema changes, but the runtime
  does not currently run `alembic upgrade head` automatically.

### 4. Fernet Key File (`data/.secret_key`)
- Auto-generated on the first system start.
- If you lose this file, you will be unable to decrypt any previously saved API keys in SQLite, and will need to re-enter them in the UI.
