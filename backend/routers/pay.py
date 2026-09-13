"""支付路由 — 微信支付 V3 闭环

扩展方向：
- 增加订单查询
- 增加退款接口
- 支持 VIP 订阅模式
"""

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import User, PayOrder
from schemas import PayOrderRequest, PayCancelRequest, PayOrderOut, PayStatusOut
from services.pay_service import wechat_pay, generate_out_trade_no
from services.auth_service import get_trusted_openid, require_openid_match
from services.user_state import normalize_vip_status

router = APIRouter(prefix="/api/pay", tags=["pay"])
logger = logging.getLogger(__name__)


def _validate_paid_order(data: dict, order: PayOrder) -> str:
    """校验回调确实属于本应用、本商户和本地订单。返回空串表示通过。"""
    amount = data.get("amount") or {}
    payer = data.get("payer") or {}
    checks = (
        (data.get("appid") == settings.WECHAT_APPID, "appid 不匹配"),
        (data.get("mchid") == settings.WECHAT_MCHID, "mchid 不匹配"),
        (data.get("out_trade_no") == order.out_trade_no, "商户订单号不匹配"),
        (payer.get("openid") == order.openid, "付款人 openid 不匹配"),
        (amount.get("total") == order.amount_cents, "支付金额不匹配"),
        (amount.get("currency", "CNY") == "CNY", "支付币种不匹配"),
    )
    for valid, message in checks:
        if not valid:
            return message
    return ""


@router.post("/create_order", response_model=PayOrderOut)
async def create_pay_order(
    req: PayOrderRequest,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """统一下单接口 — 返回前端拉起支付所需参数"""
    openid = require_openid_match(req.openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
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
            openid=openid,
            amount_cents=settings.VIP_PRICE_CENTS,
            status="created",
        )
        db.add(order)
        db.commit()

        params = await wechat_pay.create_order(openid=openid, out_trade_no=out_trade_no)
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

        validation_error = _validate_paid_order(data, order)
        if validation_error:
            logger.error(f"订单 {out_trade_no} 回调校验失败: {validation_error}")
            return {"code": "FAIL", "message": "订单信息校验失败"}

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
async def cancel_pay_order(
    req: PayCancelRequest,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """前端支付取消后回写订单状态，用于付费漏斗分析。"""
    openid = require_openid_match(req.openid, trusted_openid)
    order = db.query(PayOrder).filter(
        PayOrder.out_trade_no == req.out_trade_no,
        PayOrder.openid == openid,
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status == "created":
        order.status = "cancelled"
        db.commit()
    return {"success": True, "status": order.status}


@router.get("/status/{openid}", response_model=PayStatusOut)
async def get_pay_status(
    openid: str,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """查询用户 VIP 状态"""
    openid = require_openid_match(openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user = normalize_vip_status(db, user)

    latest_order = db.query(PayOrder).filter(
        PayOrder.user_id == user.id
    ).order_by(PayOrder.created_at.desc()).first()

    # The client calls this endpoint after wx.requestPayment succeeds. Reconcile
    # a still-pending order against WeChat so a delayed callback cannot leave a
    # real paid user without VIP access.
    if latest_order and latest_order.status == "created":
        try:
            transaction = await wechat_pay.query_order(latest_order.out_trade_no)
            if transaction.get("trade_state") == "SUCCESS":
                validation_error = _validate_paid_order(transaction, latest_order)
                if validation_error:
                    logger.error(
                        "Paid order %s query validation failed: %s",
                        latest_order.out_trade_no,
                        validation_error,
                    )
                else:
                    latest_order.status = "paid"
                    latest_order.paid_at = datetime.utcnow()
                    user.is_vip = True
                    user.vip_expire_at = datetime.utcnow() + timedelta(days=30)
                    db.commit()
                    logger.info(
                        "User %s VIP activated by order query",
                        user.openid,
                    )
        except Exception as e:
            # A temporary query failure must not make the status endpoint fail.
            # The payment callback remains available as another confirmation path.
            db.rollback()
            logger.warning("Pending order reconciliation failed: %s", e)

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
