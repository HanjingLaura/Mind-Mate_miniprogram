"""三段式主动监督调度器

扩展方向：
- 增加 WebSocket 推送催促消息
- 增加催促频率可配置
- 增加用户免打扰时段
"""

import logging
from datetime import datetime, date, timedelta

from sqlalchemy.orm import Session

from models import User, DailyTask
from services.llm_service import generate_supervision

logger = logging.getLogger(__name__)

DAY_RESET_HOUR = 4  # 跨天结算边界：凌晨 4:00


def get_effective_date(now: datetime | None = None) -> date:
    """获取有效日期 — 凌晨 00:00~04:00 仍算前一天

    This ensures students who study late into the night
    are still working on "today's" tasks until 4 AM.
    """
    if now is None:
        now = datetime.now()
    if now.hour < DAY_RESET_HOUR:
        return (now - timedelta(days=1)).date()
    return now.date()


def should_supervise(user: User, supervision_type: str, now: datetime | None = None) -> bool:
    """判断是否该对用户发送监督催促

    Args:
        user: 用户对象
        supervision_type: "morning" / "afternoon" / "evening"
        now: 当前时间

    Returns:
        是否需要催促
    """
    if now is None:
        now = datetime.now()

    # 下午和晚间监督仅对 VIP 用户生效
    if supervision_type in ("afternoon", "evening") and not user.is_vip:
        return False

    current_time = now.time()
    target_time_map = {
        "morning": user.morning_time,
        "afternoon": user.afternoon_time,
        "evening": user.evening_time,
    }

    target_time = target_time_map.get(supervision_type)
    if target_time is None:
        return False

    # 在目标时间的 ±5 分钟窗口内触发
    from datetime import timedelta as td
    time_diff = abs(
        (current_time.hour * 60 + current_time.minute)
        - (target_time.hour * 60 + target_time.minute)
    )
    return time_diff <= 5


def get_user_task_summary(db: Session, user_id: int, task_date: date) -> dict:
    """获取用户某日的任务摘要"""
    tasks = db.query(DailyTask).filter(
        DailyTask.user_id == user_id,
        DailyTask.task_date == task_date,
    ).all()

    completed = [t.content for t in tasks if t.is_completed]
    uncompleted = [t.content for t in tasks if not t.is_completed]
    goal = tasks[0].goal if tasks else ""

    return {
        "goal": goal,
        "completed_tasks": completed,
        "uncompleted_tasks": uncompleted,
    }


def finalize_previous_day_tasks(db: Session, now: datetime | None = None) -> int:
    """固化前一日未完成的任务为"未完成"状态

    在跨天边界（04:00）之后调用，将旧日未完成任务标记为明确未完成。
    Returns:
        受影响的任务数
    """
    if now is None:
        now = datetime.now()

    yesterday = get_effective_date(now - timedelta(days=1)) if now.hour >= DAY_RESET_HOUR else get_effective_date(now)

    tasks = db.query(DailyTask).filter(
        DailyTask.task_date < get_effective_date(now),
        DailyTask.is_completed == False,
    ).all()

    # 这些任务保持 is_completed=False，已经自动标记为未完成
    # 返回数量作为晨间翻旧账的素材
    return len(tasks)


async def run_supervision_cycle(db: Session) -> list[dict]:
    """运行一轮监督检查 — 由定时任务调用

    Returns:
        需要催促的用户列表 [{"user_id": int, "type": str, "message": str}]
    """
    now = datetime.now()
    effective_date = get_effective_date(now)
    results = []

    users = db.query(User).all()

    for user in users:
        for supervision_type in ["morning", "afternoon", "evening"]:
            if not should_supervise(user, supervision_type, now):
                continue

            summary = get_user_task_summary(db, user.id, effective_date)

            # 晨间还检查前一天未完成任务
            if supervision_type == "morning":
                yesterday = get_effective_date(now - timedelta(days=1))
                yesterday_summary = get_user_task_summary(db, user.id, yesterday)
                summary["uncompleted_tasks"].extend(yesterday_summary["uncompleted_tasks"])

            try:
                message = await generate_supervision(
                    supervision_type=supervision_type,
                    goal=summary["goal"],
                    completed_tasks=summary["completed_tasks"],
                    uncompleted_tasks=summary["uncompleted_tasks"],
                )
                results.append({
                    "user_id": user.id,
                    "openid": user.openid,
                    "type": supervision_type,
                    "message": message,
                })
            except Exception as e:
                logger.error(f"用户 {user.id} 监督生成失败: {e}")

    return results
