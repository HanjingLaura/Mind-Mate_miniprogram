import unittest
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import DailyTask, User
from services.execution_memory import build_execution_memory, format_execution_memory_context


class ExecutionMemoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(openid="memory-test-user")
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)
        self.today = date(2026, 7, 18)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_task(self, days_ago, content, completed, goal="准备考试"):
        target_date = self.today - timedelta(days=days_ago)
        task = DailyTask(
            user_id=self.user.id,
            task_date=target_date,
            goal=goal,
            content=content,
            is_completed=completed,
            completed_at=datetime(2026, 7, 17, 20, 0) if completed else None,
        )
        self.db.add(task)
        self.db.commit()

    def test_memory_keeps_yesterday_results_and_excludes_today_from_rate(self):
        self.add_task(1, "背单词", True)
        self.add_task(1, "做阅读", False)
        self.add_task(2, "听力训练", True)
        self.add_task(0, "今天的新任务", False)

        memory = build_execution_memory(self.db, self.user.id, self.today)

        self.assertEqual(memory["yesterday"]["completed_count"], 1)
        self.assertEqual(memory["yesterday"]["uncompleted_count"], 1)
        self.assertEqual(memory["yesterday"]["completion_rate"], 50)
        self.assertEqual(memory["recent"]["completed_count"], 2)
        self.assertEqual(memory["recent"]["total_count"], 3)
        self.assertEqual(memory["recent"]["completion_rate"], 67)
        self.assertNotIn("今天的新任务", str(memory))

    def test_streak_requires_consecutive_recorded_days_at_eighty_percent(self):
        for days_ago in (1, 2, 3):
            for index in range(5):
                self.add_task(days_ago, f"任务{days_ago}-{index}", index < 4)

        memory = build_execution_memory(self.db, self.user.id, self.today)
        self.assertEqual(memory["recent"]["qualified_streak_days"], 3)

    def test_context_marks_missing_days_as_unknown_not_failure(self):
        memory = build_execution_memory(self.db, self.user.id, self.today)
        context = format_execution_memory_context(memory)

        self.assertIn("没有任务记录", context)
        self.assertIn("禁止脑补", context)

    def test_repeated_unfinished_tasks_become_behavior_signal_not_label(self):
        self.add_task(1, "整理错题", False)
        self.add_task(4, "整理错题", False)

        memory = build_execution_memory(self.db, self.user.id, self.today)
        context = format_execution_memory_context(memory)

        self.assertEqual(
            memory["behavior_signals"]["recurring_uncompleted"],
            [{"content": "整理错题", "count": 2}],
        )
        self.assertIn("不是人格判断", context)


if __name__ == "__main__":
    unittest.main()
