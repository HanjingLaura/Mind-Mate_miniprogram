"""基于任务事实生成可解释的跨天执行记忆。"""

from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from models import DailyTask


RECENT_WINDOW_DAYS = 7
PATTERN_WINDOW_DAYS = 30
STREAK_THRESHOLD = 80


def _task_payload(task: DailyTask) -> dict:
    return {
        "id": task.id,
        "content": task.content,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _day_summary(target_date: date, tasks: list[DailyTask]) -> dict:
    completed = [task for task in tasks if task.is_completed]
    uncompleted = [task for task in tasks if not task.is_completed]
    total_count = len(tasks)
    completed_count = len(completed)
    completion_rate = round(completed_count / total_count * 100) if total_count else 0

    return {
        "date": target_date.isoformat(),
        "goal": tasks[0].goal if tasks else "",
        "total_count": total_count,
        "completed_count": completed_count,
        "uncompleted_count": len(uncompleted),
        "completion_rate": completion_rate,
        "completed_tasks": [_task_payload(task) for task in completed],
        "uncompleted_tasks": [_task_payload(task) for task in uncompleted],
    }


def empty_execution_memory(effective_date: date, window_days: int = RECENT_WINDOW_DAYS) -> dict:
    """返回稳定的空结构，方便前端直接渲染。"""
    yesterday = effective_date - timedelta(days=1)
    return {
        "effective_date": effective_date.isoformat(),
        "yesterday": _day_summary(yesterday, []),
        "recent": {
            "window_days": window_days,
            "active_days": 0,
            "total_count": 0,
            "completed_count": 0,
            "completion_rate": 0,
            "qualified_streak_days": 0,
            "recent_days": [],
        },
        "behavior_signals": {
            "recurring_uncompleted": [],
        },
    }


def build_execution_memory(
    db: Session,
    user_id: int,
    effective_date: date,
    window_days: int = RECENT_WINDOW_DAYS,
) -> dict:
    """从 DailyTask 派生昨日结果、近期表现和重复未完成模式。

    近期完成率只统计已经结束的日期，不把今天尚在进行的任务算成失败。
    """
    if window_days < 1:
        raise ValueError("window_days must be at least 1")

    yesterday = effective_date - timedelta(days=1)
    pattern_start = effective_date - timedelta(days=PATTERN_WINDOW_DAYS)
    recent_start = effective_date - timedelta(days=window_days)
    query_start = min(pattern_start, recent_start)
    tasks = db.query(DailyTask).filter(
        DailyTask.user_id == user_id,
        DailyTask.task_date >= query_start,
        DailyTask.task_date < effective_date,
    ).order_by(DailyTask.task_date.asc(), DailyTask.id.asc()).all()

    tasks_by_date: dict[date, list[DailyTask]] = defaultdict(list)
    for task in tasks:
        tasks_by_date[task.task_date].append(task)

    recent_days = []
    for offset in range(window_days):
        target_date = recent_start + timedelta(days=offset)
        day_tasks = tasks_by_date.get(target_date, [])
        if day_tasks:
            recent_days.append(_day_summary(target_date, day_tasks))

    recent_total = sum(day["total_count"] for day in recent_days)
    recent_completed = sum(day["completed_count"] for day in recent_days)
    recent_rate = round(recent_completed / recent_total * 100) if recent_total else 0

    qualified_streak = 0
    cursor = yesterday
    while True:
        day_tasks = tasks_by_date.get(cursor, [])
        if not day_tasks:
            break
        summary = _day_summary(cursor, day_tasks)
        if summary["completion_rate"] < STREAK_THRESHOLD:
            break
        qualified_streak += 1
        cursor -= timedelta(days=1)

    normalized_to_label: dict[str, str] = {}
    unfinished_counter: Counter[str] = Counter()
    for task in tasks:
        if task.is_completed:
            continue
        normalized = " ".join((task.content or "").strip().lower().split())
        if not normalized:
            continue
        normalized_to_label.setdefault(normalized, task.content.strip())
        unfinished_counter[normalized] += 1

    recurring = [
        {"content": normalized_to_label[key], "count": count}
        for key, count in unfinished_counter.most_common(3)
        if count >= 2
    ]

    return {
        "effective_date": effective_date.isoformat(),
        "yesterday": _day_summary(yesterday, tasks_by_date.get(yesterday, [])),
        "recent": {
            "window_days": window_days,
            "active_days": len(recent_days),
            "total_count": recent_total,
            "completed_count": recent_completed,
            "completion_rate": recent_rate,
            "qualified_streak_days": qualified_streak,
            "recent_days": recent_days,
        },
        "behavior_signals": {
            "recurring_uncompleted": recurring,
        },
    }


def format_execution_memory_context(memory: dict) -> str:
    """把结构化执行记忆压缩成受约束的模型上下文。"""
    yesterday = memory["yesterday"]
    recent = memory["recent"]
    signals = memory["behavior_signals"]

    lines = [
        "【用户执行记忆｜数据库行为事实】",
        "只能依据以下事实引用过去；没有记录不等于用户没做，禁止脑补、贴人格或心理标签。",
    ]

    if yesterday["total_count"]:
        completed = "、".join(task["content"] for task in yesterday["completed_tasks"]) or "无"
        uncompleted = "、".join(task["content"] for task in yesterday["uncompleted_tasks"]) or "无"
        lines.extend([
            f"昨日（{yesterday['date']}）：{yesterday['completed_count']}/{yesterday['total_count']}，完成率{yesterday['completion_rate']}%。",
            f"昨日完成：{completed}。",
            f"昨日未完成：{uncompleted}。",
        ])
    else:
        lines.append(f"昨日（{yesterday['date']}）：没有任务记录，不能据此判断用户是否拖延。")

    if recent["total_count"]:
        lines.append(
            f"近{recent['window_days']}天：有任务记录{recent['active_days']}天，"
            f"共完成{recent['completed_count']}/{recent['total_count']}，完成率{recent['completion_rate']}%；"
            f"截至昨日连续达标（单日完成率≥{STREAK_THRESHOLD}%）{recent['qualified_streak_days']}天。"
        )

    recurring = signals["recurring_uncompleted"]
    if recurring:
        patterns = "、".join(f"{item['content']}（{item['count']}次）" for item in recurring)
        lines.append(f"近30天重复未完成：{patterns}。这只是行为模式，不是人格判断。")

    lines.append("自然使用这些记忆：用户问到过去时准确回答；与当前话题无关时不要每次重复翻旧账。")
    return "\n".join(lines)


def format_supervision_memory(memory: dict) -> str:
    """为主动监督生成一句紧凑、可核验的历史摘要。"""
    yesterday = memory["yesterday"]
    recent = memory["recent"]
    parts = []
    if yesterday["total_count"]:
        parts.append(
            f"昨日完成{yesterday['completed_count']}/{yesterday['total_count']}（{yesterday['completion_rate']}%）"
        )
    else:
        parts.append("昨日无任务记录")
    if recent["total_count"]:
        parts.append(
            f"近{recent['window_days']}天完成{recent['completed_count']}/{recent['total_count']}（{recent['completion_rate']}%）"
        )
    if recent["qualified_streak_days"]:
        parts.append(f"连续达标{recent['qualified_streak_days']}天")
    return "；".join(parts)
