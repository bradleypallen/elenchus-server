# Migrations

Elenchus stores schema versions in `meta.schema_version` in every
DuckDB file it owns. The runner in `runner.py` brings a connection up
to the latest version by applying any migration whose version number
exceeds the current one. The runner is forward-only — reversal happens
through `scripts/backup.py` restore, not down-migrations.

## Layout

```
migrations/
├── runner.py
├── platform/        # one set per platform.duckdb
│   └── NNNN_*.sql
└── base/            # one set per per-base file under bases/{actor_id}/
    ├── 0001_initial.sql
    └── 0002_phase_a.sql
```

Each `.sql` file starts with:

```sql
-- version: N
-- description: one-line summary.
```

The runner reads `N` from the header, sorts by `N`, and applies any
file with `N > meta.schema_version` in its own transaction. A duplicate
version number is a programming error and aborts the run.

## When migrations are applied

- **Platform (`platform.duckdb`).** At FastAPI lifespan startup, before
  any request is accepted. Also invoked explicitly by the test fixtures
  and by `elenchus admin create` and `elenchus migrate-legacy`.
- **Per-base files.** Every call to `MaterialBase.open(path)` or
  `MaterialBase.create(path, name)` applies migrations to the
  connection before returning. The `elenchus audit` CLI also reopens
  every base, which incidentally upgrades them.

## Adding a new migration

1. Pick the next free version number (look at the highest existing
   file in the target subdirectory and add 1).
2. Create `migrations/{platform|base}/NNNN_short_name.sql`.
3. Start with the header:

   ```sql
   -- version: NNNN
   -- description: what this migration does and why.
   ```

4. Write the SQL. For backwards-compatible column additions use
   `ALTER TABLE name ADD COLUMN col TYPE DEFAULT value` — the DEFAULT
   backfills existing rows so the migration is safe on a populated
   file.
5. **If you add columns to a table that any positional
   `INSERT INTO table VALUES (...)` writes to, switch that INSERT to
   the column-explicit form** (`INSERT INTO table (a, b, c) VALUES (...)`).
   Otherwise the now-mismatched value count breaks routes that worked
   yesterday. See the W3 D1 commit (`9f6246f`) for the precedent.
6. Add a focused test in `tests/test_migrations.py` that verifies the
   schema shape produced by the new version. Existing tests already
   lock in the legacy-file forward-migration path; you don't need to
   re-prove that.
7. Run the full suite (`pytest -q`) plus `ruff check .` — the smoke
   path for a migration is that every existing test continues to pass
   against the newly migrated schema.

## Failure modes

- **Missing header**: `list_migrations` raises `ValueError`. Add the
  `-- version: N` line.
- **Duplicate version**: ditto. Renumber.
- **Migration body fails**: the runner rolls back the transaction; the
  database stays at the previous version. Fix the SQL and re-run.
- **Schema drift between dev and prod**: `elenchus audit` reports
  bases whose schema couldn't be opened, which is the closest we get
  to a "your local file is behind production" check. Restore from
  backup if needed.

## Why not Alembic / Liquibase?

Because the runtime is two database files with different schemas
managed by the same process, and the migration set is tiny (a few
files per kind). The bespoke runner is ~100 LOC, has zero
dependencies, and surfaces the version directly in `meta` so every
admin can read it from the DuckDB CLI. The cost of adopting Alembic
isn't justified at this scale; if Phase B+ pushes us into Postgres
we'll re-evaluate.
