"""微信支付 V3 服务 — 生产就绪版本"""

import time
import uuid
import json
import logging
import base64
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


def _generate_nonce_str() -> str:
    return uuid.uuid4().hex[:32]


def _get_timestamp() -> str:
    return str(int(time.time()))


def generate_out_trade_no() -> str:
    """生成微信商户订单号。"""
    return f"MM{int(time.time() * 1000)}{_generate_nonce_str()[:6]}"


class WeChatPayV3:
    """微信小程序支付 V3 接口封装"""

    def __init__(self):
        self.appid = settings.WECHAT_APPID
        self.mchid = settings.WECHAT_MCHID
        self.apiv3_key = settings.WECHAT_APIV3_KEY
        self.cert_serial_no = settings.WECHAT_CERT_SERIAL_NO
        self.notify_url = settings.WECHAT_NOTIFY_URL
        self._private_key = None
        self._platform_certs = {}

    def is_configured(self) -> tuple[bool, list[str]]:
        """检查支付所需配置是否齐全"""
        required = {
            "WECHAT_MCHID": self.mchid,
            "WECHAT_APIV3_KEY": self.apiv3_key,
            "WECHAT_CERT_SERIAL_NO": self.cert_serial_no,
            "WECHAT_NOTIFY_URL": self.notify_url,
            "WECHAT_PRIVATE_KEY_PATH": settings.WECHAT_PRIVATE_KEY_PATH,
        }
        missing = [k for k, v in required.items() if not v or v.startswith("your_")]
        return len(missing) == 0, missing

    async def create_order(
        self,
        openid: str,
        out_trade_no: str,
        description: str = "心智同行VIP体验包",
    ) -> dict:
        """统一下单接口 — JSAPI 下单"""
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

        url_path = "/v3/pay/transactions/jsapi"
        timestamp = _get_timestamp()
        nonce_str = _generate_nonce_str()

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

        params = self._build_jsapi_params(prepay_id)
        params["prepay_id"] = prepay_id
        params["out_trade_no"] = out_trade_no
        return params

    def _build_jsapi_params(self, prepay_id: str) -> dict:
        """根据 prepay_id 构造前端支付参数"""
        timestamp = _get_timestamp()
        nonce_str = _generate_nonce_str()
        package = f"prepay_id={prepay_id}"

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
        """SHA256-RSA2048 签名（使用商户私钥）"""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key = self._load_private_key()
        signature = private_key.sign(
            message.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _load_private_key(self):
        """延迟加载并缓存商户私钥"""
        if self._private_key is not None:
            return self._private_key

        path = settings.WECHAT_PRIVATE_KEY_PATH
        if not path:
            raise RuntimeError("WECHAT_PRIVATE_KEY_PATH 未配置，无法签名")

        from cryptography.hazmat.primitives import serialization
        try:
            with open(path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        except FileNotFoundError:
            raise RuntimeError(f"商户私钥文件不存在: {path}")
        except Exception as e:
            raise RuntimeError(f"加载商户私钥失败: {e}") from e

        return self._private_key

    def verify_callback(self, headers: dict, body: bytes) -> Optional[dict]:
        """验证微信支付异步回调签名并解密

        Args:
            headers: 请求头（需含 wechatpay-signature 等）
            body: 原始请求体 bytes

        Returns:
            解密后的回调数据 dict，验签失败返回 None
        """
        signature = headers.get("wechatpay-signature", "")
        timestamp = headers.get("wechatpay-timestamp", "")
        nonce = headers.get("wechatpay-nonce", "")
        serial_no = headers.get("wechatpay-serial", "")

        if not all([signature, timestamp, nonce, serial_no]):
            logger.warning("回调缺少必要验签头")
            return None

        sign_message = f"{timestamp}\n{nonce}\n{body.decode()}\n"

        # 加载平台证书并验证签名
        try:
            public_key = self._get_platform_cert(serial_no)
        except Exception as e:
            logger.error(f"加载平台证书失败: {e}")
            return None

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

        try:
            public_key.verify(
                base64.b64decode(signature),
                sign_message.encode(),
                asym_padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception as e:
            logger.error(f"回调签名验证失败: {e}")
            return None

        # 签名验证通过，解密资源
        try:
            data = json.loads(body)
            resource = data.get("resource", {})
            ciphertext = resource.get("ciphertext", "")
            nonce_val = resource.get("nonce", "")
            associated_data = resource.get("associated_data", "")

            if ciphertext and self.apiv3_key:
                decrypted = self._decrypt_aes_gcm(
                    ciphertext, nonce_val, associated_data, self.apiv3_key
                )
                return json.loads(decrypted)

        except Exception as e:
            logger.error(f"回调解密失败: {e}")
            return None

        return None

    def _get_platform_cert(self, serial_no: str):
        """加载微信平台证书公钥（用于验证回调签名）"""
        if serial_no in self._platform_certs:
            return self._platform_certs[serial_no]

        cert_path = settings.WECHAT_PLATFORM_CERT_PATH
        if not cert_path:
            raise RuntimeError("WECHAT_PLATFORM_CERT_PATH 未配置")

        from cryptography import x509
        try:
            with open(cert_path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            public_key = cert.public_key()
            self._platform_certs[serial_no] = public_key
            return public_key
        except FileNotFoundError:
            raise RuntimeError(f"平台证书文件不存在: {cert_path}")
        except Exception as e:
            raise RuntimeError(f"加载平台证书失败: {e}") from e

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
