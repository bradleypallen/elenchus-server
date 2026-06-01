-- version: 1
-- description: initial per-base schema (consolidated from MaterialBase _SCHEMA_SQL
-- and DialecticalState _ensure_tables). All CREATE statements use IF NOT EXISTS
-- so this migration is safe to run against existing single-user dialectic files
-- that were created before the migration runner was introduced.

-- ─── Meta ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS meta (
    key VARCHAR PRIMARY KEY,
    value VARCHAR
);

-- ─── MaterialBase: atomic language and consequence relation ───────────

CREATE SEQUENCE IF NOT EXISTS assessment_seq START 1;

CREATE TABLE IF NOT EXISTS atoms (
    sentence VARCHAR PRIMARY KEY,
    added_by VARCHAR DEFAULT 'system',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description VARCHAR DEFAULT ''
);

CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER DEFAULT nextval('assessment_seq'),
    premises VARCHAR NOT NULL,
    conclusions VARCHAR NOT NULL,
    judgment VARCHAR NOT NULL,
    contributor VARCHAR NOT NULL,
    assessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason VARCHAR DEFAULT '',
    domain VARCHAR DEFAULT '',
    CHECK(judgment IN ('holds', 'rejected'))
);

CREATE OR REPLACE VIEW current_assessments AS
WITH ranked AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY premises, conclusions, contributor
            ORDER BY assessed_at DESC
        ) AS rn
    FROM assessments
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

-- ─── DialecticalState: positions, tensions, conversation ──────────────

CREATE TABLE IF NOT EXISTS positions (
    atom VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    status VARCHAR DEFAULT 'open',
    introduced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK(side IN ('C', 'D')),
    CHECK(status IN ('open', 'retracted')),
    PRIMARY KEY(atom, side)
);

CREATE TABLE IF NOT EXISTS tensions (
    id INTEGER PRIMARY KEY,
    gamma VARCHAR NOT NULL,
    delta VARCHAR NOT NULL,
    reason VARCHAR DEFAULT '',
    status VARCHAR DEFAULT 'open',
    proposed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    CHECK(status IN ('open', 'accepted', 'contested'))
);

CREATE TABLE IF NOT EXISTS conversation (
    id INTEGER PRIMARY KEY,
    role VARCHAR NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- The tension_seq and conv_seq sequences are not declared here. They
-- are re-seeded procedurally from MAX(id) on every database open
-- (DialecticalState._reseed_sequences) to stay in sync with the table
-- contents across reconnects. A future migration replaces this pattern
-- with proper identity columns; for now, the procedural pattern is
-- preserved to minimize behavior change.
