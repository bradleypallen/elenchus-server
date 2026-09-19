-- version: 12
-- description: What each LLM call was *for*.
--
-- `usage.category` is the outcome class of the call (success,
-- rate_limit, …). It says nothing about why the call was made, so the
-- cost dashboard couldn't separate a participant's task turns from the
-- platform's own overhead (rolling summaries, report generation,
-- simulation personas). `purpose` carries that — the vocabulary lives
-- in `costs.PURPOSES`:
--
--   dialectic_turn   an Elenchus-condition opponent turn
--   baseline_turn    a baseline-condition chat turn
--   rolling_summary  the every-20-messages context summary
--   report_summary   the analytical summary for a PDF report
--   study_report     the legacy LLM-generated structured study report
--   sim_persona      a simulated participant / judge (`elenchus sim --llm`)
--
-- Rows written before this migration keep the empty default and are
-- shown as "unlabelled". (Before it, the two summary calls and the
-- simulation personas wrote no usage row at all, so that spend is
-- absent from the history rather than mislabelled.)
--
-- `cost_usd` stays, as the estimate made when the call was recorded,
-- but nothing reads it for reporting any more: every displayed figure
-- is priced from the token counts at read time (`pricing.py`,
-- `costs.py`), because the stored figure is only as good as the price
-- table was on the day of the call.

ALTER TABLE usage ADD COLUMN purpose VARCHAR DEFAULT '';

CREATE INDEX IF NOT EXISTS usage_purpose_idx ON usage (purpose);
