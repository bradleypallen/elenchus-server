-- version: 8
-- Password reset support: a hashed-token reset table (admin- and
-- user-initiated) plus a "must change password at next login" flag.

-- Force-change-at-next-login. Powers "reactivate + require new password"
-- and admin "require password change". Backfills false for existing rows.
ALTER TABLE actors ADD COLUMN must_change_password BOOLEAN DEFAULT false;

-- Reset tokens. We store only the SHA-256 HASH of the token; the raw
-- token lives solely in the emailed/shared link, so a leaked DB or backup
-- yields no usable reset links. Single-use (used_at) + short TTL
-- (expires_at). created_by is the admin actor for admin-issued resets
-- (NULL = self-service); request_ip is kept for rate-limiting / audit.
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash VARCHAR PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    created_by INTEGER,
    request_ip VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_password_resets_actor ON password_resets(actor_id);
