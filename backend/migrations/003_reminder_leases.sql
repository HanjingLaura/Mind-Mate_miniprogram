ALTER TABLE subscribe_auths
    ADD COLUMN scene VARCHAR(64) DEFAULT 'general';

ALTER TABLE subscribe_auths
    ADD COLUMN used_at DATETIME NULL;

ALTER TABLE scheduled_reminders
    ADD COLUMN processing_token VARCHAR(64) NOT NULL DEFAULT '';

CREATE UNIQUE INDEX uq_scheduled_reminder_request
    ON scheduled_reminders (user_id, idempotency_key);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(128) PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL
);

INSERT INTO schema_migrations (version, applied_at)
SELECT '003_reminder_leases', CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM schema_migrations WHERE version = '003_reminder_leases'
);
