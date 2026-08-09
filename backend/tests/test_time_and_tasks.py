import asyncio
import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import DailyTask, User
from routers.tasks import checkin_task, edit_task
from schemas import TaskCheckInRequest, TaskEditRequest
from services.scheduler_service import get_effective_date
from services.time_service import BEIJING_TZ, utc_naive_to_beijing


class TimeAndTaskTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(openid="time-task-test")
        self.db.add(self.user)
        self.db.commit()
        self.task = DailyTask(
            user_id=self.user.id,
            task_date=datetime(2026, 8, 9).date(),
            goal="test",
            content="write a task",
        )
        self.db.add(self.task)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_business_day_uses_beijing_four_am_boundary(self):
        late_night = datetime(2026, 8, 9, 3, 59, tzinfo=BEIJING_TZ)
        morning = datetime(2026, 8, 9, 4, 0, tzinfo=BEIJING_TZ)
        self.assertEqual(str(get_effective_date(late_night)), "2026-08-08")
        self.assertEqual(str(get_effective_date(morning)), "2026-08-09")

    def test_legacy_utc_timestamp_is_presented_as_beijing_time(self):
        value = utc_naive_to_beijing(datetime(2026, 8, 9, 13, 28))
        self.assertEqual(value.isoformat(), "2026-08-09T21:28:00+08:00")

    def test_checkin_can_be_reversed(self):
        request = TaskCheckInRequest(task_id=self.task.id, openid=self.user.openid)
        done = asyncio.run(checkin_task(request, self.db))
        self.assertTrue(done["is_completed"])
        restored = asyncio.run(checkin_task(request, self.db))
        self.assertFalse(restored["is_completed"])
        self.db.refresh(self.task)
        self.assertFalse(self.task.is_completed)
        self.assertIsNone(self.task.completed_at)

    def test_editing_a_task_preserves_its_completion_state(self):
        self.task.is_completed = True
        self.db.commit()
        request = TaskEditRequest(
            task_id=self.task.id,
            openid=self.user.openid,
            content="updated task title",
        )
        result = asyncio.run(edit_task(request, self.db))
        self.assertEqual(result.content, "updated task title")
        self.assertTrue(result.is_completed)


if __name__ == "__main__":
    unittest.main()
