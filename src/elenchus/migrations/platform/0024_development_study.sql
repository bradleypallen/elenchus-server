-- version: 24
-- description: A study can be marked as a development study — one the
-- team runs on itself to tune the system, never a registered study.
--
-- Its sessions go through the whole participant flow (practice, task,
-- writing pane, clock, post-session), so they exercise exactly what a
-- real participant would see; but their records are the team's own
-- material, and the Dialectics tab may open them under a reason, logged
-- with base_kind = 'study_dev' (docs/data-access.md, policy version 3).
-- A development study can't carry an allocation seed, and a study with
-- a seed can't be marked development: the two states are exclusive, and
-- the flag never leaves a study once its records exist (server rule).

ALTER TABLE study_configs ADD COLUMN development BOOLEAN DEFAULT FALSE;
