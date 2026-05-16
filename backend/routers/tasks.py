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
from schemas import DailyTaskOut, TaskCheckInRequest
from services.scheduler_service import get_effective_date

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
async def checkin_task(req: TaskCheckInRequest, openid: str, db: Session = Depends(get_db)):
    """任务打卡 — 标记为已完成"""
    try:
        user = db.query(User).filter(User.openid == openid).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        task = db.query(DailyTask).filter(
            DailyTask.id == req.task_id,
            DailyTask.user_id == user.id,
        ).first()

        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        task.is_completed = True
        from datetime import datetime
        task.completed_at = datetime.utcnow()
        db.commit()

        return {"success": True, "message": "打卡成功！又一个任务拿下！"}

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
