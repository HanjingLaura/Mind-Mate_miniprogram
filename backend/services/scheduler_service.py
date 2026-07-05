"""三段式主动监督调度器

扩展方向：
- 增加 WebSocket 推送催促消息
- 增加催促频率可配置
- 增加用户免打扰时段
"""

import json
import logging
from datetime import datetime, date, timedelta

from sqlalchemy.orm import Session

from models import User, DailyTask, Conversation, Message, SubscribeAuth, SupervisionLog, AnalyticsEvent
from services.llm_service import generate_supervision
from services.user_state import normalize_vip_status

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


def _minutes(value) -> int:
    return value.hour * 60 + value.minute


def _has_supervision_log(db: Session, user_id: int, task_date: date, supervision_type: str) -> bool:
    return db.query(SupervisionLog).filter(
        SupervisionLog.user_id == user_id,
        SupervisionLog.task_date == task_date,
        SupervisionLog.supervision_type == supervision_type,
    ).first() is not None


def _record_event(db: Session, user: User, event_name: str, properties: dict):
    event = AnalyticsEvent(
        user_id=user.id,
        openid=user.openid,
        event_name=event_name,
        properties=json.dumps(properties, ensure_ascii=False),
    )
    db.add(event)
    db.commit()


def _has_uncompleted_tasks(db: Session, user_id: int, task_date: date) -> bool:
    return db.query(DailyTask).filter(
        DailyTask.user_id == user_id,
        DailyTask.task_date == task_date,
        DailyTask.is_completed == False,
    ).first() is not None


def _latest_task_activity(db: Session, user_id: int, task_date: date) -> datetime | None:
    tasks = db.query(DailyTask).filter(
        DailyTask.user_id == user_id,
        DailyTask.task_date == task_date,
    ).all()
    times = []
    for task in tasks:
        if task.created_at:
            times.append(task.created_at)
        if task.completed_at:
            times.append(task.completed_at)
    return max(times) if times else None


def _stable_afternoon_minute(user_id: int, task_date: date) -> int:
    """给每个用户每天一个稳定的 14:00~17:30 随机狙击点。"""
    seed = user_id * 97 + task_date.toordinal()
    return 14 * 60 + (seed % 210)


def get_due_supervision_types(db: Session, user: User, now: datetime | None = None) -> list[str]:
    """根据免费/VIP权益和任务状态，返回本轮应触发的监督类型。"""
    if now is None:
        now = datetime.now()

    user = normalize_vip_status(db, user)
    task_date = get_effective_date(now)
    now_minutes = now.hour * 60 + now.minute
    due: list[str] = []

    yesterday = task_date - timedelta(days=1)
    has_yesterday_unfinished = _has_uncompleted_tasks(db, user.id, yesterday)

    # 晨间：免费用户体验今日目标；VIP 用户如有昨日未完成则优先翻旧账。
    if 6 <= now.hour < 12 and now_minutes >= _minutes(user.morning_time):
        morning_type = "unfinished_followup" if user.is_vip and has_yesterday_unfinished else "morning_goal"
        if not _has_supervision_log(db, user.id, task_date, morning_type):
            due.append(morning_type)

    # 晚间：所有用户每天一次复盘。
    if 18 <= now.hour <= 23 and now_minutes >= _minutes(user.evening_time):
        if not _has_supervision_log(db, user.id, task_date, "evening_review"):
            due.append("evening_review")

    if not user.is_vip:
        return due

    # VIP：午后稳定随机狙击，只在还有未完成任务时触发。
    if _has_uncompleted_tasks(db, user.id, task_date):
        random_minute = _stable_afternoon_minute(user.id, task_date)
        if 13 <= now.hour < 18 and now_minutes >= random_minute:
            if not _has_supervision_log(db, user.id, task_date, "afternoon_nudge"):
                due.append("afternoon_nudge")

        # VIP：长时间无打卡追问。第一版保持每日最多一次，防止打扰过重。
        latest = _latest_task_activity(db, user.id, task_date)
        if latest and 9 <= now.hour < 22 and now - latest >= timedelta(hours=2):
            if not _has_supervision_log(db, user.id, task_date, "behavior_nudge"):
                due.append("behavior_nudge")

    return due


def should_supervise(user: User, supervision_type: str, now: datetime | None = None) -> bool:
    """兼容旧调用：新逻辑请使用 get_due_supervision_types。"""
    if now is None:
        now = datetime.now()
    current = now.hour * 60 + now.minute
    if supervision_type == "morning":
        return 6 <= now.hour < 12 and current >= _minutes(user.morning_time)
    if supervision_type == "evening":
        return 18 <= now.hour <= 23 and current >= _minutes(user.evening_time)
    if supervision_type == "afternoon":
        return user.is_vip and 13 <= now.hour < 18
    return False


def _to_prompt_type(supervision_type: str) -> str:
    if supervision_type in ("morning_goal", "unfinished_followup"):
        return "morning"
    if supervision_type == "evening_review":
        return "evening"
    return "afternoon"


def _scene_for_type(supervision_type: str) -> str:
    if supervision_type == "morning_goal":
        return "morning_goal"
    if supervision_type == "evening_review":
        return "evening_review"
    if supervision_type == "afternoon_nudge":
        return "afternoon_nudge"
    if supervision_type == "behavior_nudge":
        return "behavior_nudge"
    return "vip_followup"


def _consume_subscribe_auth(db: Session, user_id: int, template_id: str, scene: str) -> SubscribeAuth | None:
    auth = db.query(SubscribeAuth).filter(
        SubscribeAuth.user_id == user_id,
        SubscribeAuth.template_id == template_id,
        SubscribeAuth.used == False,
        SubscribeAuth.scene == scene,
    ).order_by(SubscribeAuth.auth_time.asc()).first()
    if auth:
        return auth

    return db.query(SubscribeAuth).filter(
        SubscribeAuth.user_id == user_id,
        SubscribeAuth.template_id == template_id,
        SubscribeAuth.used == False,
    ).order_by(SubscribeAuth.auth_time.asc()).first()


def _create_supervision_log(
    db: Session,
    user_id: int,
    supervision_type: str,
    task_date: date,
    channel: str,
    message_id: int | None,
):
    log = SupervisionLog(
        user_id=user_id,
        supervision_type=supervision_type,
        task_date=task_date,
        channel=channel,
        message_id=message_id,
    )
    db.add(log)
    db.commit()
    return log


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


def save_supervision_as_message(db: Session, user_id: int, content: str, effective_date: date) -> Message:
    """将监督消息存入对话记录，用户打开小程序时可见"""
    conv = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.date == effective_date,
    ).first()
    if not conv:
        conv = Conversation(user_id=user_id, date=effective_date, title=f"目标追踪 {effective_date}")
        db.add(conv)
        db.commit()
        db.refresh(conv)

    msg = Message(conversation_id=conv.id, role="assistant", content=content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


async def run_supervision_cycle(db: Session) -> list[dict]:
    """运行一轮监督检查 — 由定时任务调用

    对符合条件的用户：
    1. 生成监督消息并存入对话
    2. 尝试通过微信订阅消息推送（如有授权）
    """
    from config import settings
    from services.wechat_service import wechat_service

    now = datetime.now()
    effective_date = get_effective_date(now)
    results = []

    users = db.query(User).all()

    for user in users:
        due_types = get_due_supervision_types(db, user, now)
        for supervision_type in due_types:

            summary = get_user_task_summary(db, user.id, effective_date)

            if supervision_type in ("morning_goal", "unfinished_followup"):
                yesterday = get_effective_date(now - timedelta(days=1))
                yesterday_summary = get_user_task_summary(db, user.id, yesterday)
                summary["uncompleted_tasks"].extend(yesterday_summary["uncompleted_tasks"])

            try:
                message = await generate_supervision(
                    supervision_type=_to_prompt_type(supervision_type),
                    goal=summary["goal"],
                    completed_tasks=summary["completed_tasks"],
                    uncompleted_tasks=summary["uncompleted_tasks"],
                )

                # 存入对话记录
                msg = save_supervision_as_message(db, user.id, message, effective_date)

                # 尝试推送订阅消息
                channel = "in_app"
                template_id = settings.WECHAT_SUBSCRIBE_TEMPLATE_ID
                if template_id:
                    scene = _scene_for_type(supervision_type)
                    auth = _consume_subscribe_auth(db, user.id, template_id, scene)
                    if auth:
                        sent = await wechat_service.send_subscribe_message(
                            openid=user.openid,
                            template_id=template_id,
                            data={
                                "thing1": {"value": message[:20]},
                                "time2": {"value": now.strftime("%H:%M")},
                            },
                        )
                        if sent:
                            channel = "wechat"
                            auth.used = True
                            auth.used_at = now
                            db.commit()
                            _record_event(db, user, "wechat_notice_sent", {
                                "supervision_type": supervision_type,
                                "scene": scene,
                            })
                    else:
                        _record_event(db, user, "wechat_notice_no_auth", {
                            "supervision_type": supervision_type,
                            "scene": scene,
                        })

                _create_supervision_log(
                    db,
                    user_id=user.id,
                    supervision_type=supervision_type,
                    task_date=effective_date,
                    channel=channel,
                    message_id=msg.id,
                )
                _record_event(db, user, "supervision_generated", {
                    "supervision_type": supervision_type,
                    "channel": channel,
                })

                results.append({
                    "user_id": user.id,
                    "openid": user.openid,
                    "type": supervision_type,
                    "message": message,
                })
            except Exception as e:
                logger.error(f"用户 {user.id} 监督生成失败: {e}")

    return results
