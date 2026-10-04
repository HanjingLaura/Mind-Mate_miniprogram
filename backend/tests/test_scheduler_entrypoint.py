import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import scheduler


class SchedulerEntrypointTests(unittest.TestCase):
    def test_sqlite_run_remains_compatible_without_mysql_lock(self):
        test_engine = create_engine("sqlite:///:memory:")
        try:
            with (
                patch.object(scheduler, "engine", test_engine),
                patch.object(scheduler, "SessionLocal", sessionmaker()),
                patch.object(scheduler, "finalize_previous_day_tasks", return_value=2),
                patch.object(
                    scheduler,
                    "run_supervision_cycle",
                    AsyncMock(return_value=[]),
                ),
            ):
                result = asyncio.run(scheduler.run_supervision_job_once())

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["finalized"], 2)
            self.assertEqual(result["results"], [])
        finally:
            test_engine.dispose()

    def test_mysql_lock_connection_is_not_reused_for_business_session(self):
        fake_engine = MagicMock()
        fake_engine.dialect.name = "mysql"
        raw_lock_connection = MagicMock()
        lock_connection = MagicMock()
        fake_engine.connect.return_value = raw_lock_connection
        raw_lock_connection.execution_options.return_value = lock_connection
        lock_connection.execute.return_value.scalar.return_value = 1
        fake_session = MagicMock()
        session_factory = MagicMock(return_value=fake_session)

        with (
            patch.object(scheduler, "engine", fake_engine),
            patch.object(scheduler, "SessionLocal", session_factory),
            patch.object(scheduler, "finalize_previous_day_tasks", return_value=0),
            patch.object(
                scheduler,
                "run_supervision_cycle",
                AsyncMock(return_value=[]),
            ),
        ):
            result = asyncio.run(scheduler.run_supervision_job_once())

        self.assertEqual(result["status"], "completed")
        raw_lock_connection.execution_options.assert_called_once_with(
            isolation_level="AUTOCOMMIT",
        )
        # Passing bind=lock_connection here would make inner Session commits
        # subordinate to an outer transaction and roll them back on close.
        session_factory.assert_called_once_with()
        lock_connection.close.assert_called_once_with()

    def test_cron_endpoint_rejects_missing_or_short_secret(self):
        with patch.object(main.settings, "INTERNAL_CRON_SECRET", "short"):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(main.run_supervision_cron("short"))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_cron_endpoint_rejects_wrong_secret(self):
        secret = "a" * 32
        with patch.object(main.settings, "INTERNAL_CRON_SECRET", secret):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(main.run_supervision_cron("b" * 32))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_cron_endpoint_returns_only_safe_summary(self):
        secret = "a" * 32
        result = {
            "status": "completed",
            "finalized": 3,
            "results": [{"message": "private reminder"}],
        }
        with (
            patch.object(main.settings, "INTERNAL_CRON_SECRET", secret),
            patch.object(
                main,
                "run_supervision_job_once",
                AsyncMock(return_value=result),
            ),
        ):
            response = asyncio.run(main.run_supervision_cron(secret))

        self.assertEqual(response, {
            "status": "completed",
            "finalized": 3,
            "delivered_count": 1,
        })


if __name__ == "__main__":
    unittest.main()
