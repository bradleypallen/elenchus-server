-- version: 14
-- description: The LLM provider's own record of usage and cost, imported
-- for reconciliation against what the platform measured.
--
-- The platform counts tokens as calls are made and prices them from its
-- own table (`costs.py`). The provider keeps its own books. Comparing
-- the two — per month, per model, tokens as well as dollars — is how a
-- stale rate, usage from outside the platform on the same key, or calls
-- that escaped recording get noticed. `provider_report.py` owns these
-- tables.
--
-- The provider's figures are fetched **off the box** (`elenchus-
-- provider-report`, with an Admin API key that never touches the
-- server) into a JSON file, and an admin uploads the file. So:
--
-- `provider_usage_daily` — one row per provider × UTC day × model (and,
--   for costs that aren't tokens — web search, code execution — a row
--   with an empty model and that `cost_type`). Derived data, not a
--   ledger: importing a report **replaces** every row for the days it
--   covers, so re-importing a fresher report for the same month is the
--   normal way to update it. No primary key or indexes — the table is
--   small and is only ever rewritten by date range.
--
-- `provider_report_imports` — one row per upload: who, when, what
--   period and scope, how much. The audit trail of the replacements.

CREATE TABLE IF NOT EXISTS provider_usage_daily (
    provider VARCHAR NOT NULL,
    day DATE NOT NULL,
    model VARCHAR NOT NULL DEFAULT '',
    cost_type VARCHAR NOT NULL DEFAULT 'tokens',
    uncached_input_tokens BIGINT NOT NULL DEFAULT 0,
    cache_read_input_tokens BIGINT NOT NULL DEFAULT 0,
    cache_creation_input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE NOT NULL DEFAULT 0,
    import_id INTEGER NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS provider_report_imports_seq START 1;

CREATE TABLE IF NOT EXISTS provider_report_imports (
    id INTEGER PRIMARY KEY DEFAULT nextval('provider_report_imports_seq'),
    provider VARCHAR NOT NULL,
    period_from DATE NOT NULL,
    period_to DATE NOT NULL,
    fetched_at VARCHAR NOT NULL DEFAULT '',
    scope VARCHAR NOT NULL DEFAULT '{}',
    rows_imported INTEGER NOT NULL DEFAULT 0,
    total_cost_usd DOUBLE NOT NULL DEFAULT 0,
    imported_by INTEGER NOT NULL,
    imported_at TIMESTAMP NOT NULL
);
