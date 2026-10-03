-- version: 25
-- description: The access log can record an administrator's deletion
-- of an ordinary dialectic (docs/data-access.md, policy version 4).
--
-- `content_access_log.action` was constrained to the three read-only
-- fetches plus 'grant'. A deletion under a reason is the fourth kind
-- of act an administrator can take on someone else's dialectic, and
-- the log must say so — the row outlives the dialectic, as the others
-- do. DuckDB cannot alter a CHECK constraint, so the table is rebuilt
-- with the wider one; ids, the sequence default and both indexes are
-- kept.

CREATE TABLE content_access_log_v25 (
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
    CHECK (action IN ('grant', 'view', 'pdf', 'records', 'delete'))
);

INSERT INTO content_access_log_v25
    (id, at_utc, actor_id, action, base_id, owner_id, base_kind, category, reason,
     grant_id, expires_at_utc)
SELECT id, at_utc, actor_id, action, base_id, owner_id, base_kind, category, reason,
       grant_id, expires_at_utc
FROM content_access_log;

DROP INDEX IF EXISTS content_access_log_base_idx;
DROP INDEX IF EXISTS content_access_log_at_idx;
DROP TABLE content_access_log;
ALTER TABLE content_access_log_v25 RENAME TO content_access_log;

CREATE INDEX IF NOT EXISTS content_access_log_base_idx ON content_access_log (base_id);
CREATE INDEX IF NOT EXISTS content_access_log_at_idx ON content_access_log (at_utc);
