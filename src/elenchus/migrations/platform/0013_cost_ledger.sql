-- version: 13
-- description: A ledger for what the platform costs to run besides LLM
-- tokens — hosting, domain, email — entered by an admin from invoices.
--
-- LLM spend is *measured* (the `usage` table, priced at read time —
-- see `costs.py`). Infrastructure spend can't be: it arrives as an
-- invoice from whoever hosts the box this year. So it is recorded, not
-- fetched — a provider-agnostic ledger survives a move between hosts or
-- accounts, and every row can carry the invoice reference a grant
-- report needs. `cost_ledger.py` owns both tables.
--
-- `cost_entries`   — one row per charge (or credit: a negative amount).
--   `amount` + `currency` are what the invoice says; `amount_usd` is
--   what is charged to the budget, entered by the admin when the
--   invoice isn't in dollars (the exchange rate that counts is the
--   finance office's, not one the platform could look up).
--   `category = 'llm_provider'` is different in kind: it is the LLM
--   provider's **own figure** for a month of usage (`covers_month`),
--   kept for reconciliation against the platform's computed spend and
--   never added to a total — that money is already counted from tokens.
--   `estimated` marks a row generated from a recurring item and not yet
--   checked against an invoice.
--   A row is never deleted: it is voided (`voided_at`, `void_reason`)
--   and stays in the ledger, excluded from every sum.
--
-- `cost_recurring` — a charge expected every month or year (the box,
--   the DNS zone, the domain renewal). Drives the run-rate, the
--   projection to the end of the budget period, the "nothing recorded
--   for last month" reminder, and one-click entry of a month's charges.
--   It is a forecast and a convenience, never itself counted as spend.

CREATE SEQUENCE IF NOT EXISTS cost_recurring_seq START 1;

CREATE TABLE IF NOT EXISTS cost_recurring (
    id INTEGER PRIMARY KEY DEFAULT nextval('cost_recurring_seq'),
    category VARCHAR NOT NULL CHECK(category IN ('hosting', 'domain', 'email', 'other')),
    vendor VARCHAR NOT NULL,
    description VARCHAR NOT NULL DEFAULT '',
    amount_usd DOUBLE NOT NULL CHECK(amount_usd > 0),
    cadence VARCHAR NOT NULL CHECK(cadence IN ('monthly', 'yearly')),
    starts_on DATE NOT NULL,
    ends_on DATE,
    created_by INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS cost_entries_seq START 1;

-- No secondary indexes: the table is tiny, and DuckDB rewrites a row
-- whose indexed column is updated, which is a needless way to meet its
-- over-eager constraint checking on an editable table.
CREATE TABLE IF NOT EXISTS cost_entries (
    id INTEGER PRIMARY KEY DEFAULT nextval('cost_entries_seq'),
    incurred_on DATE NOT NULL,
    category VARCHAR NOT NULL
        CHECK(category IN ('hosting', 'domain', 'email', 'other', 'llm_provider')),
    vendor VARCHAR NOT NULL,
    description VARCHAR NOT NULL DEFAULT '',
    amount DOUBLE NOT NULL,
    currency VARCHAR NOT NULL DEFAULT 'USD',
    amount_usd DOUBLE NOT NULL,
    invoice_ref VARCHAR NOT NULL DEFAULT '',
    covers_month VARCHAR NOT NULL DEFAULT '',
    estimated BOOLEAN NOT NULL DEFAULT FALSE,
    recurring_id INTEGER,
    created_by INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_by INTEGER,
    updated_at TIMESTAMP,
    voided_by INTEGER,
    voided_at TIMESTAMP,
    void_reason VARCHAR NOT NULL DEFAULT ''
);
