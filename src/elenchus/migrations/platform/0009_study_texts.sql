-- version: 9
-- description: The participant's written text becomes the study's
-- judged artifact. In the revised pilot design each participant is
-- given a topic and writes a short prose introduction to it, in their
-- own words, while working with the LLM; expert raters assess that
-- text. (Previously the rated artifact was an LLM-generated structured
-- report — see 0005_study_reports.sql — which stays available as an
-- offline tool but is no longer what judges see.)
--
-- Two additions:
--
-- 1. A topic on each participant token. `topic_title` names the task
--    base (so the Elenchus opponent sees it as the dialectic's topic)
--    and heads the writing pane; `topic_brief` is the longer framing
--    shown to the participant. Both default to '' so existing tokens
--    and the old issuance path keep working.
--
-- 2. `study_texts` — the text as submitted when the participant ends
--    the task. One row per session (UNIQUE), written once. The draft
--    history that led to it lives in the per-base `text_snapshots`
--    table (migration base/0004); this row is the platform-level handle
--    the judging tables will reference.

ALTER TABLE participant_session_tokens ADD COLUMN topic_title VARCHAR DEFAULT '';
ALTER TABLE participant_session_tokens ADD COLUMN topic_brief VARCHAR DEFAULT '';

CREATE SEQUENCE IF NOT EXISTS study_texts_seq START 1;

CREATE TABLE IF NOT EXISTS study_texts (
    id INTEGER PRIMARY KEY DEFAULT nextval('study_texts_seq'),
    session_id INTEGER NOT NULL UNIQUE,
    actor_id INTEGER NOT NULL,
    condition VARCHAR NOT NULL CHECK(condition IN ('elenchus', 'baseline')),
    topic_title VARCHAR DEFAULT '',
    content VARCHAR NOT NULL,
    word_count INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    -- Seconds between entering `active` and submitting, by the server
    -- clock: time on task, independent of the soft 60-minute guidance.
    active_elapsed_seconds INTEGER,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
