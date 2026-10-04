# Database migrations

Run `python -m migrations.runner` from `backend/` against an existing MySQL/CloudBase database before deploying the durable chat idempotency changes. The runner records applied versions in `schema_migrations`, takes a database advisory lock where supported, and adds the user-message request key, its unique index, and the AgentRun lease fencing token.

The SQL files are kept as a reviewable/manual fallback. Do not run both paths concurrently. Resolve duplicate `(user_id, date)` conversations before applying `002_conversation_ownership`; `003_reminder_leases` adds the scheduled-reminder fencing token, its idempotency index, and records its version in `schema_migrations`.

New SQLite databases and local SQLite upgrades are handled by `backend/main.py` at startup.
