-- version: 2
-- description: Phase C cost-tracking table. One row per LLM call,
-- success or failure. Lets the admin dashboard surface daily/monthly
-- cost rollups, per-actor breakdowns, and budget-cap enforcement.
--
-- `actor_id` and `base_id` are NULLABLE because some LLM calls don't
-- have a current actor (system summaries, batch jobs) or aren't
-- attached to any base (account-creation magic-link flows that never
-- touch an LLM today, but might in the future). `category` carries
-- the ChatCategory value from llm_client.py — 'success' for billable
-- calls, the failure categories for forensic context.

CREATE SEQUENCE IF NOT EXISTS usage_seq START 1;

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY DEFAULT nextval('usage_seq'),
    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor_id INTEGER,
    base_id VARCHAR,
    model VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd DOUBLE NOT NULL DEFAULT 0.0,
    attempts INTEGER NOT NULL DEFAULT 1,
    latency_ms INTEGER NOT NULL DEFAULT 0
);

-- Indices for the two main read patterns: time-bucket rollups
-- ("cost this month") and per-actor breakdowns.
CREATE INDEX IF NOT EXISTS usage_occurred_at_idx ON usage (occurred_at);
CREATE INDEX IF NOT EXISTS usage_actor_idx ON usage (actor_id);
