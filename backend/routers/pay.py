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
from models import User, PayOrder
from schemas import PayOrderRequest, PayCancelRequest, PayOrderOut, PayStatusOut
from services.pay_service import wechat_pay, generate_out_trade_no
from services.user_state import normalize_vip_status

router = APIRouter(prefix="/api/pay", tags=["pay"])
logger = logging.getLogger(__name__)


@router.post("/create_order", response_model=PayOrderOut)
async def create_pay_order(req: PayOrderRequest, db: Session = Depends(get_db)):
    """统一下单接口 — 返回前端拉起支付所需参数"""
    user = db.query(User).filter(User.openid == req.openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user = normalize_vip_status(db, user)

    if user.is_vip:
        raise HTTPException(status_code=400, detail="你已经是 VIP 了，不需要重复购买")

    # 检查支付配置
    ready, missing = wechat_pay.is_configured()
    if not ready:
        raise HTTPException(
            status_code=400,
            detail=f"微信支付未配置，缺少: {', '.join(missing)}",
        )

    try:
        out_trade_no = generate_out_trade_no()
        order = PayOrder(
            out_trade_no=out_trade_no,
            user_id=user.id,
            openid=req.openid,
            amount_cents=settings.VIP_PRICE_CENTS,
            status="created",
        )
        db.add(order)
        db.commit()

        params = await wechat_pay.create_order(openid=req.openid, out_trade_no=out_trade_no)
        order.prepay_id = params.get("prepay_id", "")
        db.commit()
        return params
    except Exception as e:
        logger.error(f"下单失败: {e}")
        try:
            if "order" in locals():
                order.status = "failed"
                db.commit()
        except Exception:
            db.rollback()
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
            order = db.query(PayOrder).filter(PayOrder.out_trade_no == out_trade_no).first()
            if order:
                order.status = "failed"
                db.commit()
            return {"code": "SUCCESS", "message": "已接收"}

        order = db.query(PayOrder).filter(PayOrder.out_trade_no == out_trade_no).first()
        if not order:
            logger.error(f"回调订单不存在: {out_trade_no}")
            return {"code": "FAIL", "message": "订单不存在"}

        # 升级 VIP
        user = db.query(User).filter(User.id == order.user_id).first()
        if not user:
            logger.error(f"回调用户不存在: {order.user_id}")
            return {"code": "FAIL", "message": "用户不存在"}

        if order.status == "paid":
            logger.info(f"订单 {out_trade_no} 已处理，跳过重复回调")
            return {"code": "SUCCESS", "message": "已处理"}

        from datetime import datetime, timedelta
        order.status = "paid"
        order.paid_at = datetime.utcnow()
        user.is_vip = True
        user.vip_expire_at = datetime.utcnow() + timedelta(days=30)
        db.commit()

        logger.info(f"用户 {user.openid} VIP 升级成功")
        return {"code": "SUCCESS", "message": "OK"}

    except Exception as e:
        logger.error(f"支付回调处理异常: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return {"code": "FAIL", "message": "处理异常"}


@router.post("/cancel")
async def cancel_pay_order(req: PayCancelRequest, db: Session = Depends(get_db)):
    """前端支付取消后回写订单状态，用于付费漏斗分析。"""
    order = db.query(PayOrder).filter(
        PayOrder.out_trade_no == req.out_trade_no,
        PayOrder.openid == req.openid,
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status == "created":
        order.status = "cancelled"
        db.commit()
    return {"success": True, "status": order.status}


@router.get("/status/{openid}", response_model=PayStatusOut)
async def get_pay_status(openid: str, db: Session = Depends(get_db)):
    """查询用户 VIP 状态"""
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user = normalize_vip_status(db, user)

    latest_order = db.query(PayOrder).filter(
        PayOrder.user_id == user.id
    ).order_by(PayOrder.created_at.desc()).first()

    return {
        "is_vip": user.is_vip,
        "vip_expire_at": user.vip_expire_at,
        "latest_order_status": latest_order.status if latest_order else "",
        "latest_out_trade_no": latest_order.out_trade_no if latest_order else "",
    }
