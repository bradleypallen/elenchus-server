-- version: 23
-- description: The record of every time someone other than its owner
-- looked at a dialectic (docs/data-access.md).
--
-- Reading someone else's dialectic is an explicit act with a stated
-- reason. `content_access_log` is its append-only record:
--
--   action = 'grant'    the reason was given; valid for that actor and
--                       that dialectic until `expires_at_utc`
--   action = 'view'     the content was fetched for the read-only viewer
--   action = 'pdf'      the PDF report was downloaded
--   action = 'records'  the raw records were downloaded (conversation,
--                       turn log, state events, the per-base dump)
--
-- A fetch row carries the `grant_id` it was made under and a copy of
-- the grant's category and reason, so the log reads on its own. Rows
-- are never updated or deleted. Timestamps are explicit naive UTC
-- (`content_access.now_utc`), not CURRENT_TIMESTAMP. Study records are
-- never opened this way, so `base_kind` is always 'ordinary' today; the
-- column is there so the log stays truthful if that ever changes.

CREATE SEQUENCE IF NOT EXISTS content_access_log_seq START 1;

CREATE TABLE IF NOT EXISTS content_access_log (
    id INTEGER PRIMARY KEY DEFAULT nextval('content_access_log_seq'),
    at_utc TIMESTAMP NOT NULL,
    actor_id INTEGER NOT NULL,
    action VARCHAR NOT NULL,
    base_id VARCHAR NOT NULL,
    owner_id INTEGER,
    base_kind VARCHAR DEFAULT 'ordinary',
    category VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    grant_id INTEGER,
    expires_at_utc TIMESTAMP,
    CHECK (action IN ('grant', 'view', 'pdf', 'records'))
);

CREATE INDEX IF NOT EXISTS content_access_log_base_idx ON content_access_log (base_id);
CREATE INDEX IF NOT EXISTS content_access_log_at_idx ON content_access_log (at_utc);
