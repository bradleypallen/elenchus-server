-- version: 3
-- description: Sloan-study participant session tokens. Each row is one
-- scheduled participant session: a researcher pre-issues a token bound
-- to a freshly-created actor of kind='participant', the participant
-- clicks the emailed link, the platform validates the token and
-- issues a session cookie tied to that actor. No password, no
-- account, no login screen — the token IS the credential, single-use,
-- bounded by a scheduled time window.
--
-- The `condition` column drives the within-subjects experimental
-- design (the same participant has separate tokens for the elenchus
-- and baseline conditions). `status` is the session lifecycle
-- ('scheduled' → 'active' → 'complete', with 'expired' / 'voided'
-- terminal states for tokens that never get used).
--
-- `actor_id` references actors.id but DuckDB doesn't enforce the FK
-- across writes; the application layer handles the join.

CREATE TABLE IF NOT EXISTS participant_session_tokens (
    token VARCHAR PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    study_id VARCHAR NOT NULL,
    condition VARCHAR NOT NULL CHECK(condition IN ('elenchus', 'baseline')),
    scheduled_start TIMESTAMP,
    scheduled_end TIMESTAMP,
    issued_by INTEGER NOT NULL,
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used_at TIMESTAMP,
    session_id INTEGER,
    status VARCHAR NOT NULL DEFAULT 'scheduled'
        CHECK(status IN ('scheduled', 'active', 'complete', 'expired', 'voided')),
    notes VARCHAR DEFAULT ''
);

-- Researchers query by study_id + condition for cohort views.
CREATE INDEX IF NOT EXISTS participant_tokens_study_idx
    ON participant_session_tokens (study_id, condition);

-- Per-actor lookups for "show me what this participant has scheduled".
CREATE INDEX IF NOT EXISTS participant_tokens_actor_idx
    ON participant_session_tokens (actor_id);
