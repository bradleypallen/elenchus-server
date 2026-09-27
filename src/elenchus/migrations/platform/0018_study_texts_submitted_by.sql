-- version: 18
-- description: How a session's text came to be submitted — pressed by
-- the participant ('participant') or reached by the clock ('timeout',
-- the hard stop of Registered Report §2.4). A session ended by the
-- clock is a protocol event the analysis plan names; it must be
-- readable from the platform DB and the export, not inferred from
-- snapshot timings. Rows written before this migration were all
-- pressed by the participant.

ALTER TABLE study_texts ADD COLUMN submitted_by VARCHAR DEFAULT 'participant';
