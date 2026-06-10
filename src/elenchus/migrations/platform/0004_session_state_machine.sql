-- version: 4
-- description: Phase D/2 — extend `sessions` with the participant
-- lifecycle state machine.
--
-- The Sloan participant flow is:
--   briefing → tutorial → active → post_session → surveyed → complete
-- with terminal alternatives `expired` (timed out) and `interrupted`
-- (researcher voided or platform crash). The participant never has to
-- figure out "what comes next" — the platform routes them through
-- each state based on the row in `sessions`.
--
-- `base_id` becomes NULLABLE because the dialectic base doesn't exist
-- yet during briefing / tutorial — it's created when the participant
-- transitions into `active`. `state` is the new canonical lifecycle
-- column; the existing `status` column ('open' / 'closed') stays for
-- back-compat with the few callers (tests, future per-actor session
-- list) that already read it.
--
-- `study_token` links back to participant_session_tokens.token so the
-- platform can reconcile token state when a session ends. `condition`
-- is denormalized from the token row for cheap cohort queries.

-- Existing rows (from Phase A — there are none in production but
-- some tests seed them) get a default state of 'active' since they
-- predate the lifecycle concept.

ALTER TABLE sessions ALTER COLUMN base_id DROP NOT NULL;
ALTER TABLE sessions ADD COLUMN state VARCHAR DEFAULT 'active';
ALTER TABLE sessions ADD COLUMN state_changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE sessions ADD COLUMN study_token VARCHAR;
ALTER TABLE sessions ADD COLUMN condition VARCHAR;

-- Index for "find this actor's open participant session" — the most
-- common read pattern. Filters on state IN (...active states...).
CREATE INDEX IF NOT EXISTS sessions_actor_state_idx
    ON sessions (actor_id, state);
CREATE INDEX IF NOT EXISTS sessions_study_token_idx
    ON sessions (study_token);
