"""Small, repeatable production migration runner.

Run from ``backend/`` with ``python -m migrations.runner``. SQLite startup
upgrades remain in ``main.py`` for local development; this runner is the
explicit deployment path for persistent databases.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table, inspect, text

from database import engine


MIGRATION_VERSIONS = ("001_agent_idempotency", "002_conversation_ownership")
LOCK_NAME = "mindmate_schema_migrations"


@contextmanager
def _migration_lock(conn):
    dialect = conn.dialect.name
    if dialect == "mysql":
        acquired = conn.execute(text("SELECT GET_LOCK(:name, 60)"), {"name": LOCK_NAME}).scalar()
        if acquired != 1:
            raise RuntimeError("Could not acquire MySQL migration lock")
    elif dialect == "postgresql":
        conn.execute(text("SELECT pg_advisory_lock(hashtext(:name))"), {"name": LOCK_NAME})
    try:
        yield
    finally:
        if dialect == "mysql":
            conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})
        elif dialect == "postgresql":
            conn.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": LOCK_NAME})


def _ensure_migration_table(conn) -> Table:
    metadata = MetaData()
    table = Table(
        "schema_migrations",
        metadata,
        Column("version", String(128), primary_key=True),
        Column("applied_at", DateTime, nullable=False),
    )
    metadata.create_all(conn, tables=[table])
    return table


def _add_column_if_missing(conn, table_name: str, column_name: str, ddl: str) -> None:
    columns = {column["name"] for column in inspect(conn).get_columns(table_name)}
    if column_name not in columns:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))


def _ensure_request_index(conn) -> None:
    indexes = {index["name"] for index in inspect(conn).get_indexes("messages")}
    if "uq_message_request" in indexes:
        return
    if conn.dialect.name == "sqlite":
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_message_request "
            "ON messages (conversation_id, request_id) WHERE request_id IS NOT NULL"
        ))
    else:
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_message_request "
            "ON messages (conversation_id, request_id)"
        ))


def _ensure_conversation_index(conn) -> None:
    indexes = {index["name"] for index in inspect(conn).get_indexes("conversations")}
    if "uq_conversation_user_date" in indexes:
        return
    conn.execute(text(
        "CREATE UNIQUE INDEX uq_conversation_user_date "
        "ON conversations (user_id, date)"
    ))


def run_pending_migrations() -> None:
    with engine.begin() as conn:
        with _migration_lock(conn):
            migrations = _ensure_migration_table(conn)
            for version in MIGRATION_VERSIONS:
                applied = conn.execute(
                    migrations.select().where(migrations.c.version == version)
                ).first()
                if applied:
                    print(f"already applied: {version}")
                    continue
                if version == "001_agent_idempotency":
                    _add_column_if_missing(conn, "messages", "request_id", "request_id VARCHAR(128) NULL")
                    _add_column_if_missing(
                        conn,
                        "agent_runs",
                        "lease_token",
                        "lease_token VARCHAR(64) NOT NULL DEFAULT ''",
                    )
                    _ensure_request_index(conn)
                elif version == "002_conversation_ownership":
                    _ensure_conversation_index(conn)
                conn.execute(migrations.insert().values(
                    version=version,
                    applied_at=datetime.utcnow(),
                ))
                print(f"applied: {version}")


if __name__ == "__main__":
    run_pending_migrations()
