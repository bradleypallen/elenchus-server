-- version: 6
-- description: Phase D/7 — blinded judge interface storage.
--
-- Three tables form the judging pipeline:
--
--   judge_packages    — a pair of reports (one per condition, in
--                       principle) under neutral slot labels A and
--                       B. The slot assignment is randomized at
--                       package creation so a judge's rating can't
--                       leak the condition even if the same judge
--                       sees many packages.
--
--   judge_assignments — which judge has been asked to rate which
--                       package. One row per (judge, package). The
--                       same package can be assigned to multiple
--                       judges for inter-rater reliability.
--
--   judge_ratings     — the judge's submission. Likert ratings on
--                       quality dimensions, written justifications
--                       per slot, the pairwise winner, and the
--                       condition-guess + confidence (used to
--                       validate blinding).
--
-- The condition labels are stored ONLY in judge_packages so the
-- judge-facing routes never have to consult them. Unblinding for
-- analysis happens in a separate (researcher-only) report.

CREATE SEQUENCE IF NOT EXISTS judge_packages_seq START 1;
CREATE SEQUENCE IF NOT EXISTS judge_assignments_seq START 1;
CREATE SEQUENCE IF NOT EXISTS judge_ratings_seq START 1;


CREATE TABLE IF NOT EXISTS judge_packages (
    id INTEGER PRIMARY KEY DEFAULT nextval('judge_packages_seq'),
    study_id VARCHAR NOT NULL,
    slot_a_report_id INTEGER NOT NULL,
    slot_b_report_id INTEGER NOT NULL,
    slot_a_condition VARCHAR NOT NULL,
    slot_b_condition VARCHAR NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes VARCHAR DEFAULT ''
);

CREATE INDEX IF NOT EXISTS judge_packages_study_idx ON judge_packages (study_id);


CREATE TABLE IF NOT EXISTS judge_assignments (
    id INTEGER PRIMARY KEY DEFAULT nextval('judge_assignments_seq'),
    judge_actor_id INTEGER NOT NULL,
    package_id INTEGER NOT NULL,
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    assigned_by INTEGER NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'completed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS judge_assignments_judge_idx
    ON judge_assignments (judge_actor_id, status);
CREATE INDEX IF NOT EXISTS judge_assignments_package_idx
    ON judge_assignments (package_id);


CREATE TABLE IF NOT EXISTS judge_ratings (
    id INTEGER PRIMARY KEY DEFAULT nextval('judge_ratings_seq'),
    assignment_id INTEGER NOT NULL,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Per-dimension Likert ratings (e.g. completeness, correctness,
    -- conciseness, fidelity). JSON-encoded at the application layer
    -- so the dimension set can evolve without a migration. Shape:
    --   {"completeness": {"a": 5, "b": 6}, "correctness": {...}, ...}
    ratings VARCHAR NOT NULL DEFAULT '{}',
    justification_a VARCHAR DEFAULT '',
    justification_b VARCHAR DEFAULT '',
    -- 'a' or 'b' for the better output; 'tie' if the judge can't pick.
    pairwise_winner VARCHAR CHECK(pairwise_winner IN ('a', 'b', 'tie')),
    -- Which condition does the judge think produced each slot?
    -- Used to validate blinding (correct-guess rate ≈ chance ⇒ blind).
    condition_guess_a VARCHAR
        CHECK(condition_guess_a IN ('elenchus', 'baseline', 'unsure')),
    condition_guess_b VARCHAR
        CHECK(condition_guess_b IN ('elenchus', 'baseline', 'unsure')),
    confidence INTEGER CHECK(confidence BETWEEN 1 AND 7)
);

CREATE INDEX IF NOT EXISTS judge_ratings_assignment_idx
    ON judge_ratings (assignment_id);
