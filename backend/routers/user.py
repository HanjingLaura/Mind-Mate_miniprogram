"""用户路由 — 个人信息与设置"""

import hashlib
import logging
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import User, SubscribeAuth
from schemas import LoginRequest, UserOut, UserSettingsUpdate
from services.auth_service import get_trusted_openid, issue_app_token, require_openid_match
from services.user_state import normalize_vip_status

router = APIRouter(prefix="/api/user", tags=["user"])
logger = logging.getLogger(__name__)


@router.get("/profile/{openid}", response_model=UserOut)
async def get_profile(
    openid: str,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """获取用户信息 — 不存在则自动创建"""
    openid = require_openid_match(openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        user = User(openid=openid)
        db.add(user)
        db.commit()
        db.refresh(user)
    return normalize_vip_status(db, user)


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """登录/注册 — 支持 code 换 openid（生产）或直接传 openid（开发）"""
    try:
        # wx.cloud.callContainer injects this trusted identity header when the
        # request comes from the bound Mini Program. Prefer it over exchanging
        # a temporary wx.login code through the public API.
        resolved_openid = request.headers.get("x-wx-openid", "").strip()
        if not resolved_openid and req.device_id.strip():
            resolved_openid = "app_" + hashlib.sha256(req.device_id.strip().encode()).hexdigest()[:24]
        if not resolved_openid and req.openid and settings.ALLOW_INSECURE_DEV_OPENID:
            resolved_openid = req.openid

        if not resolved_openid and req.code and settings.WECHAT_APPID and settings.WECHAT_SECRET:
            import httpx
            url = "https://api.weixin.qq.com/sns/jscode2session"
            params = {
                "appid": settings.WECHAT_APPID,
                "secret": settings.WECHAT_SECRET,
                "js_code": req.code,
                "grant_type": "authorization_code",
            }
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params)
                data = resp.json()
                if "openid" in data:
                    resolved_openid = data["openid"]
                else:
                    logger.warning(f"code2session 失败: {data}")
                    raise HTTPException(status_code=502, detail="微信登录失败，请稍后重试")

        if (
            not resolved_openid
            and req.code
            and not (settings.WECHAT_APPID and settings.WECHAT_SECRET)
            and settings.ALLOW_INSECURE_DEV_OPENID
        ):
            resolved_openid = "dev_" + hashlib.md5(req.code.encode()).hexdigest()[:12]

        if not resolved_openid:
            raise HTTPException(status_code=400, detail="需要 code 或 openid")

        user = db.query(User).filter(User.openid == resolved_openid).first()
        if not user:
            user = User(openid=resolved_openid)
            db.add(user)
            db.commit()
            db.refresh(user)
            token = issue_app_token(resolved_openid)
            return {
                "user_id": user.id,
                "openid": resolved_openid,
                "token": token,
                "is_new": True,
                "is_vip": user.is_vip,
            }

        user = normalize_vip_status(db, user)
        token = issue_app_token(resolved_openid)
        return {
            "user_id": user.id,
            "openid": resolved_openid,
            "token": token,
            "is_new": False,
            "is_vip": user.is_vip,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"登录失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="登录失败")


@router.put("/settings/{openid}")
async def update_settings(
    openid: str,
    settings_update: UserSettingsUpdate,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """更新用户自定义作息时间"""
    try:
        openid = require_openid_match(openid, trusted_openid)
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


class SubscribeAuthRequest(BaseModel):
    openid: str
    template_id: str = ""
    scene: str = "general"


@router.post("/subscribe_auth")
async def subscribe_auth(
    req: SubscribeAuthRequest,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """记录用户授权订阅消息（一次性授权）"""
    openid = require_openid_match(req.openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    template_id = settings.WECHAT_SUBSCRIBE_TEMPLATE_ID or "app-inapp"
    auth = SubscribeAuth(user_id=user.id, template_id=template_id, scene=req.scene or "general")
    db.add(auth)
    db.commit()

    unused_count = db.query(SubscribeAuth).filter(
        SubscribeAuth.user_id == user.id,
        SubscribeAuth.template_id == template_id,
        SubscribeAuth.used == False,
    ).count()

    return {"success": True, "unused_count": unused_count}


@router.get("/subscribe_status/{openid}")
async def subscribe_status(
    openid: str,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """Return remaining one-time WeChat subscription-message grants."""
    openid = require_openid_match(openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        return {"unused_count": 0, "enabled": False}
    template_id = settings.WECHAT_SUBSCRIBE_TEMPLATE_ID or "app-inapp"
    unused_count = db.query(SubscribeAuth).filter(
        SubscribeAuth.user_id == user.id,
        SubscribeAuth.template_id == template_id,
        SubscribeAuth.used == False,
    ).count()
    return {"unused_count": unused_count, "enabled": unused_count > 0}
