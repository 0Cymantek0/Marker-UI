# Database Schema & Migrations

Marker UI uses **SQLite** as its storage engine, managed through **SQLAlchemy** (using asynchronous `aiosqlite`). Runtime startup creates missing tables and self-heals additive column gaps. Alembic migration files are present for developer-managed schema work, but the application does not currently run `alembic upgrade head` automatically on startup.

---

## Schema Models

The database contains two main tables:

### 1. `ConversionJob` Model (mapped to `conversion_jobs` table)
Tracks document conversions.
- `id`: String(36) UUID (Primary Key).
- `filename`: String(512).
- `original_name`: String(512).
- `status`: String(20) (`pending`, `processing`, `completed`, `failed`).
- `input_format` / `output_format`: String(20).
- `config_json`: Text (JSON string of input parameters).
- `result_text`: Text (final Markdown result).
- `result_metadata_json`: Text (JSON metadata, including image-understanding info).
- `result_path`: String(1024) (output directory path).
- `error_message`: Text (nullable).
- `progress`: Integer (0 to 100).
- `created_at` / `updated_at` / `completed_at`: DateTime.

### 2. `Setting` Model (mapped to `settings` table)
Stores key-value configurations.
- `id`: Integer (Primary Key, autoincrements).
- `key`: String(255) (unique, indexed).
- `value`: Text.
- `category`: String(100) (`general`, `llm`, `gpu`, etc.).
- `created_at` / `updated_at`: DateTime.

---

## Migrations

All schema changes must be versioned. If you add fields to database models in `backend/app/models/job.py` or `backend/app/models/settings.py`:

1. Generate the migration file:
   ```bash
   cd backend
   alembic revision --autogenerate -m "Describe your changes"
   ```
2. Apply the migration locally:
   ```bash
   alembic upgrade head
   ```
3. Apply migrations manually for any environment that depends on Alembic version history:
   ```bash
   alembic upgrade head
   ```
4. For current local runtime startup, `create_tables()` also creates missing tables and applies additive missing-column repairs so older SQLite databases keep working. Non-additive changes still require a real migration.
