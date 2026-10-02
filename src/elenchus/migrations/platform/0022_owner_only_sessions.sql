-- version: 22
-- description: Only the owner has a session on a dialectic.
--
-- Until 0.9.5 an admin's home page listed every base in the platform
-- and opened a session row of the admin's own on each one (actor = the
-- admin, the column default `state = 'active'`, no study token). On a
-- study task base that row was newer than the participant's study
-- session, and the freeze ("the archived base is the state at
-- submission") looked up "the newest session on this base" — so once
-- any admin had opened their home page, a submitted session's base took
-- edits again.
--
-- The lookup now selects the study session by its token and the list
-- creates nothing for a base the caller doesn't own. This removes the
-- rows already there: every non-study session whose actor is not the
-- owner of its base. Study sessions (study_token IS NOT NULL) and every
-- owner's own sessions are untouched; nothing references these rows.

DELETE FROM sessions
WHERE study_token IS NULL
  AND EXISTS (
      SELECT 1 FROM bases b
      WHERE b.id = sessions.base_id AND b.owner_id <> sessions.actor_id
  );
