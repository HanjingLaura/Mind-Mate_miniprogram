import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import (
    Conversation,
    Message,
    ScheduledReminder,
    SubscribeAuth,
    User,
)
from services.reminder_service import (
    create_scheduled_reminder,
    parse_reminder_request,
)
from services.scheduler_service import _deliver_due_scheduled_reminders
from services.time_service import BEIJING_TZ, beijing_to_utc_naive, utc_naive_to_beijing


class ScheduledReminderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(openid="scheduled-reminder-user")
        self.db.add(self.user)
        self.db.flush()
        self.conversation = Conversation(
            user_id=self.user.id,
            date=datetime(2026, 8, 16).date(),
            title="提醒测试",
        )
        self.db.add(self.conversation)
        self.db.flush()
        self.source = Message(
            conversation_id=self.conversation.id,
            role="user",
            content="16:02提醒我练阅读",
        )
        self.db.add(self.source)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _create_due(self, now, request_id="reminder-request-1"):
        return create_scheduled_reminder(
            self.db,
            user_id=self.user.id,
            source_message_id=self.source.id,
            idempotency_key=request_id,
            content="练阅读",
            scheduled_at=beijing_to_utc_naive(now - timedelta(minutes=1)),
        )

    def test_absolute_and_relative_times_use_beijing_calendar(self):
        now = datetime(2026, 8, 16, 15, 55, tzinfo=BEIJING_TZ)
        absolute = parse_reminder_request("今天16点02提醒我练阅读", now)
        relative = parse_reminder_request("半小时后提醒我开会", now)

        self.assertTrue(absolute.is_intent)
        self.assertEqual(
            utc_naive_to_beijing(absolute.scheduled_at),
            datetime(2026, 8, 16, 16, 2, tzinfo=BEIJING_TZ),
        )
        self.assertEqual(absolute.content, "练阅读")
        self.assertEqual(
            utc_naive_to_beijing(relative.scheduled_at),
            datetime(2026, 8, 16, 16, 25, tzinfo=BEIJING_TZ),
        )

    def test_past_explicit_today_is_rejected_and_complaint_is_not_creation(self):
        now = datetime(2026, 8, 16, 16, 3, tzinfo=BEIJING_TZ)
        past = parse_reminder_request("今天16:02提醒我练阅读", now)
        complaint = parse_reminder_request("你为什么没提醒我", now)

        self.assertTrue(past.is_intent)
        self.assertEqual(past.error, "past_time")
        self.assertIsNone(past.scheduled_at)
        self.assertFalse(complaint.is_intent)

    def test_midnight_period_is_never_parsed_as_noon(self):
        now = datetime(2026, 8, 16, 20, 0, tzinfo=BEIJING_TZ)

        tonight = parse_reminder_request("今晚12点提醒我睡觉", now)
        tomorrow_night = parse_reminder_request("明天晚上12点提醒我交作业", now)

        self.assertEqual(
            utc_naive_to_beijing(tonight.scheduled_at),
            datetime(2026, 8, 17, 0, 0, tzinfo=BEIJING_TZ),
        )
        self.assertEqual(
            utc_naive_to_beijing(tomorrow_night.scheduled_at),
            datetime(2026, 8, 18, 0, 0, tzinfo=BEIJING_TZ),
        )

    def test_past_promise_questions_do_not_create_new_reminders(self):
        now = datetime(2026, 8, 16, 20, 0, tzinfo=BEIJING_TZ)

        for text in (
            "你不是说16点提醒我吗",
            "你说过16点要提醒我的",
            "16点02你提醒我了吗",
            "你是不是16点02提醒我了",
            "你16点有提醒我吗",
            "你16点到底提醒我没有",
            "你16点提醒我没有",
        ):
            with self.subTest(text=text):
                result = parse_reminder_request(text, now)
                self.assertFalse(result.is_intent)
                self.assertIsNone(result.scheduled_at)

    def test_negated_or_multiple_time_request_never_creates_a_reminder(self):
        now = datetime(2026, 8, 16, 16, 3, tzinfo=BEIJING_TZ)
        negated = parse_reminder_request("不要明天16:02提醒我开会", now)
        multiple = parse_reminder_request("17点提醒我开会，18点提醒我吃饭", now)

        self.assertEqual(negated.error, "negated")
        self.assertIsNone(negated.scheduled_at)
        self.assertEqual(multiple.error, "multiple_times")
        self.assertIsNone(multiple.scheduled_at)

    def test_same_request_id_creates_only_one_row(self):
        now = datetime(2026, 8, 16, 16, 3, tzinfo=BEIJING_TZ)
        first = self._create_due(now)
        second = create_scheduled_reminder(
            self.db,
            user_id=self.user.id,
            source_message_id=self.source.id,
            idempotency_key="reminder-request-1",
            content="重复内容",
            scheduled_at=beijing_to_utc_naive(now + timedelta(hours=1)),
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(ScheduledReminder).count(), 1)

    def test_same_source_message_with_new_request_id_recovers_existing_row(self):
        now = datetime(2026, 8, 16, 16, 3, tzinfo=BEIJING_TZ)
        first = self._create_due(now)
        second = create_scheduled_reminder(
            self.db,
            user_id=self.user.id,
            source_message_id=self.source.id,
            idempotency_key="client-retried-with-a-new-id",
            content="不应重复",
            scheduled_at=beijing_to_utc_naive(now + timedelta(hours=1)),
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(ScheduledReminder).count(), 1)

    def test_due_reminder_always_creates_in_app_message_without_auth(self):
        now = datetime(2026, 8, 16, 16, 3, tzinfo=BEIJING_TZ)
        reminder = self._create_due(now)
        with patch("config.settings.WECHAT_SUBSCRIBE_TEMPLATE_ID", "template-test"):
            results = asyncio.run(_deliver_due_scheduled_reminders(self.db, now))

        self.db.refresh(reminder)
        self.assertEqual(reminder.status, "sent")
        self.assertEqual(reminder.channel, "in_app")
        message = self.db.query(Message).filter(Message.id == reminder.message_id).one()
        self.assertIn("到时间了：练阅读", message.content)
        self.assertEqual(results[0]["channel"], "in_app")

    def test_wechat_attempt_claims_one_auth_and_marks_channel(self):
        now = datetime(2026, 8, 16, 16, 3, tzinfo=BEIJING_TZ)
        reminder = self._create_due(now)
        auth = SubscribeAuth(
            user_id=self.user.id,
            template_id="template-test",
            scene="scheduled_reminder",
        )
        self.db.add(auth)
        self.db.commit()

        with (
            patch("config.settings.WECHAT_SUBSCRIBE_TEMPLATE_ID", "template-test"),
            patch(
                "services.wechat_service.wechat_service.send_subscribe_message",
                AsyncMock(return_value=True),
            ) as send,
        ):
            results = asyncio.run(_deliver_due_scheduled_reminders(self.db, now))

        self.db.refresh(reminder)
        self.db.refresh(auth)
        self.assertTrue(auth.used)
        self.assertEqual(reminder.channel, "wechat")
        self.assertEqual(send.await_count, 1)
        self.assertEqual(results[0]["channel"], "wechat")

    def test_failed_wechat_attempt_is_not_reused_or_counted(self):
        now = datetime(2026, 8, 16, 16, 3, tzinfo=BEIJING_TZ)
        reminder = self._create_due(now)
        auth = SubscribeAuth(user_id=self.user.id, template_id="template-test")
        self.db.add(auth)
        self.db.commit()

        with (
            patch("config.settings.WECHAT_SUBSCRIBE_TEMPLATE_ID", "template-test"),
            patch(
                "services.wechat_service.wechat_service.send_subscribe_message",
                AsyncMock(return_value=False),
            ) as send,
        ):
            asyncio.run(_deliver_due_scheduled_reminders(self.db, now))
            asyncio.run(_deliver_due_scheduled_reminders(self.db, now + timedelta(minutes=1)))

        self.db.refresh(reminder)
        self.db.refresh(auth)
        self.assertTrue(auth.used)
        self.assertEqual(reminder.channel, "wechat_failed")
        self.assertEqual(send.await_count, 1)


if __name__ == "__main__":
    unittest.main()
