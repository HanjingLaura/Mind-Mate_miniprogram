"""智谱 GLM-4-flash 大模型服务

扩展方向：
- 支持多模型切换（glm-4、glm-4-plus）
- 增加对话摘要功能
- 增加情绪分析中间件
"""

import json
import re
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from config import settings

logger = logging.getLogger(__name__)

# 心智同行基座人格 System Prompt
SYSTEM_PROMPT = """你是"心智同行"——一个有原则、不顺从的数字损友。你的核心使命是帮助大学生对抗拖延，在考证/考公/考研/期末周/Skill Learning过程中不再摆烂。

## 核心人格规则：
1. **绝不顺从借口**：当用户说"累了""明天再学""先打把游戏"时，你必须严厉但不失温度地拆穿合理化逃避。
2. **硬核情绪价值**：用犀利的语言配合具体行动方案，不是鸡汤而是当头棒喝。
3. **目标拆解**：当用户确定具体目标（考证/Skill学习）时，必须将大目标拆解为今日可执行的子任务。
4. **翻旧账**：如果用户有未完成的昨日任务，你必须在晨间对话中翻出来吐槽。

## 心理学反击策略：
- **晨间**：目标拆解 + 前日未完成翻旧账 + 行动启动
- **午后**：借口狙击 + 中场战报 + 紧迫感注入
- **晚间**：终局清算 + 今日成就复盘 + 明日预告

## 任务拆解格式规则：
当用户在对话中确定了具体的学习目标时，你必须在回复的末尾附带以下标记：
|||TASK_SPLIT:{"goal":"总目标名称","tasks":["子任务1","子任务2","子任务3"]}|||

注意：
- 只有当用户明确提出了学习目标或计划时才附带此标记
- 子任务必须是今日可执行的具体行动，不要模糊
- 日常闲聊不需要附带标记
"""

# 监督催促模板
SUPERVISION_PROMPTS = {
    "morning": """现在是晨间唤醒时间。请根据以下信息生成一段犀利的晨间催促：

用户目标：{goal}
昨日未完成任务：{uncompleted_tasks}
今日待办：{today_tasks}

要求：翻旧账吐槽昨日未完成 + 拆解今日目标 + 给出第一个30分钟行动指令。语气要犀利但有关怀。""",

    "afternoon": """现在是午后借口狙击时间。请根据以下信息生成一段午后催促：

用户目标：{goal}
今日已完成：{completed_tasks}
今日待完成：{pending_tasks}

要求：狙击可能的午饭后摆烂借口 + 注入紧迫感 + 给出下一个行动块。语气要像损友追债。""",

    "evening": """现在是晚间终局清算时间。请根据以下信息生成一段晚间清算：

用户目标：{goal}
今日已完成：{completed_tasks}
今日未完成：{uncompleted_tasks}

要求：今日成就复盘 + 对未完成的毒舌点评 + 明日预告。语气要像军事复盘，铁面无私。""",
}


def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.ZHIPU_API_KEY,
        base_url=settings.ZHIPU_BASE_URL,
    )


async def stream_chat(
    messages: list[dict[str, str]],
    supervision_context: str | None = None,
) -> AsyncGenerator[str, None]:
    """流式调用 GLM-4-flash，返回纯文本片段（已剔除 TASK_SPLIT 标记）

    Args:
        messages: 对话历史 [{"role": "user/assistant", "content": "..."}]
        supervision_context: 监督催促上下文（如有）

    Yields:
        纯文本片段
    """
    client = _get_client()

    system_content = SYSTEM_PROMPT
    if supervision_context:
        system_content += f"\n\n## 当前监督上下文：\n{supervision_context}"

    full_messages = [{"role": "system", "content": system_content}] + messages

    try:
        response = await client.chat.completions.create(
            model=settings.ZHIPU_MODEL,
            messages=full_messages,
            stream=True,
            temperature=0.85,
            max_tokens=1024,
        )

        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        yield "[系统提示] 大模型暂时走神了，请稍后再试..."


def extract_task_split(text: str) -> tuple[str, dict | None]:
    """从 LLM 输出中提取 TASK_SPLIT 标记

    Returns:
        (cleaned_text, task_data or None)
    """
    pattern = r'\|\|\|TASK_SPLIT:(\{.*?\})\|\|\|'
    match = re.search(pattern, text)
    if not match:
        return text, None

    task_json_str = match.group(1)
    try:
        task_data = json.loads(task_json_str)
        # 严格校验结构
        if "goal" in task_data and "tasks" in task_data and isinstance(task_data["tasks"], list):
            cleaned = text[: match.start()] + text[match.end():]
            return cleaned.strip(), task_data
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"TASK_SPLIT 解析失败: {e}")

    return text, None


async def generate_supervision(
    supervision_type: str,
    goal: str,
    completed_tasks: list[str],
    uncompleted_tasks: list[str],
) -> str:
    """生成监督催促文本

    Args:
        supervision_type: "morning" / "afternoon" / "evening"
        goal: 用户目标
        completed_tasks: 已完成任务列表
        uncompleted_tasks: 未完成任务列表

    Returns:
        催促文本
    """
    template = SUPERVISION_PROMPTS.get(supervision_type, SUPERVISION_PROMPTS["morning"])

    prompt = template.format(
        goal=goal or "暂无设定目标",
        completed_tasks="、".join(completed_tasks) if completed_tasks else "无",
        uncompleted_tasks="、".join(uncompleted_tasks) if uncompleted_tasks else "无",
        today_tasks="、".join(uncompleted_tasks) if uncompleted_tasks else "新的一天，设定目标吧",
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
            max_tokens=512,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"监督催促生成失败: {e}")
        return "嘿，该行动了！别让我催你。"
