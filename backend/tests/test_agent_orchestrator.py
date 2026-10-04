import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import settings
from database import Base
from models import AgentRun, User
from services.agent_orchestrator import begin_agent_run


class AgentOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(openid="agent-lease-test")
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_running_run_is_not_claimed_until_lease_expires(self):
        run = AgentRun(
            user_id=self.user.id,
            run_key="scheduled:today",
            agent_name="supervisor",
            stage="understand",
            trigger="scheduled",
            status="running",
            started_at=datetime.utcnow(),
        )
        self.db.add(run)
        self.db.commit()

        _, should_run = begin_agent_run(
            self.db,
            user_id=self.user.id,
            run_key="scheduled:today",
            agent_name="supervisor",
            stage="understand",
            trigger="scheduled",
            input_snapshot={},
        )
        self.assertFalse(should_run)

    def test_stale_run_can_be_reclaimed(self):
        original_lease = settings.AGENT_RUN_LEASE_SECONDS
        try:
            settings.AGENT_RUN_LEASE_SECONDS = 30
            run = AgentRun(
                user_id=self.user.id,
                run_key="scheduled:stale",
                agent_name="supervisor",
                stage="understand",
                trigger="scheduled",
                status="running",
                started_at=datetime.utcnow() - timedelta(seconds=60),
            )
            self.db.add(run)
            self.db.commit()

            reclaimed, should_run = begin_agent_run(
                self.db,
                user_id=self.user.id,
                run_key="scheduled:stale",
                agent_name="supervisor",
                stage="understand",
                trigger="scheduled",
                input_snapshot={"retry": True},
            )
            self.assertTrue(should_run)
            self.assertEqual(reclaimed.status, "running")
            self.assertEqual(reclaimed.input_snapshot, '{"retry": true}')
        finally:
            settings.AGENT_RUN_LEASE_SECONDS = original_lease


if __name__ == "__main__":
    unittest.main()
