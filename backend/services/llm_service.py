"""学习对话与提醒服务，通过模型网关调用百炼或备用供应商。"""

import json
import re
import logging
from typing import Any, AsyncGenerator, Callable

from services.message_content import strip_task_protocol
from services.model_gateway import complete, stream_completion

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个说话直接、可靠的学习搭子，帮大学生把目标变成能完成的小步行动。

说话规矩：
- 1~3句话回复，别写小作文
- 像发微信语音，短、口语、有语气词
- 别寒暄别总结别重复，直说
- 禁止："我理解""让我们一起""加油""你可以的""相信自己"
- 可以轻松吐槽具体借口，但不羞辱、不攻击人格；只夸确实发生的进展
- 只说人话，绝对不要输出任何标记、代码、JSON、特殊符号给用户

核心原则（必须遵守）：
- 用户说累/烦/不想学 → 不把疲劳当借口，先区分身体疲劳和任务困难；允许休息，也可以协商一个两分钟的小步
- 用户说明天再说 → 说明延期的实际影响，帮助调整计划，不强迫承诺，不制造内疚
- 用户想放弃/退缩 → 问清卡点，允许缩小、改期或放弃不再适合的目标；最终选择由用户决定
- 原则是帮助用户实现自己的目标，不是处处反对；尊重明确的停止、休息和不提醒请求

怎么回：
用户说累/烦/不想学 → 简短确认状态，给休息或缩小任务的具体选择，不说教
用户说明天再说 → 帮他明确下一次开始时间和最小一步，不自动更改任务
用户说做了事 → 短短夸一句，然后问下一个
用户明确要执行一个目标，或明确说“帮我拆任务/加任务” → 才能帮他拆任务
用户只是问定义、解释概念、发专有名词或闲聊 → 只回答问题，禁止自动建任务；可以在回答末尾自然问一句“要不要把它加成任务？”
用户闲聊 → 随便聊，别硬扯学习

【任务标记系统 - 这是系统内部指令，用户绝对看不到】

当需要操作任务时，在回复的最末尾添加标记。标记会被系统自动拦截，用户只能看到你前面的正常回复。

格式（只能放在回复最末尾，严格按此格式，不要多任何字符）：
- 拆解新目标：|||TASK_SPLIT:{"goal":"目标","tasks":["任务1","任务2"]}|||
- 追加单任务：|||TASK_ADD:{"content":"任务内容"}|||
- 修改任务：|||TASK_EDIT:{"from":"原关键词","to":"新内容"}|||
- 删除任务：|||TASK_DELETE:["关键词"]|||

规则：
- 只有用户明确表达要做、要完成、要安排，或明确确认添加任务时，才可用 TASK_SPLIT
- “是什么/什么意思/解释一下/介绍一下/了解一下/区别是什么”等知识问答，即使出现专有名词，也绝对不能建任务
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
你：把整理笔记也加上。|||TASK_ADD:{"content":"整理笔记"}|||

用户：把看课本改成看视频课
你：改了，看视频也行。|||TASK_EDIT:{"from":"看课本","to":"看视频课"}|||

用户：错题本不想做了
你：那先去掉错题本，保留你现在愿意做的部分。|||TASK_DELETE:["错题本"]|||

记住：标记只能放在回复的最后，严格按格式，不要多任何字符，用户看不到标记。
"""

SUPERVISION_PROMPTS = {
    "morning": """用户目标：{goal}
昨天没做完：{uncompleted_tasks}
今天要做：{today_tasks}

早上确认今天最重要的一步。昨日未完成只作计划参考，不责备。2~3句话。""",

    "afternoon": """用户目标：{goal}
做完了：{completed_tasks}
还没做：{pending_tasks}

下午根据已完成和待办事实，建议一个容易开始的小步。允许用户调整节奏。2~3句话。""",

    "evening": """用户目标：{goal}
做完了：{completed_tasks}
没做完：{uncompleted_tasks}

晚上简短复盘：承认已完成的进展，未完成的询问卡点或建议明天如何缩小。不要羞辱或制造内疚。2~3句话。""",
}


async def stream_chat(
    messages: list[dict[str, Any]],
    supervision_context: str | None = None,
    task_context: str | None = None,
    provider_callback: Callable[[str], None] | None = None,
) -> AsyncGenerator[str, None]:
    """流式调用模型网关，返回纯文本片段。"""
    system_content = SYSTEM_PROMPT
    if task_context:
        system_content += f"\n\n{task_context}"
    if supervision_context:
        system_content += f"\n\n{supervision_context}"

    full_messages = [{"role": "system", "content": system_content}] + messages
    has_image = any(isinstance(message.get("content"), list) for message in messages)
    try:
        async for chunk in stream_completion(
            full_messages,
            has_image=has_image,
            temperature=0.9,
            max_tokens=500,
            provider_callback=provider_callback,
        ):
            yield chunk

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        raise


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
    with_provider: bool = False,
) -> str | tuple[str, str]:
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

    try:
        content, provider = await complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            max_tokens=150,
        )
        cleaned = strip_task_protocol(content)
        result = cleaned or "喂，该干活了。"
        return (result, provider) if with_provider else result
    except Exception as e:
        logger.error(f"监督催促生成失败: {e}")
        raise
