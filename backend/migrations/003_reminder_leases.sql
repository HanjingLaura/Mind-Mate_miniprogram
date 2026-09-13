ALTER TABLE subscribe_auths
    ADD COLUMN scene VARCHAR(64) DEFAULT 'general';

ALTER TABLE subscribe_auths
    ADD COLUMN used_at DATETIME NULL;

ALTER TABLE scheduled_reminders
    ADD COLUMN processing_token VARCHAR(64) NOT NULL DEFAULT '';
