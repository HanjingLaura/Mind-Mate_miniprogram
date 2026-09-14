"""三段式主动监督调度器

扩展方向：
- 增加 WebSocket 推送催促消息
- 增加催促频率可配置
- 增加用户免打扰时段
"""

import json
import logging
import secrets
from datetime import datetime, date, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from config import settings
from models import (
    User,
    DailyTask,
    Conversation,
    Message,
    SubscribeAuth,
    ScheduledReminder,
    SupervisionLog,
    AnalyticsEvent,
    AgentRun,
)
from services.execution_memory import build_execution_memory, format_supervision_memory
from services.llm_service import generate_supervision
from services.agent_orchestrator import advance_agent_stage, begin_agent_run, finish_agent_run, recover_stale_agent_runs, verify_agent_run
from services.message_content import strip_task_protocol
from services.user_state import normalize_vip_status
from services.time_service import (
    beijing_now,
    beijing_to_utc_naive,
    utc_now_naive,
    utc_naive_to_beijing,
)

logger = logging.getLogger(__name__)


def _lock_agent_run_lease(db: Session, agent_run: AgentRun) -> None:
    """Fence post-provider state writes behind the current AgentRun lease."""
    current = db.query(AgentRun).filter(
        AgentRun.id == agent_run.id,
        AgentRun.status == "running",
        AgentRun.lease_token == agent_run.lease_token,
    ).with_for_update().first()
    if current is None:
        raise RuntimeError("Agent run lease is no longer owned")


def _wechat_channel(result: bool | None) -> str:
    if result is True:
        return "wechat"
    if result is None:
        return "wechat_unknown"
    return "wechat_failed"

DAY_RESET_HOUR = 4  # 跨天结算边界：凌晨 4:00


def get_effective_date(now: datetime | None = None) -> date:
    """获取有效日期 — 凌晨 00:00~04:00 仍算前一天

    This ensures students who study late into the night
    are still working on "today's" tasks until 4 AM.
    """
    if now is None:
        now = beijing_now()
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


def _daily_intervention_count(db: Session, user_id: int, task_date: date) -> int:
    """Count proactive supervision only; user-created reminders do not consume this budget."""
    return db.query(SupervisionLog).filter(
        SupervisionLog.user_id == user_id,
        SupervisionLog.task_date == task_date,
        SupervisionLog.supervision_type.in_((
            "morning_goal",
            "unfinished_followup",
            "afternoon_nudge",
            "behavior_nudge",
            "evening_review",
        )),
    ).count()


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
        now = beijing_now()

    user = normalize_vip_status(db, user)
    task_date = get_effective_date(now)
    max_interventions = settings.AGENT_MAX_DAILY_INTERVENTIONS
    intervention_count = _daily_intervention_count(db, user.id, task_date)
    if max_interventions <= 0 or intervention_count >= max_interventions:
        return []
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
        return due[: max_interventions - intervention_count]

    # VIP：午后稳定随机狙击，只在还有未完成任务时触发。
    if _has_uncompleted_tasks(db, user.id, task_date):
        random_minute = _stable_afternoon_minute(user.id, task_date)
        if 13 <= now.hour < 18 and now_minutes >= random_minute:
            if not _has_supervision_log(db, user.id, task_date, "afternoon_nudge"):
                due.append("afternoon_nudge")

        # VIP：长时间无打卡追问。第一版保持每日最多一次，防止打扰过重。
        latest = utc_naive_to_beijing(_latest_task_activity(db, user.id, task_date))
        if latest and 9 <= now.hour < 22 and now - latest >= timedelta(minutes=max(15, settings.AGENT_IDLE_NUDGE_MINUTES)):
            if not _has_supervision_log(db, user.id, task_date, "behavior_nudge"):
                due.append("behavior_nudge")

    return due[: max_interventions - intervention_count]


def should_supervise(user: User, supervision_type: str, now: datetime | None = None) -> bool:
    """兼容旧调用：新逻辑请使用 get_due_supervision_types。"""
    if now is None:
        now = beijing_now()
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
    if supervision_type == "scheduled_reminder":
        return "scheduled_reminder"
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
    ).order_by(SubscribeAuth.auth_time.asc()).with_for_update().first()
    if auth:
        return auth

    return db.query(SubscribeAuth).filter(
        SubscribeAuth.user_id == user_id,
        SubscribeAuth.template_id == template_id,
        SubscribeAuth.used == False,
    ).order_by(SubscribeAuth.auth_time.asc()).with_for_update().first()


def _claim_subscribe_auth(db: Session, auth: SubscribeAuth) -> bool:
    """Reserve one one-time grant before the external WeChat call.

    The WeChat send API has no idempotency key.  Reserving first avoids using
    the same grant repeatedly after a timeout or a permanent WeChat error.
    """
    updated = db.query(SubscribeAuth).filter(
        SubscribeAuth.id == auth.id,
        SubscribeAuth.used.is_(False),
    ).update({
        "used": True,
        "used_at": utc_now_naive(),
    }, synchronize_session=False)
    return updated == 1


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
        now = beijing_now()

    yesterday = get_effective_date(now - timedelta(days=1)) if now.hour >= DAY_RESET_HOUR else get_effective_date(now)

    tasks = db.query(DailyTask).filter(
        DailyTask.task_date < get_effective_date(now),
        DailyTask.is_completed == False,
    ).all()

    # 这些任务保持 is_completed=False，已经自动标记为未完成
    # 返回数量作为晨间翻旧账的素材
    return len(tasks)


def save_supervision_as_message(
    db: Session,
    user_id: int,
    content: str,
    effective_date: date,
    supervision_type: str,
) -> tuple[Message, SupervisionLog]:
    """Atomically persist a fixed supervision message and its dedupe log."""
    content = strip_task_protocol(content)
    conv = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.date == effective_date,
    ).first()
    if not conv:
        conv = Conversation(user_id=user_id, date=effective_date, title=f"目标追踪 {effective_date}")
        db.add(conv)
        db.flush()

    msg = Message(conversation_id=conv.id, role="assistant", content=content)
    db.add(msg)
    db.flush()
    supervision_log = SupervisionLog(
        user_id=user_id,
        supervision_type=supervision_type,
        task_date=effective_date,
        channel="in_app",
        message_id=msg.id,
    )
    db.add(supervision_log)
    db.commit()
    db.refresh(msg)
    db.refresh(supervision_log)
    return msg, supervision_log


async def _deliver_pending_wechat_notice(
    db: Session,
    user: User,
    effective_date: date,
    now: datetime,
) -> dict | None:
    """Retry a same-day in-app reminder after the user grants notification access."""
    from config import settings
    from services.wechat_service import wechat_service

    template_id = settings.WECHAT_SUBSCRIBE_TEMPLATE_ID
    if not template_id:
        return None
    # Fresh in-app notices may be promoted. Known failures and uncertain sends
    # are retryable for this business day only after a new explicit grant.
    freshness_cutoff = utc_now_naive() - timedelta(minutes=5)
    pending_log = db.query(SupervisionLog).filter(
        SupervisionLog.user_id == user.id,
        SupervisionLog.task_date == effective_date,
        or_(
            and_(SupervisionLog.channel == "in_app", SupervisionLog.sent_at >= freshness_cutoff),
            SupervisionLog.channel.in_(("wechat_failed", "wechat_unknown")),
        ),
        SupervisionLog.message_id.isnot(None),
    ).order_by(SupervisionLog.sent_at.desc()).first()
    if not pending_log:
        return None
    scene = _scene_for_type(pending_log.supervision_type)
    auth = _consume_subscribe_auth(db, user.id, template_id, scene)
    if not auth:
        return None
    message = db.query(Message).filter(Message.id == pending_log.message_id).first()
    if not message:
        return None
    summary = get_user_task_summary(db, user.id, effective_date)
    pending_log.channel = "wechat_pending"
    if not _claim_subscribe_auth(db, auth):
        db.rollback()
        return None
    db.commit()
    sent = await wechat_service.send_subscribe_message(
        openid=user.openid,
        template_id=template_id,
        data={
            "thing1": {"value": message.content[:20]},
            "time2": {"value": now.strftime("%Y-%m-%d %H:%M")},
        },
    )
    pending_log.channel = _wechat_channel(sent)
    if pending_log.channel != "wechat":
        db.commit()
        _record_event(db, user, "wechat_notice_unknown" if sent is None else "wechat_notice_failed", {
            "supervision_type": pending_log.supervision_type,
            "scene": scene,
            "retried_after_auth": True,
        })
        return None
    pending_log.channel = "wechat"
    db.commit()
    _record_event(db, user, "wechat_notice_sent", {
        "supervision_type": pending_log.supervision_type,
        "scene": scene,
        "retried_after_auth": True,
    })
    return {
        "user_id": user.id,
        "openid": user.openid,
        "type": pending_log.supervision_type,
        "message": message.content,
        "channel": "wechat",
    }


def _fallback_supervision_message(supervision_type: str, summary: dict) -> str:
    """Keep reminders useful even when the LLM provider is unavailable."""
    completed = len(summary.get("completed_tasks") or [])
    pending = len(summary.get("uncompleted_tasks") or [])
    total = completed + pending
    if supervision_type == "evening_review":
        if total:
            return f"今晚复盘：{total} 个任务完成了 {completed} 个。没做完的先看一眼，决定现在收尾还是安排到明天。"
        return "今晚还没有任务记录。花一分钟写下明天最重要的一件事。"
    if supervision_type in {"unfinished_followup", "behavior_nudge"}:
        if pending:
            return f"还有 {pending} 个任务没动。先选一个最小步骤，或者告诉我卡在哪里。"
        return "今天还没看到新的完成记录。需要的话，把下一步缩小一点。"
    if supervision_type == "afternoon_nudge":
        return f"你还有 {pending} 个任务。现在方便的话，先做十分钟；不方便就调整一下时间。"
    if total:
        return f"今天有 {total} 个任务，先选一个最小的开始。"
    return "新的一天开始了。先列出今天最重要的一件事，再决定第一步。"


async def _retry_failed_scheduled_reminders(db: Session, now: datetime) -> list[dict]:
    """Retry only confirmed WeChat failures after a new user grant."""
    from config import settings
    from services.wechat_service import wechat_service

    cutoff = beijing_to_utc_naive(now) - timedelta(minutes=30)
    reminders = db.query(ScheduledReminder).filter(
        ScheduledReminder.status == "sent",
        ScheduledReminder.channel.in_(("wechat_failed", "wechat_unknown")),
        ScheduledReminder.sent_at >= cutoff,
    ).all()
    results = []
    for reminder in reminders:
        user = db.query(User).filter(User.id == reminder.user_id).first()
        auth = _consume_subscribe_auth(db, reminder.user_id, settings.WECHAT_SUBSCRIBE_TEMPLATE_ID, "scheduled_reminder") if user and settings.WECHAT_SUBSCRIBE_TEMPLATE_ID else None
        if not auth:
            continue
        reminder.channel = "wechat_pending"
        reminder.sent_at = utc_now_naive()
        if not _claim_subscribe_auth(db, auth):
            db.rollback()
            continue
        db.commit()
        try:
            sent = await wechat_service.send_subscribe_message(
                openid=user.openid,
                template_id=settings.WECHAT_SUBSCRIBE_TEMPLATE_ID,
                data={
                    "thing1": {"value": reminder.content[:20]},
                    "time2": {"value": utc_naive_to_beijing(reminder.scheduled_at).strftime("%Y-%m-%d %H:%M")},
                },
            )
            reminder.channel = _wechat_channel(sent)
            _record_event(db, user, "wechat_notice_sent" if sent is True else ("wechat_notice_unknown" if sent is None else "wechat_notice_failed"), {
                "supervision_type": "scheduled_reminder",
                "reminder_id": reminder.id,
                "scene": "scheduled_reminder",
                "retried_after_auth": True,
            })
        except Exception as exc:
            reminder.channel = "wechat_unknown"
            logger.warning("定时提醒 %s 重试结果未知: %s", reminder.id, exc)
            _record_event(db, user, "wechat_notice_unknown", {
                "supervision_type": "scheduled_reminder",
                "reminder_id": reminder.id,
                "scene": "scheduled_reminder",
                "retried_after_auth": True,
            })
        reminder.sent_at = utc_now_naive()
        db.commit()
        results.append({"user_id": user.id, "openid": user.openid, "type": "scheduled_reminder", "message": reminder.content, "channel": reminder.channel})
    return results


async def _deliver_due_scheduled_reminders(db: Session, now: datetime) -> list[dict]:
    """Deliver persisted exact-time reminders without invoking the LLM."""
    from config import settings
    from services.wechat_service import wechat_service

    now_utc = beijing_to_utc_naive(now)
    grace_cutoff = now_utc - timedelta(minutes=30)
    pending_cutoff = now_utc - timedelta(seconds=settings.AGENT_RUN_LEASE_SECONDS)

    # Recover only expired processing leases. The token prevents an old worker
    # from overwriting a newer scheduler attempt after this recovery runs.
    stale_processing = db.query(ScheduledReminder).filter(
        ScheduledReminder.status == "processing",
        ScheduledReminder.sent_at.isnot(None),
        ScheduledReminder.sent_at < pending_cutoff,
    ).all()
    for reminder in stale_processing:
        if reminder.message_id is None:
            db.query(ScheduledReminder).filter(
                ScheduledReminder.id == reminder.id,
                ScheduledReminder.status == "processing",
                ScheduledReminder.processing_token == reminder.processing_token,
            ).update({
                "status": "pending",
                "channel": "",
                "sent_at": None,
                "processing_token": "",
            }, synchronize_session=False)
            continue
        channel = "wechat_unknown" if reminder.channel == "wechat_pending" else "in_app"
        db.query(ScheduledReminder).filter(
            ScheduledReminder.id == reminder.id,
            ScheduledReminder.status == "processing",
            ScheduledReminder.processing_token == reminder.processing_token,
        ).update({
            "status": "sent",
            "channel": channel,
            "sent_at": now_utc,
            "processing_token": "",
        }, synchronize_session=False)
    if stale_processing:
        db.commit()

    stale_pending_wechat = db.query(ScheduledReminder).filter(
        ScheduledReminder.status == "sent",
        ScheduledReminder.channel == "wechat_pending",
        ScheduledReminder.sent_at < pending_cutoff,
    ).all()
    for reminder in stale_pending_wechat:
        db.query(ScheduledReminder).filter(
            ScheduledReminder.id == reminder.id,
            ScheduledReminder.status == "sent",
            ScheduledReminder.channel == "wechat_pending",
            ScheduledReminder.sent_at < pending_cutoff,
        ).update({"channel": "wechat_unknown", "sent_at": now_utc}, synchronize_session=False)
    if stale_pending_wechat:
        db.commit()

    # A reminder hours late is more confusing than useful.  Keep the row for
    # auditability, but do not suddenly push a stale external notification.
    stale = db.query(ScheduledReminder).filter(
        ScheduledReminder.status == "pending",
        ScheduledReminder.scheduled_at < grace_cutoff,
    ).all()
    for reminder in stale:
        reminder.status = "expired"
    if stale:
        db.commit()

    results: list[dict] = await _retry_failed_scheduled_reminders(db, now)
    reminders = db.query(ScheduledReminder).filter(
        ScheduledReminder.status == "pending",
        ScheduledReminder.scheduled_at >= grace_cutoff,
        ScheduledReminder.scheduled_at <= now_utc,
    ).order_by(ScheduledReminder.scheduled_at.asc()).limit(100).all()

    for reminder in reminders:
        user = db.query(User).filter(User.id == reminder.user_id).first()
        if not user:
            reminder.status = "expired"
            db.commit()
            continue

        remind_at = utc_naive_to_beijing(reminder.scheduled_at)
        effective_date = get_effective_date(remind_at)
        visible_text = f"到时间了：{reminder.content}。先从最小一步开始。"

        processing_token = ""
        try:
            # Claim the reminder atomically before creating the in-app message.
            # The token fences stale workers after a lease recovery.
            processing_token = secrets.token_urlsafe(24)
            claimed_at = utc_now_naive()
            claimed = db.query(ScheduledReminder).filter(
                ScheduledReminder.id == reminder.id,
                ScheduledReminder.status == "pending",
            ).update({
                "status": "processing",
                "channel": "in_app",
                "sent_at": claimed_at,
                "processing_token": processing_token,
            }, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                continue
            reminder.status = "processing"
            reminder.channel = "in_app"
            reminder.sent_at = claimed_at
            reminder.processing_token = processing_token
            conv = db.query(Conversation).filter(
                Conversation.user_id == user.id,
                Conversation.date == effective_date,
            ).first()
            if not conv:
                conv = Conversation(
                    user_id=user.id,
                    date=effective_date,
                    title=f"目标追踪 {effective_date}",
                )
                db.add(conv)
                db.flush()
            msg = Message(conversation_id=conv.id, role="assistant", content=visible_text)
            db.add(msg)
            db.flush()
            reminder.message_id = msg.id
            db.commit()

            channel = "in_app"
            template_id = settings.WECHAT_SUBSCRIBE_TEMPLATE_ID
            if template_id:
                auth = _consume_subscribe_auth(
                    db,
                    user.id,
                    template_id,
                    "scheduled_reminder",
                )
                if auth and _claim_subscribe_auth(db, auth):
                    reminder.channel = "wechat_pending"
                    db.commit()
                    try:
                        sent = await wechat_service.send_subscribe_message(
                            openid=user.openid,
                            template_id=template_id,
                            data={
                                "thing1": {"value": reminder.content[:20]},
                                "time2": {"value": remind_at.strftime("%Y-%m-%d %H:%M")},
                            },
                        )
                        channel = _wechat_channel(sent)
                        _record_event(db, user, "wechat_notice_sent" if sent is True else ("wechat_notice_unknown" if sent is None else "wechat_notice_failed"), {
                            "supervision_type": "scheduled_reminder",
                            "reminder_id": reminder.id,
                            "scene": "scheduled_reminder",
                        })
                    except Exception as exc:
                        channel = "wechat_unknown"
                        logger.warning("定时提醒 %s 微信结果未知: %s", reminder.id, exc)
                        _record_event(db, user, "wechat_notice_unknown", {
                            "supervision_type": "scheduled_reminder",
                            "reminder_id": reminder.id,
                            "scene": "scheduled_reminder",
                        })
                elif auth:
                    db.rollback()
                else:
                    _record_event(db, user, "wechat_notice_no_auth", {
                        "supervision_type": "scheduled_reminder",
                        "reminder_id": reminder.id,
                        "scene": "scheduled_reminder",
                    })

            finalized = db.query(ScheduledReminder).filter(
                ScheduledReminder.id == reminder.id,
                ScheduledReminder.status == "processing",
                ScheduledReminder.processing_token == processing_token,
            ).update({
                "status": "sent",
                "channel": channel,
                "sent_at": utc_now_naive(),
                "processing_token": "",
            }, synchronize_session=False)
            if finalized != 1:
                db.rollback()
                continue
            db.commit()
            _create_supervision_log(
                db,
                user_id=user.id,
                supervision_type="scheduled_reminder",
                task_date=effective_date,
                channel=channel,
                message_id=msg.id,
            )
            _record_event(db, user, "scheduled_reminder_delivered", {
                "reminder_id": reminder.id,
                "channel": channel,
            })
            results.append({
                "user_id": user.id,
                "openid": user.openid,
                "type": "scheduled_reminder",
                "message": visible_text,
                "channel": channel,
            })
        except Exception as exc:
            logger.error("定时提醒 %s 发送失败: %s", reminder.id, exc)
            db.rollback()
            current = db.query(ScheduledReminder).filter(
                ScheduledReminder.id == reminder.id,
            ).first()
            if current and processing_token:
                if current.message_id:
                    db.query(ScheduledReminder).filter(
                        ScheduledReminder.id == current.id,
                        ScheduledReminder.status == "processing",
                        ScheduledReminder.processing_token == processing_token,
                    ).update({
                        "status": "sent",
                        "channel": current.channel if current.channel in {
                            "wechat_pending", "wechat_failed", "wechat_unknown"
                        } else "in_app",
                        "sent_at": utc_now_naive(),
                        "processing_token": "",
                    }, synchronize_session=False)
                else:
                    db.query(ScheduledReminder).filter(
                        ScheduledReminder.id == current.id,
                        ScheduledReminder.status == "processing",
                        ScheduledReminder.processing_token == processing_token,
                    ).update({
                        "status": "pending",
                        "channel": "",
                        "sent_at": None,
                        "processing_token": "",
                    }, synchronize_session=False)
                db.commit()

    return results


async def run_supervision_cycle(db: Session) -> list[dict]:
    """运行一轮监督检查 — 由定时任务调用

    对符合条件的用户：
    1. 生成监督消息并存入对话
    2. 尝试通过微信订阅消息推送（如有授权）
    """
    from config import settings
    from services.wechat_service import wechat_service

    now = beijing_now()
    effective_date = get_effective_date(now)
    recover_stale_agent_runs(db)
    stale_notices = db.query(SupervisionLog).filter(
        SupervisionLog.channel == "wechat_pending",
        SupervisionLog.sent_at < utc_now_naive() - timedelta(seconds=settings.AGENT_RUN_LEASE_SECONDS),
    ).all()
    for notice in stale_notices:
        # WeChat has no idempotency key. A crashed send is unknown, not a
        # confirmed failure: keep the in-app message and never blindly resend.
        notice.channel = "wechat_unknown"
    if stale_notices:
        db.commit()
    results = await _deliver_due_scheduled_reminders(db, now)

    users = db.query(User).all()

    for user in users:
        try:
            retried = await _deliver_pending_wechat_notice(db, user, effective_date, now)
            if retried:
                results.append(retried)
        except Exception as e:
            logger.error(f"用户 {user.id} 待发送微信提醒重试失败: {e}")
            db.rollback()

        due_types = get_due_supervision_types(db, user, now)
        if not due_types:
            continue
        execution_memory = build_execution_memory(db, user.id, effective_date)
        memory_context = format_supervision_memory(execution_memory)
        for supervision_type in due_types:

            summary = get_user_task_summary(db, user.id, effective_date)

            if supervision_type in ("morning_goal", "unfinished_followup"):
                yesterday = get_effective_date(now - timedelta(days=1))
                yesterday_summary = get_user_task_summary(db, user.id, yesterday)
                summary["uncompleted_tasks"].extend(yesterday_summary["uncompleted_tasks"])
                if not summary["goal"]:
                    summary["goal"] = yesterday_summary["goal"]

            run_key = f"supervision:{effective_date.isoformat()}:{supervision_type}"
            agent_run, should_run = begin_agent_run(
                db,
                user_id=user.id,
                run_key=run_key,
                agent_name="supervisor",
                stage="understand",
                trigger="scheduled",
                input_snapshot={
                    "supervision_type": supervision_type,
                    "task_date": effective_date.isoformat(),
                    "goal": summary["goal"],
                    "completed_count": len(summary["completed_tasks"]),
                    "pending_count": len(summary["uncompleted_tasks"]),
                },
            )
            if not should_run:
                logger.info("监督 Agent 跳过重复运行: user=%s key=%s", user.id, run_key)
                continue

            try:
                advance_agent_stage(db, agent_run, "plan")
                agent_status = "completed"
                agent_provider = ""
                agent_output = {}
                try:
                    generated = await generate_supervision(
                        supervision_type=_to_prompt_type(supervision_type),
                        goal=summary["goal"],
                        completed_tasks=summary["completed_tasks"],
                        uncompleted_tasks=summary["uncompleted_tasks"],
                        memory_context=memory_context,
                        with_provider=True,
                    )
                    if isinstance(generated, tuple):
                        message, agent_provider = generated
                    else:
                        message, agent_provider = generated, "unknown"
                    agent_output = {"message": message, "supervision_type": supervision_type}
                except Exception as llm_error:
                    logger.warning(
                        "用户 %s 监督文案生成失败，使用固定兜底: %s",
                        user.id,
                        llm_error,
                    )
                    message = _fallback_supervision_message(supervision_type, summary)
                    agent_status = "fallback"
                    agent_provider = "fallback"
                    agent_output = {"message": message, "reason": "model_unavailable"}

                # Fence the side effect before saving the message and de-dup log.
                # The stage update holds the row lock until this transaction commits.
                advance_agent_stage(db, agent_run, "act")
                msg, supervision_log = save_supervision_as_message(
                    db,
                    user.id,
                    message,
                    effective_date,
                    supervision_type,
                )
                # 尝试推送订阅消息
                channel = "in_app"
                template_id = settings.WECHAT_SUBSCRIBE_TEMPLATE_ID
                if template_id:
                    scene = _scene_for_type(supervision_type)
                    auth = _consume_subscribe_auth(db, user.id, template_id, scene)
                    if auth and _claim_subscribe_auth(db, auth):
                        supervision_log.channel = "wechat_pending"
                        supervision_log.sent_at = utc_now_naive()
                        db.commit()
                        sent = await wechat_service.send_subscribe_message(
                            openid=user.openid,
                            template_id=template_id,
                            data={
                                # Template 37312: reminder content and time.
                                "thing1": {"value": message[:20]},
                                "time2": {"value": now.strftime("%Y-%m-%d %H:%M")},
                            },
                        )
                        _lock_agent_run_lease(db, agent_run)
                        if sent is True:
                            channel = "wechat"
                            supervision_log.channel = channel
                            db.commit()
                            _record_event(db, user, "wechat_notice_sent", {
                                "supervision_type": supervision_type,
                                "scene": scene,
                            })
                        elif sent is None:
                            channel = "wechat_unknown"
                            supervision_log.channel = channel
                            db.commit()
                            _record_event(db, user, "wechat_notice_unknown", {
                                "supervision_type": supervision_type,
                                "scene": scene,
                            })
                        else:
                            channel = "wechat_failed"
                            supervision_log.channel = channel
                            db.commit()
                            _record_event(db, user, "wechat_notice_failed", {
                                "supervision_type": supervision_type,
                                "scene": scene,
                            })
                    elif auth:
                        db.rollback()
                    else:
                        _record_event(db, user, "wechat_notice_no_auth", {
                            "supervision_type": supervision_type,
                            "scene": scene,
                        })
                _record_event(db, user, "supervision_generated", {
                    "supervision_type": supervision_type,
                    "channel": channel,
                })
                verification = verify_agent_run(db, agent_run, {
                    "message_persisted": db.get(Message, msg.id) is not None,
                    "supervision_log_persisted": db.get(SupervisionLog, supervision_log.id) is not None,
                    "channel_finalized": channel in {"in_app", "wechat", "wechat_failed", "wechat_unknown"},
                })
                finish_agent_run(
                    db,
                    agent_run,
                    status=agent_status,
                    output_snapshot={**agent_output, "channel": channel, "verification": verification},
                    provider=agent_provider,
                    stage="reflect",
                )

                results.append({
                    "user_id": user.id,
                    "openid": user.openid,
                    "type": supervision_type,
                    "message": message,
                })
            except Exception as e:
                try:
                    finish_agent_run(db, agent_run, status="failed", error_message=str(e))
                except Exception:
                    db.rollback()
                logger.error(f"用户 {user.id} 监督生成失败: {e}")
                db.rollback()

    return results
