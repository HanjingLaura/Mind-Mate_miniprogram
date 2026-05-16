"""微信支付 V3 服务

扩展方向：
- 增加退款接口
- 增加订单查询
- 支持 VIP 订阅续费
"""

import time
import uuid
import json
import logging
import hashlib
import base64
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


def _generate_nonce_str() -> str:
    return uuid.uuid4().hex[:32]


def _get_timestamp() -> str:
    return str(int(time.time()))


class WeChatPayV3:
    """微信小程序支付 V3 接口封装"""

    def __init__(self):
        self.appid = settings.WECHAT_APPID
        self.mchid = settings.WECHAT_MCHID
        self.apiv3_key = settings.WECHAT_APIV3_KEY
        self.cert_serial_no = settings.WECHAT_CERT_SERIAL_NO
        self.notify_url = settings.WECHAT_NOTIFY_URL

    async def create_order(self, openid: str, description: str = "心智同行VIP体验包") -> dict:
        """统一下单接口 — JSAPI 下单

        Returns:
            前端拉起支付所需的全部参数
        """
        out_trade_no = f"MM{int(time.time() * 1000)}{_generate_nonce_str()[:6]}"
        total = settings.VIP_PRICE_CENTS

        body = {
            "appid": self.appid,
            "mchid": self.mchid,
            "description": description,
            "out_trade_no": out_trade_no,
            "notify_url": self.notify_url,
            "amount": {"total": total, "currency": "CNY"},
            "payer": {"openid": openid},
        }

        # 构造签名并发起请求
        url_path = "/v3/pay/transactions/jsapi"
        timestamp = _get_timestamp()
        nonce_str = _generate_nonce_str()

        # V3 签名消息
        sign_message = f"POST\n{url_path}\n{timestamp}\n{nonce_str}\n{json.dumps(body, ensure_ascii=False)}\n"
        signature = self._sign(sign_message)

        headers = {
            "Authorization": f'WECHATPAY2-SHA256-RSA2048 mchid="{self.mchid}",'
            f'nonce_str="{nonce_str}",timestamp="{timestamp}",'
            f'serial_no="{self.cert_serial_no}",signature="{signature}"',
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    "https://api.mch.weixin.qq.com" + url_path,
                    json=body,
                    headers=headers,
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                prepay_id = data.get("prepay_id", "")
            except Exception as e:
                logger.error(f"微信下单失败: {e}")
                raise RuntimeError(f"微信下单失败: {e}") from e

        # 生成前端 wx.requestPayment 所需参数
        return self._build_jsapi_params(prepay_id)

    def _build_jsapi_params(self, prepay_id: str) -> dict:
        """根据 prepay_id 构造前端支付参数"""
        timestamp = _get_timestamp()
        nonce_str = _generate_nonce_str()
        package = f"prepay_id={prepay_id}"

        # 签名消息
        sign_message = f"{self.appid}\n{timestamp}\n{nonce_str}\n{package}\n"
        pay_sign = self._sign(sign_message)

        return {
            "time_stamp": timestamp,
            "nonce_str": nonce_str,
            "package": package,
            "sign_type": "RSA",
            "pay_sign": pay_sign,
        }

    def _sign(self, message: str) -> str:
        """SHA256-RSA2048 签名

        注意：生产环境需要加载商户 API 证书私钥。
        此处为 MVP 预留，使用环境变量或文件路径加载。
        """
        # MVP 阶段：如果未配置私钥，返回占位签名
        # 生产环境需替换为真正的 RSA 签名
        private_key_path = getattr(settings, "WECHAT_PRIVATE_KEY_PATH", "")
        if not private_key_path:
            logger.warning("未配置商户私钥，使用占位签名（仅限开发测试）")
            return base64.b64encode(hashlib.sha256(message.encode()).digest()).decode()

        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            with open(private_key_path, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)

            signature = private_key.sign(
                message.encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return base64.b64encode(signature).decode()
        except Exception as e:
            logger.error(f"签名失败: {e}")
            raise

    def verify_callback(self, headers: dict, body: bytes) -> Optional[dict]:
        """验证微信支付异步回调签名

        Args:
            headers: 请求头（需含 Wechatpay-Signature 等）
            body: 原始请求体 bytes

        Returns:
            解密后的回调数据 dict，验签失败返回 None
        """
        signature = headers.get("wechatpay-signature", "")
        timestamp = headers.get("wechatpay-timestamp", "")
        nonce = headers.get("wechatpay-nonce", "")

        if not all([signature, timestamp, nonce]):
            logger.warning("回调缺少必要验签头")
            return None

        # 构造验签消息
        sign_message = f"{timestamp}\n{nonce}\n{body.decode()}\n"

        # MVP 阶段：验签逻辑预留
        # 生产环境需加载微信平台证书并验证签名
        try:
            # 解密通知内容
            data = json.loads(body)
            resource = data.get("resource", {})
            ciphertext = resource.get("ciphertext", "")
            nonce = resource.get("nonce", "")
            associated_data = resource.get("associated_data", "")

            if ciphertext and self.apiv3_key:
                decrypted = self._decrypt_aes_gcm(
                    ciphertext, nonce, associated_data, self.apiv3_key
                )
                return json.loads(decrypted)

        except Exception as e:
            logger.error(f"回调验签/解密失败: {e}")
            return None

        return None

    @staticmethod
    def _decrypt_aes_gcm(ciphertext: str, nonce: str, associated_data: str, key: str) -> str:
        """AES-256-GCM 解密微信回调通知"""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key_bytes = key.encode()
        nonce_bytes = nonce.encode()
        ciphertext_bytes = base64.b64decode(ciphertext)
        aesgcm = AESGCM(key_bytes)

        decrypted = aesgcm.decrypt(
            nonce_bytes,
            ciphertext_bytes,
            associated_data.encode() if associated_data else None,
        )
        return decrypted.decode()


wechat_pay = WeChatPayV3()
