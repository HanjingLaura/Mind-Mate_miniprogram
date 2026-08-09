"""管理员路由 — 种子用户内测通道

扩展方向：
- 批量升级接口
- 种子用户邀请码系统
- 管理员数据看板
"""

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import User
from schemas import AdminBypassRequest

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)


@router.post("/bypass_upgrade")
async def bypass_upgrade(req: AdminBypassRequest, db: Session = Depends(get_db)):
    """管理员内测升级接口 — 密钥验证后直接升级 VIP

    用于种子用户免费体验全天候监督功能。
    密钥从环境变量 ADMIN_SECRET_KEY 读取，绝不硬编码。
    """
    if not settings.ADMIN_BYPASS_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")

    # 使用常量时间比较，且空密钥永远不能通过。
    if not settings.ADMIN_SECRET_KEY or not secrets.compare_digest(
        req.secret_key,
        settings.ADMIN_SECRET_KEY,
    ):
        logger.warning(f"管理员升级密钥错误，user_id={req.user_id}")
        raise HTTPException(status_code=403, detail="暗号错误，你不是内测人员吧？")

    try:
        user = db.query(User).filter(User.id == req.user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        if user.is_vip:
            return {"success": True, "message": "已经是 VIP 了，无需重复升级"}

        user.is_vip = True
        from datetime import datetime, timedelta
        user.vip_expire_at = datetime.utcnow() + timedelta(days=90)  # 内测给 90 天
        db.commit()

        logger.info(f"管理员升级成功: user_id={req.user_id}")
        return {"success": True, "message": "VIP 升级成功！全天候监督已开启，准备好被追杀吧"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"管理员升级失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="升级失败，请联系管理员")
