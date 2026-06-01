-- version: 1
-- description: initial platform.duckdb schema. Carries identity (actors),
-- session tokens (auth_sessions, magic_links), invite-based signup
-- (invites), and the metadata that ties bases and per-actor sessions
-- together. Per-base content lives in separate per-base DuckDB files;
-- this file is held open for the process lifetime.

-- ─── Meta ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS meta (
    key VARCHAR PRIMARY KEY,
    value VARCHAR
);

-- ─── Identity ─────────────────────────────────────────────────────────

CREATE SEQUENCE IF NOT EXISTS actors_id_seq START 1;

CREATE TABLE IF NOT EXISTS actors (
    id INTEGER PRIMARY KEY DEFAULT nextval('actors_id_seq'),
    kind VARCHAR NOT NULL,
    email VARCHAR UNIQUE,
    display_name VARCHAR NOT NULL,
    password_hash VARCHAR,
    credentials VARCHAR DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deactivated_at TIMESTAMP,
    CHECK(kind IN (
        'admin',
        'researcher',
        'user',
        'judge',
        'participant',
        'opponent_llm',
        'system'
    ))
);

-- ─── Authentication ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS auth_sessions (
    token VARCHAR PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
);

-- Magic-link tokens for passwordless login. Single-use, short expiry.
CREATE TABLE IF NOT EXISTS magic_links (
    token VARCHAR PRIMARY KEY,
    email VARCHAR NOT NULL,
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP,
    consumed_by INTEGER
);

-- ─── Invites ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS invites (
    token VARCHAR PRIMARY KEY,
    role VARCHAR NOT NULL,
    intended_email VARCHAR,
    issued_by INTEGER,
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    consumed_at TIMESTAMP,
    consumed_by INTEGER,
    metadata VARCHAR DEFAULT '{}',
    CHECK(role IN ('admin', 'researcher', 'user', 'judge'))
);

-- ─── Bases (knowledge-base metadata; per-base DuckDB content lives elsewhere) ─

CREATE SEQUENCE IF NOT EXISTS bases_id_seq START 1;

CREATE TABLE IF NOT EXISTS bases (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    owner_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id, name)
);

-- ─── Per-respondent sessions against a base ───────────────────────────

CREATE SEQUENCE IF NOT EXISTS sessions_id_seq START 1;

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY DEFAULT nextval('sessions_id_seq'),
    actor_id INTEGER NOT NULL,
    base_id VARCHAR NOT NULL,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    status VARCHAR DEFAULT 'open',
    CHECK(status IN ('open', 'closed', 'expired'))
);

-- ─── Platform-wide settings ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS platform_settings (
    key VARCHAR PRIMARY KEY,
    value VARCHAR
);

-- Seed defaults. Use OR IGNORE so re-running on an already-migrated DB
-- doesn't fail (the migration runner shouldn't re-run this, but
-- defending against it is cheap).
INSERT OR IGNORE INTO platform_settings VALUES ('signup_mode', 'invite_only');

-- Note: cross-database foreign keys (actor_id in per-base files →
-- actors.id here) cannot be enforced by DuckDB. Application-layer
-- validation handles this via db/platform.py:actor_exists() and
-- periodic audit (see ROADMAP §Cross-DB Integrity).
