-- version: 20
-- description: Judging as the study's registration describes it (§2.5,
-- §2.6): each judge sees a participant's two finished texts as a PAIR,
-- labelled "Text A" and "Text B" with the labels drawn per (participant,
-- judge); rates each text on the four dimensions with a one-sentence
-- justification per dimension; ranks the pair; and, once every pair in
-- their queue is done, guesses each text's condition with a confidence.
-- The ranking is the fallback primary outcome if the reliability gate
-- fails, which is why it must exist. Migration 0011's per-text
-- `text_assignments` / `text_ratings` are kept as the store for the
-- per-text ratings — each gets a `pair_id` — so nothing is copied.
--
-- `text_pair_assignments` — one row per (participant, judge). `position`
--     orders that judge's queue at random; `label_a_text_id` /
--     `label_b_text_id` fix which text is "A" for this judge. `status`
--     is 'completed' once both texts are rated and the pair is ranked.
-- `text_ratings.justifications` — JSON, dimension key → one sentence.
-- `text_pair_rankings` — every ranking submitted for a pair; the newest
--     counts.
-- `text_condition_guesses` — every guess submitted; the newest per
--     (judge, text) counts. Collected after all assessments, not on the
--     rating form.

CREATE SEQUENCE IF NOT EXISTS text_pair_assignments_seq START 1;

CREATE TABLE IF NOT EXISTS text_pair_assignments (
    id INTEGER PRIMARY KEY DEFAULT nextval('text_pair_assignments_seq'),
    study_id VARCHAR NOT NULL,
    participant_id INTEGER NOT NULL,
    judge_actor_id INTEGER NOT NULL,
    label_a_text_id INTEGER NOT NULL,
    label_b_text_id INTEGER NOT NULL,
    assigned_by INTEGER NOT NULL,
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    position DOUBLE NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
    completed_at TIMESTAMP,
    UNIQUE(participant_id, judge_actor_id)
);

CREATE INDEX IF NOT EXISTS text_pair_assignments_judge_idx ON text_pair_assignments (judge_actor_id);

ALTER TABLE text_assignments ADD COLUMN pair_id INTEGER;
ALTER TABLE text_ratings ADD COLUMN justifications VARCHAR DEFAULT '{}';

CREATE SEQUENCE IF NOT EXISTS text_pair_rankings_seq START 1;

CREATE TABLE IF NOT EXISTS text_pair_rankings (
    id INTEGER PRIMARY KEY DEFAULT nextval('text_pair_rankings_seq'),
    pair_id INTEGER NOT NULL,
    preferred_text_id INTEGER NOT NULL,
    rubric_version VARCHAR NOT NULL,
    seconds_spent INTEGER,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS text_condition_guesses_seq START 1;

CREATE TABLE IF NOT EXISTS text_condition_guesses (
    id INTEGER PRIMARY KEY DEFAULT nextval('text_condition_guesses_seq'),
    judge_actor_id INTEGER NOT NULL,
    text_id INTEGER NOT NULL,
    guess VARCHAR NOT NULL CHECK(guess IN ('elenchus', 'baseline', 'unsure')),
    confidence INTEGER,
    rubric_version VARCHAR NOT NULL,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
