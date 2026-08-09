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
from models import User, DailyTask
from schemas import DailyTaskOut, TaskCheckInRequest, TaskCreateRequest, TaskEditRequest, TaskDeleteRequest
from services.execution_memory import build_execution_memory, empty_execution_memory
from services.scheduler_service import get_effective_date
from services.time_service import utc_now_naive

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)


@router.get("/today/{openid}", response_model=list[DailyTaskOut])
async def get_today_tasks(openid: str, db: Session = Depends(get_db)):
    """获取今日任务列表（考虑 04:00 跨天边界）"""
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
async def checkin_task(req: TaskCheckInRequest, db: Session = Depends(get_db)):
    """任务打卡 — 标记为已完成"""
    try:
        user = db.query(User).filter(User.openid == req.openid).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        task = db.query(DailyTask).filter(
            DailyTask.id == req.task_id,
            DailyTask.user_id == user.id,
        ).first()

        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        # Completion is a user-controlled toggle. The chat model must never
        # silently change this state, and a completed task can be restored.
        task.is_completed = not task.is_completed
        task.completed_at = utc_now_naive() if task.is_completed else None
        db.commit()

        return {
            "success": True,
            "is_completed": task.is_completed,
            "message": "任务已完成" if task.is_completed else "已恢复为未完成",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"打卡失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="打卡失败，请重试")


@router.get("/yesterday_uncompleted/{openid}")
async def get_yesterday_uncompleted(openid: str, db: Session = Depends(get_db)):
    """获取昨日未完成任务 — 晨间翻旧账用"""
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
async def get_execution_memory(openid: str, db: Session = Depends(get_db)):
    """获取昨日复盘与近 7 天执行记忆。"""
    effective_date = get_effective_date()
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        return empty_execution_memory(effective_date)
    return build_execution_memory(db, user.id, effective_date)


@router.post("/add")
async def add_task(req: TaskCreateRequest, db: Session = Depends(get_db)):
    """手动添加任务"""
    user = db.query(User).filter(User.openid == req.openid).first()
    if not user:
        user = User(openid=req.openid)
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
async def edit_task(req: TaskEditRequest, db: Session = Depends(get_db)):
    """Edit a task without changing its completion state."""
    user = db.query(User).filter(User.openid == req.openid).first()
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
async def delete_task(req: TaskDeleteRequest, db: Session = Depends(get_db)):
    """删除任务"""
    user = db.query(User).filter(User.openid == req.openid).first()
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
