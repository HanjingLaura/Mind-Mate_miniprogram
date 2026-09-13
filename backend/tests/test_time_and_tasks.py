import asyncio
import json
import unittest
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import AgentRun, Conversation, DailyTask, Message, SubscribeAuth, SupervisionLog, User
from routers.tasks import checkin_task, edit_task
from schemas import TaskCheckInRequest, TaskEditRequest
from config import settings
from services.scheduler_service import (
    _deliver_pending_wechat_notice,
    get_due_supervision_types,
    get_effective_date,
    run_supervision_cycle,
)
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
        done_request = TaskCheckInRequest(
            task_id=self.task.id, openid=self.user.openid, completed=True
        )
        done = asyncio.run(checkin_task(done_request, self.db, self.user.openid))
        self.assertTrue(done["is_completed"])
        self.assertTrue(done["encouragement"])
        self.assertIsNotNone(done["encouragement_message_id"])
        repeated = asyncio.run(checkin_task(done_request, self.db, self.user.openid))
        self.assertTrue(repeated["is_completed"])
        self.assertFalse(repeated["state_changed"])
        self.assertFalse(repeated["encouragement"])
        restored = asyncio.run(checkin_task(TaskCheckInRequest(
            task_id=self.task.id, openid=self.user.openid, completed=False
        ), self.db, self.user.openid))
        self.assertFalse(restored["is_completed"])
        self.db.refresh(self.task)
        self.assertFalse(self.task.is_completed)
        self.assertIsNone(self.task.completed_at)
        run = self.db.query(AgentRun).filter(
            AgentRun.user_id == self.user.id,
            AgentRun.agent_name == "task-checkin",
        ).order_by(AgentRun.id.desc()).first()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.stage, "reflect")

    def test_editing_a_task_preserves_its_completion_state(self):
        self.task.is_completed = True
        self.db.commit()
        request = TaskEditRequest(
            task_id=self.task.id,
            openid=self.user.openid,
            content="updated task title",
        )
        result = asyncio.run(edit_task(request, self.db, self.user.openid))
        self.assertEqual(result.content, "updated task title")
        self.assertTrue(result.is_completed)

    def test_free_user_receives_morning_and_evening_supervision_slots(self):
        self.user.is_vip = False
        self.user.morning_time = time(9, 0)
        self.user.evening_time = time(20, 0)
        self.db.commit()

        morning = datetime(2026, 8, 9, 10, 0, tzinfo=BEIJING_TZ)
        evening = datetime(2026, 8, 9, 21, 0, tzinfo=BEIJING_TZ)
        self.assertIn("morning_goal", get_due_supervision_types(self.db, self.user, morning))
        self.assertIn("evening_review", get_due_supervision_types(self.db, self.user, evening))

    def test_supervision_budget_caps_a_single_scheduler_round(self):
        original_limit = settings.AGENT_MAX_DAILY_INTERVENTIONS
        try:
            settings.AGENT_MAX_DAILY_INTERVENTIONS = 1
            self.user.is_vip = True
            self.user.morning_time = time(9, 0)
            self.task.created_at = datetime(2026, 8, 9, 0, 0)
            self.db.commit()

            due = get_due_supervision_types(
                self.db,
                self.user,
                datetime(2026, 8, 9, 10, 0, tzinfo=BEIJING_TZ),
            )
            self.assertLessEqual(len(due), 1)
        finally:
            settings.AGENT_MAX_DAILY_INTERVENTIONS = original_limit

    def test_zero_supervision_budget_disables_proactive_intervention(self):
        original_limit = settings.AGENT_MAX_DAILY_INTERVENTIONS
        try:
            settings.AGENT_MAX_DAILY_INTERVENTIONS = 0
            self.user.morning_time = time(9, 0)
            self.db.commit()
            self.assertEqual(
                get_due_supervision_types(
                    self.db,
                    self.user,
                    datetime(2026, 8, 9, 10, 0, tzinfo=BEIJING_TZ),
                ),
                [],
            )
        finally:
            settings.AGENT_MAX_DAILY_INTERVENTIONS = original_limit

    def test_failed_wechat_notice_can_retry_after_new_auth(self):
        now = datetime(2026, 8, 9, 21, 0, tzinfo=BEIJING_TZ)
        conversation = Conversation(
            user_id=self.user.id,
            date=get_effective_date(now),
            title="retry notification",
        )
        self.db.add(conversation)
        self.db.flush()
        message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="继续完成这一件事",
        )
        self.db.add(message)
        self.db.flush()
        log = SupervisionLog(
            user_id=self.user.id,
            supervision_type="morning_goal",
            task_date=get_effective_date(now),
            channel="wechat_failed",
            message_id=message.id,
            sent_at=datetime.utcnow(),
        )
        auth = SubscribeAuth(
            user_id=self.user.id,
            template_id="template-retry",
            scene="morning_goal",
        )
        self.db.add_all([log, auth])
        self.db.commit()

        with (
            patch("config.settings.WECHAT_SUBSCRIBE_TEMPLATE_ID", "template-retry"),
            patch(
                "services.wechat_service.wechat_service.send_subscribe_message",
                AsyncMock(return_value=True),
            ),
        ):
            result = asyncio.run(_deliver_pending_wechat_notice(
                self.db,
                self.user,
                get_effective_date(now),
                now,
            ))

        self.db.refresh(auth)
        self.db.refresh(log)
        self.assertIsNotNone(result)
        self.assertTrue(auth.used)
        self.assertEqual(log.channel, "wechat")

    def test_fixed_supervision_falls_back_when_llm_is_unavailable(self):
        now = datetime(2026, 8, 9, 10, 0, tzinfo=BEIJING_TZ)
        self.user.morning_time = time(9, 0)
        self.db.commit()
        with (
            patch("services.scheduler_service.beijing_now", return_value=now),
            patch(
                "services.scheduler_service.generate_supervision",
                AsyncMock(side_effect=RuntimeError("provider unavailable")),
            ),
            patch("config.settings.WECHAT_SUBSCRIBE_TEMPLATE_ID", ""),
        ):
            results = asyncio.run(run_supervision_cycle(self.db))

        self.assertEqual(len(results), 1)
        self.assertIn("今天有 1 个任务", results[0]["message"])
        run = self.db.query(AgentRun).filter(
            AgentRun.user_id == self.user.id,
            AgentRun.agent_name == "supervisor",
        ).order_by(AgentRun.id.desc()).first()
        self.assertEqual(run.status, "fallback")
        self.assertEqual(run.stage, "reflect")
        self.assertTrue(json.loads(run.output_snapshot)["verification"]["passed"])

    def test_old_in_app_notice_is_never_backfilled_after_new_auth(self):
        now = datetime(2026, 8, 9, 21, 0, tzinfo=BEIJING_TZ)
        conversation = Conversation(
            user_id=self.user.id,
            date=get_effective_date(now),
            title="stale notification",
        )
        self.db.add(conversation)
        self.db.flush()
        message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="这是已经过时的站内提醒",
        )
        self.db.add(message)
        self.db.flush()
        log = SupervisionLog(
            user_id=self.user.id,
            supervision_type="morning_goal",
            task_date=get_effective_date(now),
            channel="in_app",
            message_id=message.id,
            sent_at=datetime.utcnow() - timedelta(minutes=10),
        )
        auth = SubscribeAuth(
            user_id=self.user.id,
            template_id="template-test",
            scene="morning_goal",
        )
        self.db.add_all([log, auth])
        self.db.commit()

        with (
            patch("config.settings.WECHAT_SUBSCRIBE_TEMPLATE_ID", "template-test"),
            patch(
                "services.wechat_service.wechat_service.send_subscribe_message",
                AsyncMock(return_value=True),
            ) as send,
        ):
            result = asyncio.run(_deliver_pending_wechat_notice(
                self.db,
                self.user,
                get_effective_date(now),
                now,
            ))

        self.db.refresh(auth)
        self.assertIsNone(result)
        self.assertFalse(auth.used)
        self.assertEqual(send.await_count, 0)


if __name__ == "__main__":
    unittest.main()
