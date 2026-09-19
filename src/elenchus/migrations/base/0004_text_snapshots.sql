-- version: 4
-- description: Writing-pane capture. The participant composes their
-- text in an editor beside the dialogue; these two append-only tables
-- record how it came to be, for offline process analysis (when did the
-- text change relative to the dialogue? how much of it arrived by
-- paste?).
--
-- `text_snapshots` — the full text at each autosave. Consecutive
--                    identical contents are not re-stored. The last
--                    row with trigger='submit' is the submitted text.
-- `editor_events`  — things that happened in the editor that a
--                    snapshot can't show: a paste (length and time
--                    only — never the pasted content), a soft timer
--                    warning being shown.
--
-- Same conventions as base/0003: insert-only, ids from sequences
-- re-seeded on open, `at_utc` is the application clock.

CREATE TABLE IF NOT EXISTS text_snapshots (
    id INTEGER PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    at_utc VARCHAR NOT NULL,
    actor_id INTEGER,
    -- 'autosave' | 'blur' | 'paste' | 'submit'
    trigger VARCHAR NOT NULL,
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    word_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS editor_events (
    id INTEGER PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    at_utc VARCHAR NOT NULL,
    actor_id INTEGER,
    -- 'paste' | 'soft_warning_shown'
    event_type VARCHAR NOT NULL,
    payload JSON NOT NULL DEFAULT '{}'
);
