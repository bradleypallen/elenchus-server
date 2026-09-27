-- version: 5
-- description: The identity of each inference call, for the study's
-- model-stability protocol: the model the provider reports having used
-- (which may carry a release date the requested name does not), the
-- provider's request id, and the sampling parameters sent. A participant
-- whose two sessions returned different identifiers is a "straddled
-- pair"; that can only be detected from what the provider returned, not
-- from what was asked for. NULL on rows written before this migration,
-- and on failed calls that never got a response.

ALTER TABLE turn_log ADD COLUMN response_model VARCHAR;
ALTER TABLE turn_log ADD COLUMN request_id VARCHAR;
ALTER TABLE turn_log ADD COLUMN temperature DOUBLE;
ALTER TABLE turn_log ADD COLUMN max_tokens INTEGER;
