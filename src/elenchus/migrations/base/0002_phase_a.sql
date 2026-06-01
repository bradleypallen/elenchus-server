-- version: 2
-- description: Phase A schema extensions.
--
-- Adds the future-proofing columns the multi-user platform needs to
-- attribute every base mutation to a specific actor/contributor and to
-- scope position/tension/conversation state to a session and a case
-- (epistemic context). Also seeds the `cases` table with a single
-- default case so existing single-user code paths continue to work
-- without explicit case management.
--
-- All ALTER TABLE ADD COLUMN statements use DEFAULT values so existing
-- rows are populated immediately and the migration is safe to apply to
-- legacy single-user dialectic files. The default contributor/actor id
-- of 1 conventionally refers to the admin actor created by
-- `elenchus migrate-legacy`; per-base files without a corresponding
-- platform.actors row are still readable, the foreign-key relationship
-- is enforced at the application layer.

-- ─── atoms: contributor attribution + paraphrases + bibliographic refs ─

ALTER TABLE atoms ADD COLUMN contributor_id INTEGER DEFAULT 1;
ALTER TABLE atoms ADD COLUMN paraphrases JSON DEFAULT '[]';
-- "references" is a SQL reserved word in some dialects; DuckDB accepts
-- it unquoted but we double-quote for portability.
ALTER TABLE atoms ADD COLUMN "references" JSON DEFAULT '[]';

-- ─── assessments: contributor + provenance + lifecycle status ─────────
-- `domain` already exists from 0001; we leave its default ('' = unset)
-- alone rather than changing it under existing rows.

ALTER TABLE assessments ADD COLUMN contributor_id INTEGER DEFAULT 1;
ALTER TABLE assessments ADD COLUMN provenance JSON DEFAULT '{}';
ALTER TABLE assessments ADD COLUMN status VARCHAR DEFAULT 'active';

-- ─── positions: actor + case scoping ──────────────────────────────────

ALTER TABLE positions ADD COLUMN actor_id INTEGER DEFAULT 1;
ALTER TABLE positions ADD COLUMN case_id INTEGER DEFAULT 1;

-- ─── tensions: case scoping ───────────────────────────────────────────

ALTER TABLE tensions ADD COLUMN case_id INTEGER DEFAULT 1;

-- ─── conversation: session/case/actor scoping ─────────────────────────
-- session_id defaults to 1 (the default session created at base open
-- time). case_id and actor_id are nullable for system-generated turns
-- like the summary regeneration job.

ALTER TABLE conversation ADD COLUMN session_id INTEGER DEFAULT 1;
ALTER TABLE conversation ADD COLUMN case_id INTEGER;
ALTER TABLE conversation ADD COLUMN actor_id INTEGER;

-- ─── New: cases ──────────────────────────────────────────────────────
-- A "case" is a distinct epistemic context inside a base — e.g. the
-- core theory vs. an applied scenario in which the theory's claims are
-- tested. Multi-respondent dialectical features (Phase B+) will scope
-- view-relative endorsements by case_id without forking the base.
-- Every per-base file has at least one default case with id=1; new
-- cases can be introduced later without a migration.

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    name VARCHAR NOT NULL DEFAULT 'default',
    description VARCHAR DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO cases (id, name, description)
    VALUES (1, 'default', 'Default case for this base.');

-- ─── Refresh views to honor the new `status` column ──────────────────
-- `current_assessments` now filters on status='active' so retracted /
-- superseded rows are excluded from the consequence relation. The
-- shape of the view is unchanged for backwards compatibility with code
-- that reads (premises, conclusions, judgment, contributor, ...).

CREATE OR REPLACE VIEW current_assessments AS
WITH ranked AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY premises, conclusions, contributor
            ORDER BY assessed_at DESC
        ) AS rn
    FROM assessments
    WHERE status = 'active'
)
SELECT premises, conclusions, judgment, contributor,
       assessed_at, reason, domain
FROM ranked WHERE rn = 1;

CREATE OR REPLACE VIEW base_sequents AS
SELECT premises, conclusions,
       COUNT(*) AS n_assessors,
       MIN(assessed_at) AS first_assessed
FROM current_assessments
WHERE judgment = 'holds'
GROUP BY premises, conclusions
HAVING COUNT(*) = (
    SELECT COUNT(DISTINCT contributor)
    FROM current_assessments ca2
    WHERE ca2.premises = current_assessments.premises
      AND ca2.conclusions = current_assessments.conclusions
);
