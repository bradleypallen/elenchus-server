-- version: 5
-- description: Phase D/5 — storage for the structured LLM-generated
-- reports that go to blinded judges.
--
-- Each row is one report produced from one session. `condition` is
-- denormalized from the session for cheap cohort queries and as a
-- safety check at retrieval time (the blinded-judge interface must
-- never accidentally surface this column to a judge).
--
-- `content` holds the raw report text. The same template is used for
-- both conditions, so a judge reading two reports can't tell from
-- structure alone which condition produced which — that's the whole
-- point of format-balancing.
--
-- Generator metadata (model, token counts, cost) lets the per-study
-- export reconstruct exactly how each report was produced — important
-- for replication and for accounting against the Phase C usage table.

CREATE SEQUENCE IF NOT EXISTS study_reports_seq START 1;

CREATE TABLE IF NOT EXISTS study_reports (
    id INTEGER PRIMARY KEY DEFAULT nextval('study_reports_seq'),
    session_id INTEGER NOT NULL,
    condition VARCHAR NOT NULL CHECK(condition IN ('elenchus', 'baseline')),
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    content VARCHAR NOT NULL,
    generator_model VARCHAR NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd DOUBLE NOT NULL DEFAULT 0.0,
    -- Free-form metadata for downstream tooling (e.g. judge package id,
    -- regeneration reason). JSON serialized at the application layer.
    metadata VARCHAR DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS study_reports_session_idx ON study_reports (session_id);
CREATE INDEX IF NOT EXISTS study_reports_condition_idx ON study_reports (condition);
