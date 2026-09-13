"""任务路由 — 每日任务看板与打卡

扩展方向：
- 任务优先级排序
- 任务分类标签
- 批量打卡接口
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import User, DailyTask, Conversation, Message
from schemas import DailyTaskOut, TaskCheckInRequest, TaskCreateRequest, TaskEditRequest, TaskDeleteRequest
from services.execution_memory import build_execution_memory, empty_execution_memory
from services.auth_service import get_trusted_openid, require_openid_match
from services.scheduler_service import get_effective_date
from services.time_service import utc_now_naive
from services.agent_orchestrator import advance_agent_stage, begin_agent_run, finish_agent_run, verify_agent_run

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _completion_encouragement(completed: int, total: int, task_content: str) -> str:
    """Short, factual praise shown immediately after a manual check-in."""
    if total > 0 and completed >= total:
        return f"漂亮，{task_content}拿下。今天 {total} 个任务全部清空了，这波真可以。"
    if completed == 1:
        return f"可以，{task_content}已经拿下。第一步最难，你已经动起来了。"
    return f"稳，{task_content}完成。现在是 {completed}/{total}，继续保持这个节奏。"


def _save_encouragement(db: Session, user_id: int, content: str, task_date) -> Message:
    conv = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.date == task_date,
    ).first()
    if not conv:
        conv = Conversation(user_id=user_id, date=task_date, title=f"目标追踪 {task_date}")
        db.add(conv)
        db.flush()
    message = Message(conversation_id=conv.id, role="assistant", content=content)
    db.add(message)
    db.flush()
    return message
logger = logging.getLogger(__name__)


@router.get("/today/{openid}", response_model=list[DailyTaskOut])
async def get_today_tasks(
    openid: str,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """获取今日任务列表（考虑 04:00 跨天边界）"""
    openid = require_openid_match(openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        return []

    effective_date = get_effective_date()
    tasks = db.query(DailyTask).filter(
        DailyTask.user_id == user.id,
        DailyTask.task_date == effective_date,
    ).all()

    return tasks


@router.post("/checkin")
async def checkin_task(
    req: TaskCheckInRequest,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """任务打卡 — 标记为已完成"""
    agent_run = None
    should_audit = False
    try:
        openid = require_openid_match(req.openid, trusted_openid)
        user = db.query(User).filter(User.openid == openid).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        task = db.query(DailyTask).filter(
            DailyTask.id == req.task_id,
            DailyTask.user_id == user.id,
        ).first()

        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        agent_run, should_audit = begin_agent_run(
            db,
            user_id=user.id,
            run_key=f"task-checkin:{task.id}:{task.task_date.isoformat()}:{req.completed}",
            agent_name="task-checkin",
            stage="understand",
            trigger="task_change",
            input_snapshot={"task_id": task.id, "requested_completed": req.completed},
        )
        if should_audit:
            advance_agent_stage(db, agent_run, "plan")
            advance_agent_stage(db, agent_run, "act")

        # begin_agent_run claims its own transaction and may commit, so take
        # the row lock again after the claim and before reading the state.
        task = db.query(DailyTask).filter(
            DailyTask.id == req.task_id,
            DailyTask.user_id == user.id,
        ).with_for_update().first()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        # Set an explicit target state instead of toggling. This makes a
        # network retry idempotent and prevents a successful check-in from
        # being accidentally reversed when the response is retried.
        state_changed = task.is_completed != req.completed
        task.is_completed = req.completed
        if state_changed:
            task.completed_at = utc_now_naive() if task.is_completed else None
        db.flush()

        encouragement = ""
        encouragement_message_id = None
        if task.is_completed and state_changed:
            tasks = db.query(DailyTask).filter(
                DailyTask.user_id == user.id,
                DailyTask.task_date == task.task_date,
            ).all()
            completed = sum(1 for item in tasks if item.is_completed)
            encouragement = _completion_encouragement(completed, len(tasks), task.content)
            encouragement_message = _save_encouragement(
                db, user.id, encouragement, task.task_date
            )
            encouragement_message_id = encouragement_message.id
        db.commit()
        db.refresh(task)
        if should_audit:
            verification = verify_agent_run(db, agent_run, {
                "task_state_matches_request": task.is_completed == req.completed,
                "completion_timestamp_matches_state": bool(task.completed_at) == req.completed,
            })
            finish_agent_run(
                db, agent_run, status="completed", stage="reflect", provider="deterministic",
                output_snapshot={
                    "task_id": task.id, "is_completed": task.is_completed,
                    "state_changed": state_changed, "verification": verification,
                    "encouragement_message_id": encouragement_message_id,
                },
            )

        return {
            "success": True,
            "is_completed": task.is_completed,
            "state_changed": state_changed,
            "message": "任务已完成" if task.is_completed else "已恢复为未完成",
            "encouragement": encouragement,
            "encouragement_message_id": encouragement_message_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"打卡失败: {e}")
        db.rollback()
        if agent_run and should_audit:
            try:
                finish_agent_run(db, agent_run, status="failed", error_message=str(e))
            except Exception:
                db.rollback()
        raise HTTPException(status_code=500, detail="打卡失败，请重试")


@router.get("/yesterday_uncompleted/{openid}")
async def get_yesterday_uncompleted(
    openid: str,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """获取昨日未完成任务 — 晨间翻旧账用"""
    openid = require_openid_match(openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        return []

    from datetime import timedelta
    yesterday = get_effective_date() - timedelta(days=1)
    tasks = db.query(DailyTask).filter(
        DailyTask.user_id == user.id,
        DailyTask.task_date == yesterday,
        DailyTask.is_completed == False,
    ).all()

    return [
        {
            "id": t.id,
            "content": t.content,
            "goal": t.goal,
            "task_date": t.task_date.isoformat(),
        }
        for t in tasks
    ]


@router.get("/memory/{openid}")
async def get_execution_memory(
    openid: str,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """获取昨日复盘与近 7 天执行记忆。"""
    effective_date = get_effective_date()
    openid = require_openid_match(openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        return empty_execution_memory(effective_date)
    return build_execution_memory(db, user.id, effective_date)


@router.post("/add")
async def add_task(
    req: TaskCreateRequest,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """手动添加任务"""
    openid = require_openid_match(req.openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        user = User(openid=openid)
        db.add(user)
        db.commit()
        db.refresh(user)

    effective_date = get_effective_date()
    goal = req.goal
    if not goal:
        existing = db.query(DailyTask).filter(
            DailyTask.user_id == user.id,
            DailyTask.task_date == effective_date,
        ).first()
        goal = existing.goal if existing else "今日目标"

    task = DailyTask(
        user_id=user.id,
        task_date=effective_date,
        goal=goal,
        content=req.content.strip(),
        is_completed=False,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    return DailyTaskOut.model_validate(task)


@router.post("/edit", response_model=DailyTaskOut)
async def edit_task(
    req: TaskEditRequest,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """Edit a task without changing its completion state."""
    openid = require_openid_match(req.openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    task = db.query(DailyTask).filter(
        DailyTask.id == req.task_id,
        DailyTask.user_id == user.id,
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    task.content = req.content.strip()
    db.commit()
    db.refresh(task)
    return DailyTaskOut.model_validate(task)


@router.delete("/delete")
async def delete_task(
    req: TaskDeleteRequest,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """删除任务"""
    openid = require_openid_match(req.openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    task = db.query(DailyTask).filter(
        DailyTask.id == req.task_id,
        DailyTask.user_id == user.id,
    ).first()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    db.delete(task)
    db.commit()
    return {"success": True}
