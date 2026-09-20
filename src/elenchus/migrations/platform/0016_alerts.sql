-- version: 16
-- description: Keep the alerts the platform raises, so an admin can see
-- them.
--
-- Alerts (an LLM outage, a revoked key, a day's spend far above normal)
-- went to the server log, and to email if `ALERT_EMAIL_TO` and an SMTP
-- backend were configured. An admin without a shell on the server — the
-- person actually running a study — saw none of it unless email happened
-- to be set up. `alerting.DatabaseAlertChannel` now also writes each
-- dispatched alert here, and the admin dashboard's System tab lists them.
--
-- Only alerts that were actually dispatched are stored: one swallowed by
-- the dispatcher's dedup window isn't. The channel keeps the newest
-- `alerting.ALERT_HISTORY_ROWS` rows and deletes older ones, so this can't
-- grow without bound during a provider outage. `at_utc` is written
-- explicitly (naive UTC), not from CURRENT_TIMESTAMP.

CREATE SEQUENCE IF NOT EXISTS alerts_seq START 1;

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY DEFAULT nextval('alerts_seq'),
    at_utc TIMESTAMP NOT NULL,
    severity VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    subject VARCHAR NOT NULL,
    body VARCHAR NOT NULL DEFAULT '',
    metadata VARCHAR NOT NULL DEFAULT '{}'
);
