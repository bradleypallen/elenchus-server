-- version: 21
-- description: Allocation as the study's registration describes it
-- (§2.1): the allocation list is generated once from a SEED entered by
-- a team member who does not administer sessions; the seed's hash and a
-- hash of the list are deposited with the registration; a participant's
-- sequence is revealed to the session administrator only when their
-- first session is scheduled. Until a study has a seed, enrolment draws
-- and reveals at once, as before (the practice study).
--
-- `study_configs.allocation_seed`  — the seed (never returned by the
--                                    API; only its hash), `..._set_by`,
--                                    `..._set_at`, and `planned_n`.
-- `study_participants.block_index` — the participant's place in the
--                                    block-allocated sequence (manual
--                                    placements have none).
-- `study_participants.revealed_at` — NULL while the sequence is hidden;
--                                    set, with `revealed_by`, when
--                                    session 1 is scheduled and the two
--                                    links are issued.

ALTER TABLE study_configs ADD COLUMN allocation_seed VARCHAR DEFAULT '';
ALTER TABLE study_configs ADD COLUMN allocation_seed_set_by INTEGER;
ALTER TABLE study_configs ADD COLUMN allocation_seed_set_at TIMESTAMP;
ALTER TABLE study_configs ADD COLUMN planned_n INTEGER DEFAULT 48;

ALTER TABLE study_participants ADD COLUMN block_index INTEGER;
ALTER TABLE study_participants ADD COLUMN revealed_at TIMESTAMP;
ALTER TABLE study_participants ADD COLUMN revealed_by INTEGER;
