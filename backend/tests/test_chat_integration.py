import asyncio
import unittest
from datetime import datetime, time
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import AgentRun, Conversation, DailyTask, Message, User
from routers.chat import chat_send
from schemas import ChatRequest
from services.scheduler_service import get_effective_date
from services.time_service import BEIJING_TZ


class ChatIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(openid="chat-integration-user")
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _fake_stream(output, captured):
        async def generator(messages, supervision_context=None, task_context=None, provider_callback=None):
            captured["messages"] = messages
            captured["task_context"] = task_context
            if provider_callback:
                provider_callback("bailian")
            yield output

        return generator

    def test_latest_database_checkin_overrides_old_chat_state(self):
        task = DailyTask(
            user_id=self.user.id,
            task_date=get_effective_date(),
            goal="休息前完成计划",
            content="背单词",
            is_completed=True,
        )
        self.db.add(task)
        self.db.commit()
        captured = {}
        with patch(
            "routers.chat.stream_chat",
            self._fake_stream("行，数据库显示已经做完了。", captured),
        ):
            result = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="我现在可以打游戏了吗",
                sync=True,
            ), self.db, self.user.openid))

        self.assertIn("✓ 背单词", captured["task_context"])
        self.assertIn("最高优先级事实", captured["task_context"])
        self.assertEqual(result["content"], "行，数据库显示已经做完了。")

    def test_definition_question_cannot_apply_model_task_directive(self):
        captured = {}
        model_output = (
            '私募基金是面向合格投资者募集的非公开基金。'
            '|||TASK_SPLIT:{"goal":"学习私募基金","tasks":["查定义","看案例"]}|||'
        )
        with patch(
            "routers.chat.stream_chat",
            self._fake_stream(model_output, captured),
        ):
            result = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="私募基金是什么",
                sync=True,
            ), self.db, self.user.openid))

        tasks = self.db.query(DailyTask).filter(DailyTask.user_id == self.user.id).all()
        self.assertEqual(tasks, [])
        self.assertNotIn("TASK_SPLIT", result["content"])
        self.assertEqual(result["task_changes"], [])

    def test_image_message_reaches_model_as_multimodal_content(self):
        captured = {}
        with patch(
            "routers.chat.stream_chat",
            self._fake_stream("图里是一道数学题。", captured),
        ):
            result = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="",
                image_url="https://example.com/question.jpg",
                image_cloud_id="cloud://env/question.jpg",
                sync=True,
            ), self.db, self.user.openid))

        current_message = captured["messages"][-1]
        self.assertIsInstance(current_message["content"], list)
        self.assertEqual(current_message["content"][1]["type"], "image_url")
        self.assertEqual(result["content"], "图里是一道数学题。")

    def test_model_cannot_edit_or_delete_without_explicit_user_intent(self):
        task = DailyTask(
            user_id=self.user.id,
            task_date=get_effective_date(),
            goal="学习",
            content="背单词",
            is_completed=False,
        )
        self.db.add(task)
        self.db.commit()
        output = (
            '普通回复。|||TASK_EDIT:{"from":"背单词","to":"打游戏"}|||'
            '|||TASK_DELETE:["背单词"]|||'
        )
        with patch(
            "routers.chat.stream_chat",
            self._fake_stream(output, {}),
        ):
            result = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="我今天心情不错",
                sync=True,
            ), self.db, self.user.openid))

        self.db.refresh(task)
        self.assertEqual(task.content, "背单词")
        self.assertEqual(result["task_changes"], [])
        self.assertNotIn("TASK_", result["content"])

    def test_confirmation_after_task_proposal_can_create_tasks(self):
        conversation = Conversation(
            user_id=self.user.id,
            date=get_effective_date(),
            title="学习计划",
        )
        self.db.add(conversation)
        self.db.flush()
        self.db.add(Message(
            conversation_id=conversation.id,
            role="assistant",
            content="要不要把背单词加成任务？",
        ))
        self.db.commit()
        output = '行，安排。|||TASK_SPLIT:{"goal":"背单词","tasks":["背20个单词"]}|||'
        with patch(
            "routers.chat.stream_chat",
            self._fake_stream(output, {}),
        ):
            result = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="可以",
                sync=True,
            ), self.db, self.user.openid))

        tasks = self.db.query(DailyTask).filter(DailyTask.user_id == self.user.id).all()
        self.assertEqual([task.content for task in tasks], ["背20个单词"])
        self.assertEqual(len(result["task_changes"]), 1)

    def test_protocol_only_model_output_never_persists_blank_assistant(self):
        output = '|||TASK_SPLIT:{"goal":"练习","tasks":["背20个单词"]}|||'
        with patch(
            "routers.chat.stream_chat",
            self._fake_stream(output, {}),
        ):
            result = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="帮我制定任务，今天背单词",
                sync=True,
            ), self.db, self.user.openid))

        assistant = self.db.query(Message).filter(
            Message.role == "assistant",
        ).order_by(Message.id.desc()).first()
        self.assertEqual(result["content"], "好，已经按你的要求更新任务了。")
        self.assertTrue(assistant.content.strip())

    def test_empty_model_output_gets_visible_fallback(self):
        with patch(
            "routers.chat.stream_chat",
            self._fake_stream("", {}),
        ):
            result = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="你好",
                sync=True,
            ), self.db, self.user.openid))

        self.assertEqual(result["content"], "刚才没有生成有效回复，你再说一次。")
        assistant = self.db.query(Message).filter(
            Message.role == "assistant",
        ).order_by(Message.id.desc()).first()
        self.assertTrue(assistant.content.strip())

    def test_explicit_reminder_is_persisted_without_calling_llm(self):
        with patch(
            "routers.chat.stream_chat",
            side_effect=AssertionError("reminder path must not call the LLM"),
        ):
            result = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="30分钟后提醒我背单词",
                request_id="chat-reminder-integration",
                sync=True,
            ), self.db, self.user.openid))

        self.assertIn("已设置", result["content"])
        self.assertEqual(result["reminder_change"]["action"], "set")

    def test_streaming_chat_commits_before_final_event_and_records_provider(self):
        with patch(
            "routers.chat.stream_chat",
            self._fake_stream("这是一段足够长的回复，用来确认流式接口会先发内容，再发送最终事件。", {}),
        ):
            response = asyncio.run(chat_send(ChatRequest(
                openid=self.user.openid,
                content="继续",
                sync=False,
            ), self.db, self.user.openid))

            async def collect():
                return "".join([chunk async for chunk in response.body_iterator])

            body = asyncio.run(collect())
        self.assertIn('"draft": true', body)
        self.assertIn('"committed": true', body)
        self.assertIn("这是一段足够长的回复", body)
        run = self.db.query(AgentRun).filter(
            AgentRun.user_id == self.user.id,
            AgentRun.agent_name == "coordinator",
        ).order_by(AgentRun.id.desc()).first()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.provider, "bailian")
        self.assertEqual(run.stage, "reflect")


if __name__ == "__main__":
    unittest.main()
