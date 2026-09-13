# Database migrations

Run `001_agent_idempotency.sql` against an existing MySQL/CloudBase database before deploying the durable chat idempotency changes. The migration adds the user-message request key, its unique index, and the AgentRun lease fencing token.

New SQLite databases and local SQLite upgrades are handled by `backend/main.py` at startup.
