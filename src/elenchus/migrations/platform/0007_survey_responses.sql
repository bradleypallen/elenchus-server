-- version: 7
-- description: Phase D/8 — post-session questionnaire storage.
--
-- One row per (session, instrument) submission. The four instruments
-- the Sloan study administers after each session are NASA-TLX, SUS,
-- Trust in Automated Systems (Jian et al. 2000), and the custom
-- Epistemic Experience Questionnaire. Item definitions live in
-- `questionnaires.py` (versioned with the code) so the per-study
-- export can reproduce exactly what each participant saw; the
-- `instrument_version` column records which revision was in effect
-- at submission time.
--
-- `responses` is a JSON object mapping item id → numeric response.
-- Validation (every item answered, values in range) happens at the
-- application layer against the instrument definition.

CREATE SEQUENCE IF NOT EXISTS survey_responses_seq START 1;

CREATE TABLE IF NOT EXISTS survey_responses (
    id INTEGER PRIMARY KEY DEFAULT nextval('survey_responses_seq'),
    session_id INTEGER NOT NULL,
    instrument VARCHAR NOT NULL,
    instrument_version VARCHAR NOT NULL DEFAULT '1',
    responses VARCHAR NOT NULL DEFAULT '{}',
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS survey_responses_session_idx
    ON survey_responses (session_id);
CREATE INDEX IF NOT EXISTS survey_responses_instrument_idx
    ON survey_responses (instrument);
