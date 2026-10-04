-- Apply once to an existing MySQL/CloudBase database before deploying 70fae28+.
-- SQLite development databases are upgraded by backend/main.py.

ALTER TABLE messages
    ADD COLUMN request_id VARCHAR(128) NULL;

ALTER TABLE agent_runs
    ADD COLUMN lease_token VARCHAR(64) NOT NULL DEFAULT '';

CREATE UNIQUE INDEX uq_message_request
    ON messages (conversation_id, request_id);
