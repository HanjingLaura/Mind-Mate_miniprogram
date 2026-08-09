"""微信支付 V3 服务 — 生产就绪版本"""

import time
import uuid
import json
import logging
import base64
import binascii
from typing import Optional
from urllib.parse import quote

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


def _is_placeholder(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return (
        not normalized
        or normalized.startswith("your_")
        or "your-domain" in normalized
        or "example.com" in normalized
        or "localhost" in normalized
    )


class WeChatPayV3:
    """微信小程序支付 V3 接口封装"""

    def __init__(self):
        self.appid = settings.WECHAT_APPID
        self.mchid = settings.WECHAT_MCHID
        self.apiv3_key = settings.WECHAT_APIV3_KEY
        self.cert_serial_no = settings.WECHAT_CERT_SERIAL_NO
        self.notify_url = settings.WECHAT_NOTIFY_URL
        self._private_key = None
        self._wechat_pay_public_key = None
        self._platform_certs = {}

    def is_configured(self) -> tuple[bool, list[str]]:
        """检查支付所需配置是否齐全"""
        required = {
            "WECHAT_APPID": self.appid,
            "WECHAT_MCHID": self.mchid,
            "WECHAT_APIV3_KEY": self.apiv3_key,
            "WECHAT_CERT_SERIAL_NO": self.cert_serial_no,
            "WECHAT_NOTIFY_URL": self.notify_url,
            "WECHAT_PRIVATE_KEY_PATH 或 WECHAT_PRIVATE_KEY_B64": (
                settings.WECHAT_PRIVATE_KEY_PATH or settings.WECHAT_PRIVATE_KEY_B64
            ),
        }
        uses_public_key = any(
            not _is_placeholder(value)
            for value in (
                settings.WECHAT_PAY_PUBLIC_KEY_ID,
                settings.WECHAT_PAY_PUBLIC_KEY_PATH,
                settings.WECHAT_PAY_PUBLIC_KEY_B64,
            )
        )
        if uses_public_key:
            required.update({
                "WECHAT_PAY_PUBLIC_KEY_ID": settings.WECHAT_PAY_PUBLIC_KEY_ID,
                "WECHAT_PAY_PUBLIC_KEY_PATH 或 WECHAT_PAY_PUBLIC_KEY_B64": (
                    settings.WECHAT_PAY_PUBLIC_KEY_PATH
                    or settings.WECHAT_PAY_PUBLIC_KEY_B64
                ),
            })
        else:
            required["WECHAT_PLATFORM_CERT_PATH 或 WECHAT_PLATFORM_CERT_B64"] = (
                settings.WECHAT_PLATFORM_CERT_PATH or settings.WECHAT_PLATFORM_CERT_B64
            )
        missing = [k for k, v in required.items() if _is_placeholder(v)]
        if not _is_placeholder(self.apiv3_key) and len(self.apiv3_key.encode()) != 32:
            missing.append("WECHAT_APIV3_KEY(必须为32字节)")
        if self.notify_url and not self.notify_url.startswith("https://"):
            missing.append("WECHAT_NOTIFY_URL(必须为HTTPS)")
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
        # 微信支付签名覆盖的是实际发送的 HTTP 请求体。必须只序列化一次，
        # 避免签名文本与 httpx 的 json= 再序列化结果不一致。
        body_text = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        headers = self._build_headers("POST", url_path, body_text)

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    "https://api.mch.weixin.qq.com" + url_path,
                    content=body_text.encode("utf-8"),
                    headers=headers,
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                prepay_id = data.get("prepay_id", "")
            except httpx.HTTPStatusError as e:
                # Keep the WeChat business error in server logs for diagnosis.
                # The response body does not contain this application's secrets.
                response_text = (e.response.text or "").strip()[:1000]
                request_id = e.response.headers.get("Request-ID", "")
                logger.error(
                    "WeChat order failed: status=%s request_id=%s response=%s",
                    e.response.status_code,
                    request_id,
                    response_text,
                )
                raise RuntimeError(
                    f"WeChat order failed: HTTP {e.response.status_code}"
                ) from e
            except Exception as e:
                logger.error(f"微信下单失败: {e}")
                raise RuntimeError(f"微信下单失败: {e}") from e

        params = self._build_jsapi_params(prepay_id)
        params["prepay_id"] = prepay_id
        params["out_trade_no"] = out_trade_no
        return params

    async def query_order(self, out_trade_no: str) -> dict:
        """Query an order after the client returns from the payment sheet.

        Payment notifications remain the primary confirmation mechanism. This
        lookup is a safe fallback for delayed notification delivery and is only
        used for local orders that are still pending.
        """
        url_path = (
            f"/v3/pay/transactions/out-trade-no/{quote(out_trade_no, safe='')}"
            f"?mchid={quote(self.mchid, safe='')}"
        )
        headers = self._build_headers("GET", url_path)
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    "https://api.mch.weixin.qq.com" + url_path,
                    headers=headers,
                    timeout=10,
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                response_text = (e.response.text or "").strip()[:1000]
                request_id = e.response.headers.get("Request-ID", "")
                logger.error(
                    "WeChat order query failed: status=%s request_id=%s response=%s",
                    e.response.status_code,
                    request_id,
                    response_text,
                )
                raise RuntimeError(
                    f"WeChat order query failed: HTTP {e.response.status_code}"
                ) from e
            except Exception as e:
                logger.error(f"WeChat order query failed: {e}")
                raise RuntimeError(f"WeChat order query failed: {e}") from e

    def _build_headers(self, method: str, canonical_url: str, body_text: str = "") -> dict:
        """构造 API v3 Authorization，并保证签名串与实际请求一致。"""
        timestamp = _get_timestamp()
        nonce_str = _generate_nonce_str()
        sign_message = f"{method}\n{canonical_url}\n{timestamp}\n{nonce_str}\n{body_text}\n"
        signature = self._sign(sign_message)
        headers = {
            "Authorization": f'WECHATPAY2-SHA256-RSA2048 mchid="{self.mchid}",'
            f'nonce_str="{nonce_str}",timestamp="{timestamp}",'
            f'serial_no="{self.cert_serial_no}",signature="{signature}"',
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # 新商户默认使用微信支付公钥模式。声明公钥 ID 后，微信会用对应
        # 公钥对 API 应答与回调进行签名。
        return headers

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

        from cryptography.hazmat.primitives import serialization
        try:
            pem = self._load_pem_bytes(
                settings.WECHAT_PRIVATE_KEY_PATH,
                settings.WECHAT_PRIVATE_KEY_B64,
                "商户私钥",
            )
            self._private_key = serialization.load_pem_private_key(pem, password=None)
        except FileNotFoundError:
            raise RuntimeError(f"商户私钥文件不存在: {settings.WECHAT_PRIVATE_KEY_PATH}")
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

        # 新商户默认使用微信支付公钥；兼容历史平台证书模式。
        try:
            public_key = self._get_callback_public_key(serial_no)
        except Exception as e:
            logger.error(f"加载微信支付验签公钥失败: {e}")
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

    def _get_callback_public_key(self, serial_no: str):
        if not _is_placeholder(settings.WECHAT_PAY_PUBLIC_KEY_ID):
            return self._get_wechat_pay_public_key(serial_no)
        return self._get_platform_cert(serial_no)

    def _get_wechat_pay_public_key(self, public_key_id: str):
        """加载微信支付公钥，用于验证 API v3 回调签名。"""
        expected_id = settings.WECHAT_PAY_PUBLIC_KEY_ID
        if public_key_id != expected_id:
            raise RuntimeError("回调公钥 ID 与本地微信支付公钥 ID 不一致")
        if self._wechat_pay_public_key is not None:
            return self._wechat_pay_public_key

        from cryptography.hazmat.primitives import serialization
        try:
            pem = self._load_pem_bytes(
                settings.WECHAT_PAY_PUBLIC_KEY_PATH,
                settings.WECHAT_PAY_PUBLIC_KEY_B64,
                "微信支付公钥",
            )
            self._wechat_pay_public_key = serialization.load_pem_public_key(pem)
            return self._wechat_pay_public_key
        except FileNotFoundError:
            raise RuntimeError(
                f"微信支付公钥文件不存在: {settings.WECHAT_PAY_PUBLIC_KEY_PATH}"
            )
        except Exception as e:
            raise RuntimeError(f"加载微信支付公钥失败: {e}") from e

    def _get_platform_cert(self, serial_no: str):
        """加载微信平台证书公钥（用于验证回调签名）"""
        if serial_no in self._platform_certs:
            return self._platform_certs[serial_no]

        from cryptography import x509
        try:
            pem = self._load_pem_bytes(
                settings.WECHAT_PLATFORM_CERT_PATH,
                settings.WECHAT_PLATFORM_CERT_B64,
                "微信支付平台证书",
            )
            cert = x509.load_pem_x509_certificate(pem)
            actual_serial_no = format(cert.serial_number, "X")
            if actual_serial_no.upper() != serial_no.upper():
                raise RuntimeError(
                    "回调证书序列号与本地微信支付平台证书不一致"
                )
            public_key = cert.public_key()
            self._platform_certs[serial_no] = public_key
            return public_key
        except FileNotFoundError:
            raise RuntimeError(f"平台证书文件不存在: {settings.WECHAT_PLATFORM_CERT_PATH}")
        except Exception as e:
            raise RuntimeError(f"加载平台证书失败: {e}") from e

    @staticmethod
    def _load_pem_bytes(path: str, encoded: str, label: str) -> bytes:
        """优先从 base64 环境变量加载，适合无状态云容器；也兼容本地文件。"""
        if not _is_placeholder(encoded):
            try:
                return base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as e:
                raise RuntimeError(f"{label} base64 格式无效") from e
        if path:
            with open(path, "rb") as f:
                return f.read()
        raise RuntimeError(f"{label}未配置")

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
