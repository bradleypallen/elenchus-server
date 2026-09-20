-- version: 15
-- description: A study's own task length.
--
-- The writing pane's clock (guidance only — nothing is ever cut off) ran
-- off one server-wide setting, `ELENCHUS_TASK_MINUTES`. So a practice
-- study with a five-minute task needed someone with a shell on the
-- server to change an environment variable and restart it — and put the
-- real study on the short clock too while it was set. The length
-- belongs to the study: a `TRAINING` study can run at 5 minutes beside a
-- `PILOT` at 60, set by whoever runs the study, from the Study tab.
--
-- NULL means "the server's default" (`ELENCHUS_TASK_MINUTES`, else 60),
-- so existing studies behave exactly as before. Read when a session's
-- screen is loaded, like `min_gap_hours` — not copied onto links at
-- enrolment — so set it before the first participant starts and leave
-- it alone afterwards; every change is logged.

ALTER TABLE study_configs ADD COLUMN task_minutes INTEGER;
