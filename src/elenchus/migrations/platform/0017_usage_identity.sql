-- version: 17
-- description: The provider's own identification of each call on the
-- usage table too — the model it reports having used and its request id
-- — so spend can be reconciled and a model change noticed from the
-- platform DB alone, without opening every base. Empty for rows written
-- before this migration and for calls that got no response. (DuckDB
-- cannot add a column with a NOT NULL constraint; the writer never
-- stores NULL here.)

ALTER TABLE usage ADD COLUMN response_model VARCHAR DEFAULT '';
ALTER TABLE usage ADD COLUMN request_id VARCHAR DEFAULT '';
