import unittest

from routers.chat import (
    _allows_task_add,
    _allows_task_creation,
    _allows_task_delete,
    _allows_task_edit,
)
from services.message_content import (
    decode_message_content,
    encode_message_content,
    strip_task_protocol,
)


class ChatSafetyTests(unittest.TestCase):
    def test_definition_question_never_creates_tasks(self):
        self.assertFalse(_allows_task_creation("私募基金"))
        self.assertFalse(_allows_task_creation("私募基金是什么，解释一下"))
        self.assertFalse(_allows_task_creation("我想了解一下私募基金"))
        self.assertFalse(_allows_task_creation("我今天要打游戏"))

    def test_explicit_execution_intent_can_create_tasks(self):
        self.assertTrue(_allows_task_creation("我今天要背单词"))
        self.assertTrue(_allows_task_creation("明天我要复习高数"))
        self.assertTrue(_allows_task_creation("帮我拆成任务，今晚复习高数"))
        self.assertTrue(_allows_task_add("再加一个整理错题"))
        self.assertFalse(_allows_task_add("加吧"))
        proposal = "要不要把背单词加成任务？"
        self.assertTrue(_allows_task_add("加吧", proposal))
        self.assertTrue(_allows_task_creation("可以", proposal))
        self.assertFalse(_allows_task_creation("可以", "你觉得这个解释清楚吗？"))
        self.assertTrue(_allows_task_add("我今天还要跑步"))
        self.assertFalse(_allows_task_creation("不要加到任务"))
        self.assertFalse(_allows_task_add("先别加任务"))

    def test_edit_and_delete_require_explicit_user_intent(self):
        self.assertTrue(_allows_task_edit("把背单词改成背30个单词"))
        self.assertFalse(_allows_task_edit("背单词挺好的"))
        self.assertTrue(_allows_task_delete("删掉背单词这个任务"))
        self.assertTrue(_allows_task_delete("错题本不想做了"))
        self.assertFalse(_allows_task_delete("给我解释一下错题本"))

    def test_internal_task_protocol_is_never_user_visible(self):
        value = '行，给你拆好了。|||TASK_SPLIT:{"goal":"背词","tasks":["背20个"]}|||'
        self.assertEqual(strip_task_protocol(value), "行，给你拆好了。")
        malformed = '正常回复|||TASK_ADD:{"content":"背词"}'
        self.assertEqual(strip_task_protocol(malformed), "正常回复")
        no_pipes = '自然语言回复 TASK_SPLIT:{"goal":"背词","tasks":["背20个"]}'
        self.assertEqual(strip_task_protocol(no_pipes), "自然语言回复")
        one_pipe = '自然语言回复|TASK_DELETE:["背词"]|'
        self.assertEqual(strip_task_protocol(one_pipe), "自然语言回复")

    def test_image_message_round_trip_uses_existing_text_column(self):
        encoded = encode_message_content(
            "这题怎么做",
            "https://example.com/question.jpg",
            "cloud://env.abc/question.jpg",
        )
        decoded = decode_message_content(encoded)
        self.assertEqual(decoded["type"], "image")
        self.assertEqual(decoded["text"], "这题怎么做")
        self.assertEqual(decoded["image_cloud_id"], "cloud://env.abc/question.jpg")


if __name__ == "__main__":
    unittest.main()
