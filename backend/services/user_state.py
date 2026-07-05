"""用户状态工具函数。"""

from datetime import datetime

from sqlalchemy.orm import Session

from models import User


def normalize_vip_status(db: Session, user: User) -> User:
    """如果 VIP 已过期，立即收回权益并落库。"""
    if user.is_vip and user.vip_expire_at and user.vip_expire_at <= datetime.utcnow():
        user.is_vip = False
        db.commit()
        db.refresh(user)
    return user
