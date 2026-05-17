"""智谱 GLM 大模型服务 — OpenAI 兼容接口"""

import json
import re
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是他朋友，嘴毒但心软，专门帮大学生对抗拖延。

说话规矩：
- 1~3句话回复，别写小作文
- 像发微信语音，短、口语、有语气词
- 别寒暄别总结别重复，直说
- 禁止："我理解""让我们一起""加油""你可以的""相信自己"
- 可以怼，可以损，偶尔夸一句真的做得好的

怎么回：
用户说累/烦/不想学 → 先损一句，再推一把
用户说明天再说 → 直接否定，别给退路
用户说做了事 → 短短夸一句，然后问下一个
用户定了目标 → 帮他拆成3~5个具体子任务
用户闲聊 → 随便聊，别硬扯学习

关于任务：
- 用户提了自己的计划/待办 → 识别出来，沿用他的任务
- 用户没计划 → 帮他定目标，拆成具体的小任务
- 用户已有今日任务时 → 别重复拆，聊进度就好
- 用户要拆任务或定了新目标时，回复最后偷偷带标记（用户看不到）：
|||TASK_SPLIT:{"goal":"总目标","tasks":["子任务1","子任务2","子任务3"]}|||
只有需要拆任务时才带，其他时候不带。
- 用户说完成了某个任务时，判断他是否真的完成了，如果确实完成了，回复最后偷偷带标记：
|||TASK_DONE:["任务内容关键词1","任务内容关键词2"]|||
关键词要能匹配到任务列表里的内容，只带真正完成的，不确定的别带。
- 用户想修改某个任务内容时，带标记：
|||TASK_EDIT:{"from":"原任务关键词","to":"新内容"}|||
- 用户想删除某个任务时，带标记：
|||TASK_DELETE:["要删除的任务关键词"]|||
"""

SUPERVISION_PROMPTS = {
    "morning": """用户目标：{goal}
昨天没做完：{uncompleted_tasks}
今天要做：{today_tasks}

早上翻旧账催他。2~3句话，短，像发微信。""",

    "afternoon": """用户目标：{goal}
做完了：{completed_tasks}
还没做：{pending_tasks}

下午狙击借口，逼他动。2~3句话。""",

    "evening": """用户目标：{goal}
做完了：{completed_tasks}
没做完：{uncompleted_tasks}

晚上清算。做完了简短夸，没做完损一句+说明天继续。2~3句话。""",
}


def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.ZHIPU_API_KEY,
        base_url=settings.ZHIPU_BASE_URL,
        timeout=60.0,
        max_retries=2,
    )


async def stream_chat(
    messages: list[dict[str, str]],
    supervision_context: str | None = None,
    task_context: str | None = None,
) -> AsyncGenerator[str, None]:
    """流式调用 GLM，返回纯文本片段"""
    client = _get_client()

    system_content = SYSTEM_PROMPT
    if task_context:
        system_content += f"\n\n{task_context}"
    if supervision_context:
        system_content += f"\n\n{supervision_context}"

    full_messages = [{"role": "system", "content": system_content}] + messages

    try:
        response = await client.chat.completions.create(
            model=settings.ZHIPU_MODEL,
            messages=full_messages,
            stream=True,
            temperature=0.9,
            max_tokens=300,
        )

        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        yield "啊...脑子短路了\n等会再试"


def extract_task_split(text: str) -> tuple[str, dict | None]:
    """从 LLM 输出中提取 TASK_SPLIT 标记"""
    pattern = r'\|\|\|TASK_SPLIT:(\{.*?\})\|\|\|'
    match = re.search(pattern, text)
    if not match:
        return text, None

    task_json_str = match.group(1)
    try:
        task_data = json.loads(task_json_str)
        if "goal" in task_data and "tasks" in task_data and isinstance(task_data["tasks"], list):
            cleaned = text[: match.start()] + text[match.end():]
            return cleaned.strip(), task_data
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"TASK_SPLIT 解析失败: {e}")

    return text, None


def extract_task_done(text: str) -> tuple[str, list[str] | None]:
    """从 LLM 输出中提取 TASK_DONE 标记

    Returns:
        (cleaned_text, list of completed task keywords or None)
    """
    pattern = r'\|\|\|TASK_DONE:(\[.*?\])\|\|\|'
    match = re.search(pattern, text)
    if not match:
        return text, None

    try:
        keywords = json.loads(match.group(1))
        if isinstance(keywords, list) and len(keywords) > 0:
            cleaned = text[: match.start()] + text[match.end():]
            return cleaned.strip(), keywords
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"TASK_DONE 解析失败: {e}")

    return text, None


def extract_task_edit(text: str) -> tuple[str, dict | None]:
    """从 LLM 输出中提取 TASK_EDIT 标记

    Returns:
        (cleaned_text, {"from": "原关键词", "to": "新内容"} or None)
    """
    pattern = r'\|\|\|TASK_EDIT:(\{.*?\})\|\|\|'
    match = re.search(pattern, text)
    if not match:
        return text, None

    try:
        data = json.loads(match.group(1))
        if "from" in data and "to" in data:
            cleaned = text[: match.start()] + text[match.end():]
            return cleaned.strip(), data
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"TASK_EDIT 解析失败: {e}")

    return text, None


def extract_task_delete(text: str) -> tuple[str, list[str] | None]:
    """从 LLM 输出中提取 TASK_DELETE 标记

    Returns:
        (cleaned_text, list of task keywords to delete or None)
    """
    pattern = r'\|\|\|TASK_DELETE:(\[.*?\])\|\|\|'
    match = re.search(pattern, text)
    if not match:
        return text, None

    try:
        keywords = json.loads(match.group(1))
        if isinstance(keywords, list) and len(keywords) > 0:
            cleaned = text[: match.start()] + text[match.end():]
            return cleaned.strip(), keywords
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"TASK_DELETE 解析失败: {e}")

    return text, None


async def generate_supervision(
    supervision_type: str,
    goal: str,
    completed_tasks: list[str],
    uncompleted_tasks: list[str],
) -> str:
    """生成监督催促文本"""
    template = SUPERVISION_PROMPTS.get(supervision_type, SUPERVISION_PROMPTS["morning"])

    prompt = template.format(
        goal=goal or "还没定目标",
        completed_tasks="、".join(completed_tasks) if completed_tasks else "无",
        uncompleted_tasks="、".join(uncompleted_tasks) if uncompleted_tasks else "无",
        today_tasks="、".join(uncompleted_tasks) if uncompleted_tasks else "新的一天",
        pending_tasks="、".join(uncompleted_tasks) if uncompleted_tasks else "无",
    )

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=settings.ZHIPU_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            stream=False,
            temperature=0.9,
            max_tokens=150,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"监督催促生成失败: {e}")
        return "喂\n该干活了"
