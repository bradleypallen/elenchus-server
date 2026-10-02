-- version: 6
-- description: Which version of the system prompt a turn ran under, by
-- its label (e.g. `elenchus/2026-06-10`) beside the SHA-256 recorded
-- since 0.4. The prompts now live as versioned files in
-- src/elenchus/prompts/; the label is what an analysis groups turns by
-- and what the study's registration cites next to the hash.
-- Turns recorded before this migration keep NULL here; their hash
-- still identifies the text.

ALTER TABLE turn_log ADD COLUMN system_prompt_version VARCHAR;
