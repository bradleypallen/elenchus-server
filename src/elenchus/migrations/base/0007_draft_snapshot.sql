-- version: 7
-- description: Which draft of the participant's text the model was shown
-- on each turn (design-notes/text-as-positum.md).
--
-- From 0.14.0 the participant's first draft is the positum and every
-- later turn carries the current draft. `request_content` holds the
-- text as sent; this column names the `text_snapshots` row it came
-- from, so the draft shown at the previous turn — which the opponent
-- is also given, to parse the changes as speech acts — is reproducible
-- from the record rather than guessed from timestamps. NULL for a turn
-- that carried no draft (the ordinary interface, and every turn before
-- 0.14.0).

ALTER TABLE turn_log ADD COLUMN draft_snapshot_id INTEGER;
