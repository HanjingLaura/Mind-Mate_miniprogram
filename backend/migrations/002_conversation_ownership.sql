-- Apply after 001_agent_idempotency on an existing MySQL/CloudBase database.
-- Resolve any duplicate user/date conversations before creating this index.

CREATE UNIQUE INDEX uq_conversation_user_date
    ON conversations (user_id, date);
