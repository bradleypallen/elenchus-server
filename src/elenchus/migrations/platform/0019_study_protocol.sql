-- version: 19
-- description: Three things the study's registration asks the platform
-- to keep that it didn't (design note G1–G3):
--
--   * `study_configs.max_gap_days` — the longest a participant's two
--     sessions may be apart (the registration's "[Set: 21] days"; 0 =
--     no maximum). Not a gate: a pair outside the window is flagged in
--     the roster and the export for the named sensitivity analysis.
--   * screening covariates on `study_participants` — hands-on
--     ontology-engineering experience, prior LLM-tool use, and whether
--     the person nominated a topic — recorded at enrolment and exported
--     beside the participant code. Coded levels, '' = not recorded.
--   * `session_deviations` — protocol deviations logged at the time
--     (technical failure, interruption, session ended early, ended by
--     the clock, other), by a researcher or by the platform itself
--     (`logged_by` NULL), before any text is judged. Never deleted.

ALTER TABLE study_configs ADD COLUMN max_gap_days INTEGER DEFAULT 21;

ALTER TABLE study_participants ADD COLUMN ontology_experience VARCHAR DEFAULT '';
ALTER TABLE study_participants ADD COLUMN prior_llm_use VARCHAR DEFAULT '';
ALTER TABLE study_participants ADD COLUMN nominated_topic BOOLEAN DEFAULT FALSE;

CREATE SEQUENCE IF NOT EXISTS session_deviations_seq START 1;

CREATE TABLE IF NOT EXISTS session_deviations (
    id INTEGER PRIMARY KEY DEFAULT nextval('session_deviations_seq'),
    session_id INTEGER NOT NULL,
    kind VARCHAR NOT NULL,
    note VARCHAR NOT NULL DEFAULT '',
    logged_by INTEGER,
    logged_at TIMESTAMP NOT NULL
);
