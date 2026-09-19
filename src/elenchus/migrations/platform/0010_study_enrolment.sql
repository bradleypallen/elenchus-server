-- version: 10
-- description: Participant enrolment for the within-subjects crossover.
--
-- Each participant does two sessions, one per condition, on a different
-- topic each time, days apart. Until now a researcher issued the two
-- tokens by hand, and because every token creates its own passwordless
-- actor, nothing in the data said the two sessions belonged to one
-- person — and nothing balanced condition order against topic.
--
-- `study_configs`      — per-study setup: the two topics (A and B) and
--                        the minimum gap between a participant's
--                        sessions.
-- `study_participants` — one row per enrolled person: a stable
--                        `participant_code` (the key that links their two
--                        sessions in the export), and their allocation —
--                        which condition and which topic come first.
--                        The 2×2 of (first_condition × first_topic) is
--                        the counterbalancing cell.
-- tokens               — gain `participant_id` and `period` (1 or 2).
--
-- A token still owns its own actor: the actor is the *session's*
-- identity (it owns that session's bases), the participant row is the
-- *person's*. Keeping them apart means an abandoned first session can
-- never be confused with the second at login time.

CREATE TABLE IF NOT EXISTS study_configs (
    study_id VARCHAR PRIMARY KEY,
    topic_a_title VARCHAR NOT NULL,
    topic_a_brief VARCHAR DEFAULT '',
    topic_b_title VARCHAR NOT NULL,
    topic_b_brief VARCHAR DEFAULT '',
    -- A participant's second link stays closed until this long after
    -- their first session ended. 0 disables the wait.
    min_gap_hours INTEGER NOT NULL DEFAULT 48,
    created_by INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS study_participants_seq START 1;

CREATE TABLE IF NOT EXISTS study_participants (
    id INTEGER PRIMARY KEY DEFAULT nextval('study_participants_seq'),
    study_id VARCHAR NOT NULL,
    participant_code VARCHAR NOT NULL,
    -- The researcher's label for the person. Identifying: never exported.
    display_name VARCHAR NOT NULL,
    first_condition VARCHAR NOT NULL CHECK(first_condition IN ('elenchus', 'baseline')),
    first_topic VARCHAR NOT NULL CHECK(first_topic IN ('A', 'B')),
    -- 'block' — drawn by permuted-block randomization;
    -- 'manual' — the researcher chose the cell (e.g. a replacement).
    allocation VARCHAR NOT NULL DEFAULT 'block' CHECK(allocation IN ('block', 'manual')),
    enrolled_by INTEGER NOT NULL,
    enrolled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes VARCHAR DEFAULT '',
    UNIQUE(study_id, participant_code)
);

ALTER TABLE participant_session_tokens ADD COLUMN participant_id INTEGER;
ALTER TABLE participant_session_tokens ADD COLUMN period INTEGER;
