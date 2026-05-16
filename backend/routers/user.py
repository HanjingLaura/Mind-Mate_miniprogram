"""用户路由 — 个人信息与设置

扩展方向：
- 增加用户头像上传
- 增加学习数据统计
- 增加连续打卡天数
"""

import logging
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import User
from schemas import UserOut, UserSettingsUpdate

router = APIRouter(prefix="/api/user", tags=["user"])
logger = logging.getLogger(__name__)


@router.get("/profile/{openid}", response_model=UserOut)
async def get_profile(openid: str, db: Session = Depends(get_db)):
    """获取用户信息 — 不存在则自动创建"""
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        user = User(openid=openid)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


@router.post("/login")
async def login(openid: str, db: Session = Depends(get_db)):
    """登录/注册 — 微信登录后调用此接口同步用户"""
    try:
        user = db.query(User).filter(User.openid == openid).first()
        if not user:
            user = User(openid=openid)
            db.add(user)
            db.commit()
            db.refresh(user)
            return {"user_id": user.id, "is_new": True, "is_vip": user.is_vip}

        return {"user_id": user.id, "is_new": False, "is_vip": user.is_vip}

    except Exception as e:
        logger.error(f"登录失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="登录失败")


@router.put("/settings/{openid}")
async def update_settings(
    openid: str,
    settings_update: UserSettingsUpdate,
    db: Session = Depends(get_db),
):
    """更新用户自定义作息时间"""
    try:
        user = db.query(User).filter(User.openid == openid).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        if settings_update.morning_time:
            parts = settings_update.morning_time.split(":")
            user.morning_time = time(int(parts[0]), int(parts[1]))

        if settings_update.afternoon_time:
            parts = settings_update.afternoon_time.split(":")
            user.afternoon_time = time(int(parts[0]), int(parts[1]))

        if settings_update.evening_time:
            parts = settings_update.evening_time.split(":")
            user.evening_time = time(int(parts[0]), int(parts[1]))

        user.updated_at = datetime.utcnow()
        db.commit()

        return {"success": True, "message": "作息时间已更新"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"设置更新失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="设置更新失败")
