ALTER TABLE scheduled_reminders
    ADD COLUMN processing_token VARCHAR(64) NOT NULL DEFAULT '';
