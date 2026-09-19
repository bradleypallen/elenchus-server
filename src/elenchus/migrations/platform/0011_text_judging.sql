-- version: 11
-- description: Blinded rating of the participants' texts.
--
-- The revised pilot's judged artifact is the text each participant
-- submits (`study_texts`, migration 0009), and the panel gives each
-- text **absolute** ratings — texts on different topics can't sensibly
-- be compared head to head, so the pairwise packaging of migration 0006
-- (one elenchus report + one baseline report in slots A/B) doesn't fit.
-- Those tables stay for the legacy report flow; these are the pilot's.
--
-- `text_assignments` — one row per (text, judge). `position` is a random
--                      number drawn at assignment; a judge's queue is
--                      ordered by it, so each judge meets the texts in
--                      their own random order and no ordering (by
--                      condition, by participant, by submission time)
--                      is shared across the panel.
-- `text_ratings`     — a judge's submission for an assignment. As with
--                      the legacy ratings, a judge may resubmit; every
--                      submission is kept and the newest one counts.
--                      `rubric_version` pins the wording they rated
--                      against; `seconds_spent` is how long the rating
--                      form was open (client-reported).

CREATE SEQUENCE IF NOT EXISTS text_assignments_seq START 1;

CREATE TABLE IF NOT EXISTS text_assignments (
    id INTEGER PRIMARY KEY DEFAULT nextval('text_assignments_seq'),
    study_id VARCHAR NOT NULL,
    text_id INTEGER NOT NULL,
    judge_actor_id INTEGER NOT NULL,
    assigned_by INTEGER NOT NULL,
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    position DOUBLE NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
    completed_at TIMESTAMP,
    UNIQUE(text_id, judge_actor_id)
);

CREATE INDEX IF NOT EXISTS text_assignments_judge_idx ON text_assignments (judge_actor_id);

CREATE SEQUENCE IF NOT EXISTS text_ratings_seq START 1;

CREATE TABLE IF NOT EXISTS text_ratings (
    id INTEGER PRIMARY KEY DEFAULT nextval('text_ratings_seq'),
    assignment_id INTEGER NOT NULL,
    rubric_version VARCHAR NOT NULL,
    -- JSON object: dimension key → integer score.
    ratings VARCHAR NOT NULL,
    justification VARCHAR DEFAULT '',
    -- The blinding check: which way of working the judge thinks produced
    -- the text. If the panel guesses at chance, the blind held.
    condition_guess VARCHAR CHECK(condition_guess IN ('elenchus', 'baseline', 'unsure')),
    confidence INTEGER,
    seconds_spent INTEGER,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS text_ratings_assignment_idx ON text_ratings (assignment_id);
