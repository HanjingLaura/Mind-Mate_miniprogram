"""反馈与产品事件埋点。"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import User, Feedback, AnalyticsEvent
from schemas import FeedbackCreate, AnalyticsEventCreate
from services.auth_service import get_trusted_openid, require_openid_match

router = APIRouter(prefix="/api", tags=["telemetry"])
logger = logging.getLogger(__name__)


@router.post("/feedback")
async def create_feedback(
    req: FeedbackCreate,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """记录一条轻反馈。"""
    openid = require_openid_match(req.openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    feedback = Feedback(
        user_id=user.id,
        target_type=req.target_type,
        target_id=req.target_id,
        rating=req.rating,
        comment=req.comment,
    )
    db.add(feedback)
    db.commit()
    return {"success": True, "feedback_id": feedback.id}


@router.post("/events")
async def create_event(
    req: AnalyticsEventCreate,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """记录一条产品事件。"""
    openid = require_openid_match(req.openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()

    event = AnalyticsEvent(
        user_id=user.id if user else None,
        openid=openid,
        event_name=req.event_name,
        properties=json.dumps(req.properties, ensure_ascii=False),
    )
    db.add(event)
    db.commit()
    return {"success": True}
