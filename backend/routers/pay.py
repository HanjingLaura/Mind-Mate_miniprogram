"""支付路由 — 微信支付 V3 闭环

扩展方向：
- 增加订单查询
- 增加退款接口
- 支持 VIP 订阅模式
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import User
from schemas import PayOrderRequest, PayOrderOut
from services.pay_service import wechat_pay

router = APIRouter(prefix="/api/pay", tags=["pay"])
logger = logging.getLogger(__name__)


@router.post("/create_order", response_model=PayOrderOut)
async def create_pay_order(req: PayOrderRequest, db: Session = Depends(get_db)):
    """统一下单接口 — 返回前端拉起支付所需参数"""
    user = db.query(User).filter(User.openid == req.openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if user.is_vip:
        raise HTTPException(status_code=400, detail="你已经是 VIP 了，不需要重复购买")

    # 开发/未配置环境直接提示，避免打到微信接口导致超时
    required = {
        "WECHAT_MCHID": settings.WECHAT_MCHID,
        "WECHAT_APIV3_KEY": settings.WECHAT_APIV3_KEY,
        "WECHAT_CERT_SERIAL_NO": settings.WECHAT_CERT_SERIAL_NO,
        "WECHAT_NOTIFY_URL": settings.WECHAT_NOTIFY_URL,
        "WECHAT_PRIVATE_KEY_PATH": settings.WECHAT_PRIVATE_KEY_PATH,
    }
    missing = [k for k, v in required.items() if not v or v.startswith("your_")]
    if missing:
        raise HTTPException(
            status_code=400,
            detail="微信支付未配置，请先在 backend/.env 填写商户参数",
        )

    try:
        params = await wechat_pay.create_order(openid=req.openid)
        return params
    except Exception as e:
        logger.error(f"下单失败: {e}")
        raise HTTPException(status_code=500, detail="下单失败，请稍后重试")


@router.post("/callback")
async def pay_callback(request: Request, db: Session = Depends(get_db)):
    """微信支付异步回调通知 — 验签后升级 VIP

    微信支付服务器会向此接口发送支付结果通知。
    必须验签后才能信任通知内容。
    """
    try:
        body = await request.body()
        headers = dict(request.headers)

        # 验签 + 解密
        data = wechat_pay.verify_callback(headers, body)
        if not data:
            logger.warning("支付回调验签失败")
            return {"code": "FAIL", "message": "验签失败"}

        # 提取关键信息
        out_trade_no = data.get("out_trade_no", "")
        trade_state = data.get("trade_state", "")
        openid = data.get("payer", {}).get("openid", "")

        if trade_state != "SUCCESS":
            logger.info(f"订单 {out_trade_no} 状态非成功: {trade_state}")
            return {"code": "SUCCESS", "message": "已接收"}

        # 升级 VIP
        user = db.query(User).filter(User.openid == openid).first()
        if not user:
            logger.error(f"回调用户不存在: {openid}")
            return {"code": "FAIL", "message": "用户不存在"}

        if user.is_vip:
            logger.info(f"用户 {openid} 已是 VIP，跳过升级")
            return {"code": "SUCCESS", "message": "已处理"}

        user.is_vip = True
        from datetime import datetime, timedelta
        user.vip_expire_at = datetime.utcnow() + timedelta(days=30)
        db.commit()

        logger.info(f"用户 {openid} VIP 升级成功")
        return {"code": "SUCCESS", "message": "OK"}

    except Exception as e:
        logger.error(f"支付回调处理异常: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return {"code": "FAIL", "message": "处理异常"}


@router.get("/status/{openid}")
async def get_pay_status(openid: str, db: Session = Depends(get_db)):
    """查询用户 VIP 状态"""
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    return {
        "is_vip": user.is_vip,
        "vip_expire_at": user.vip_expire_at.isoformat() if user.vip_expire_at else None,
    }
