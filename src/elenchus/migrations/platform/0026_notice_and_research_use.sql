-- version: 26
-- description: The sign-up notice and the research-use choice
-- (docs/data-access.md, policy version 5).
--
-- Every account holder accepts a versioned notice — what the platform
-- records, who can see it, how to delete it — at sign-up, or on first
-- sign-in after this migration for accounts that predate it. The one
-- choice the notice offers is whether the person's ordinary dialectics
-- may be used in the project's research; it defaults to no and can be
-- changed either way at any time. Study participants are not account
-- holders: their consent is the study's and is not recorded here.
--
-- `actors` carries the current state; `consent_events` is the
-- append-only history (which notice version was accepted when, and
-- each time the research-use choice changed), never updated or deleted.

-- Order matters on DuckDB 1.5: an ADD COLUMN with a DEFAULT must be the
-- LAST statement that touches the table in a transaction, or the commit
-- fails with "another transaction has altered this table" (the 0.13.0
-- rehearsal on the production box caught the original order, which had
-- the DEFAULT column third of four). The table and index come first,
-- the four ALTERs after, the DEFAULT one last.

CREATE SEQUENCE IF NOT EXISTS consent_events_seq START 1;

CREATE TABLE IF NOT EXISTS consent_events (
    id INTEGER PRIMARY KEY DEFAULT nextval('consent_events_seq'),
    actor_id INTEGER NOT NULL,
    at_utc TIMESTAMP NOT NULL,
    kind VARCHAR NOT NULL,
    notice_version VARCHAR NOT NULL,
    CHECK (kind IN ('terms_accepted', 'research_use_on', 'research_use_off'))
);

CREATE INDEX IF NOT EXISTS consent_events_actor_idx ON consent_events (actor_id);

ALTER TABLE actors ADD COLUMN terms_version VARCHAR;
ALTER TABLE actors ADD COLUMN terms_accepted_at TIMESTAMP;
ALTER TABLE actors ADD COLUMN research_use_set_at TIMESTAMP;
ALTER TABLE actors ADD COLUMN research_use BOOLEAN DEFAULT false;
