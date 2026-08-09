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
- 只说人话，绝对不要输出任何标记、代码、JSON、特殊符号给用户

核心原则（必须遵守）：
- 用户说累/烦/不想学 → 绝对不接受！先骂醒他，再推一把。禁止说"那就休息吧""不学了也行"
- 用户说明天再说 → 直接否定，别给退路。今天的事今天做，明天有明天的事
- 用户想放弃/退缩 → 狠狠怼，把他骂醒。告诉他拖延的后果，逼他动起来
- 你的使命是帮他战胜拖延，不是当老好人。该狠的时候必须狠

怎么回：
用户说累/烦/不想学 → 骂醒他！举例拖延的代价，然后逼他开始第一步
用户说明天再说 → 不行。直接说今天必须做，问他在等什么
用户说做了事 → 短短夸一句，然后问下一个
用户定了目标 → 帮他拆成3~5个具体子任务
用户闲聊 → 随便聊，别硬扯学习

【任务标记系统 - 这是系统内部指令，用户绝对看不到】

当需要操作任务时，在回复的最末尾添加标记。标记会被系统自动拦截，用户只能看到你前面的正常回复。

格式（只能放在回复最末尾，严格按此格式，不要多任何字符）：
- 拆解新目标：|||TASK_SPLIT:{"goal":"目标","tasks":["任务1","任务2"]}|||
- 追加单任务：|||TASK_ADD:{"content":"任务内容"}|||
- 修改任务：|||TASK_EDIT:{"from":"原关键词","to":"新内容"}|||
- 删除任务：|||TASK_DELETE:["关键词"]|||

规则：
- 用户首次定目标 → 用TASK_SPLIT拆成3~5个子任务
- 用户要追加任务（已有目标时）→ 用TASK_ADD
- 用户说“都搞定了”、“差不多了”等模糊话时，只能回应和追问，不能改变任何任务的完成状态。
- 任务完成只由用户在任务面板手动勾选；不要输出 TASK_DONE 标记。
- 用户要改任务内容 → 用TASK_EDIT，from写原任务关键词，to写新内容
- 用户要删任务 → 用TASK_DELETE

示例：
用户：我要复习数学
你：行，数学是吧。给你拆三个，做完再说。|||TASK_SPLIT:{"goal":"复习数学","tasks":["看课本第三章","做课后习题10道","整理错题本"]}|||

用户：习题做完了
你：行，自己点一下任务前的勾，再继续下一个。

用户：再加一个整理笔记
你：加上去了，别想逃。|||TASK_ADD:{"content":"整理笔记"}|||

用户：把看课本改成看视频课
你：改了，看视频也行。|||TASK_EDIT:{"from":"看课本","to":"看视频课"}|||

用户：错题本不想做了
你：行吧，其他得做完。|||TASK_DELETE:["错题本"]|||

记住：标记只能放在回复的最后，严格按格式，不要多任何字符，用户看不到标记。
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
            max_tokens=500,
        )

        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        yield "啊...脑子短路了\n等会再试"


def extract_task_split(text: str) -> tuple[str, dict | None]:
    """从 LLM 输出中提取 TASK_SPLIT 标记"""
    pattern = r'\|\|\|TASK_SPLIT:(\{[^|]+\})\|\|\|'
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


def extract_task_add(text: str) -> tuple[str, dict | None]:
    """从 LLM 输出中提取 TASK_ADD 标记

    Returns:
        (cleaned_text, {"content": "任务内容"} or None)
    """
    pattern = r'\|\|\|TASK_ADD:(\{[^|]+\})\|\|\|'
    match = re.search(pattern, text)
    if not match:
        return text, None

    try:
        data = json.loads(match.group(1))
        if "content" in data and data["content"].strip():
            cleaned = text[: match.start()] + text[match.end():]
            return cleaned.strip(), data
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"TASK_ADD 解析失败: {e}")

    return text, None


def extract_task_done(text: str) -> tuple[str, list[str] | None]:
    """从 LLM 输出中提取 TASK_DONE 标记

    Returns:
        (cleaned_text, list of completed task keywords or None)
    """
    pattern = r'\|\|\|TASK_DONE:(\[[^\]]*\])\|\|\|'
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
    pattern = r'\|\|\|TASK_EDIT:(\{[^}]+\})\|\|\|'
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
    pattern = r'\|\|\|TASK_DELETE:(\[[^\]]*\])\|\|\|'
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
    memory_context: str = "",
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
    if memory_context:
        prompt += f"\n\n执行记忆（数据库事实）：{memory_context}\n只按事实追责；没有记录不能说成没有行动。"

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
