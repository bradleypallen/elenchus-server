-- version: 3
-- description: Research capture log. Two append-only tables that record
-- what happened in a dialectic at a grain the live tables don't keep,
-- so formal analysis (NMMS, RDF translation) can be done offline from
-- the captured data alone.
--
-- Why the live tables aren't enough: `conversation` stores only the
-- opponent's clean prose (the raw LLM envelope is discarded at parse
-- time); `positions` is an upsert keyed on (atom, side), so a
-- re-commit overwrites history and a retraction has no timestamp; and
-- nothing records *which turn* produced a given state change, or that
-- a change came from a UI button rather than the opponent.
--
-- `turn_log`     — one row per LLM exchange, both study conditions,
--                  including exchanges where the LLM call failed.
-- `state_events` — one row per state transition (or attempted
--                  transition), whatever code path made it.
--
-- Rows are only ever inserted. Ids come from the `turn_log_seq` /
-- `state_event_seq` sequences, which — like `conv_seq` / `tension_seq`
-- — are re-seeded from MAX(id) on every open
-- (DialecticalState._reseed_sequences) rather than declared here, so a
-- restore from backup can never hand out an id that already exists.
--
-- `at_utc` is the application clock (ISO-8601 with offset, microsecond
-- precision). `created_at` is DuckDB's CURRENT_TIMESTAMP, which is the
-- *transaction start* time — every event of one turn shares it — so
-- order by `id`, and use `at_utc` for timing.

CREATE TABLE IF NOT EXISTS turn_log (
    id INTEGER PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    at_utc VARCHAR NOT NULL,
    -- 'elenchus' (structured opponent) or 'baseline' (free-form chat).
    mode VARCHAR NOT NULL CHECK(mode IN ('elenchus', 'baseline')),
    -- 'ok' — the exchange completed and was applied;
    -- 'llm_error' — the LLM call failed, nothing was applied.
    outcome VARCHAR NOT NULL CHECK(outcome IN ('ok', 'llm_error')),
    actor_id INTEGER,
    -- What the respondent typed (or the UI's follow-up sentence).
    user_message TEXT NOT NULL,
    -- The two-phase UI flow's structured context, when present.
    action_context JSON,
    -- Exactly what the LLM was shown as the final user message.
    request_content TEXT,
    -- How many prior conversation messages were sent, and whether the
    -- rolling summary stood in for the older ones.
    history_window INTEGER,
    summary_included BOOLEAN,
    system_prompt_name VARCHAR,
    system_prompt_sha256 VARCHAR,
    -- Dialectical state as the LLM saw it / as the turn left it.
    -- NULL for baseline turns, which have no formal state.
    state_before JSON,
    state_after JSON,
    -- Verbatim LLM output, before any parsing or repair.
    raw_text TEXT,
    -- Which recovery path produced `parsed` (see response_parsing.py).
    parse_strategy VARCHAR,
    parsed JSON,
    user_conversation_id INTEGER,
    assistant_conversation_id INTEGER,
    model VARCHAR,
    attempts INTEGER,
    latency_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    error_category VARCHAR,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS state_events (
    id INTEGER PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    at_utc VARCHAR NOT NULL,
    -- The turn_log row whose speech acts caused this event; NULL when
    -- the change did not come from an LLM exchange.
    turn_id INTEGER,
    -- 'opponent' — applied from the LLM's parsed speech acts;
    -- 'ui'       — a direct UI action (accept / contest / retract);
    -- 'direct'   — any other caller (CLI, scripts, admin tooling).
    source VARCHAR NOT NULL,
    actor_id INTEGER,
    event_type VARCHAR NOT NULL,
    -- 'applied' — state changed; 'noop' — well-formed but changed
    -- nothing (e.g. retracting what isn't held); 'dropped' — refused
    -- (Phase B firewall, malformed or unknown speech act).
    outcome VARCHAR NOT NULL CHECK(outcome IN ('applied', 'noop', 'dropped')),
    payload JSON NOT NULL DEFAULT '{}',
    note VARCHAR DEFAULT ''
);

CREATE INDEX IF NOT EXISTS state_events_turn_idx ON state_events (turn_id);
