"""微信服务 — access_token 管理与订阅消息发送"""

import logging
import time

import httpx

from config import settings

logger = logging.getLogger(__name__)


class WeChatService:
    """微信 API 服务封装"""

    def __init__(self):
        self._access_token = ""
        self._token_expires_at = 0

    async def get_access_token(self) -> str | None:
        """获取 access_token；None 表示网络结果未知，空串表示明确不可用。"""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        if not settings.WECHAT_APPID or not settings.WECHAT_SECRET:
            logger.debug("[WeChatService] appid/secret 未配置，跳过获取 access_token")
            return ""

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.weixin.qq.com/cgi-bin/token",
                    params={
                        "grant_type": "client_credential",
                        "appid": settings.WECHAT_APPID,
                        "secret": settings.WECHAT_SECRET,
                    },
                )
                data = resp.json()
                if "access_token" in data:
                    self._access_token = data["access_token"]
                    self._token_expires_at = time.time() + data.get("expires_in", 7000) - 200
                    return self._access_token
                else:
                    logger.error(f"[WeChatService] 获取 access_token 失败: {data}")
                    return ""
        except Exception as e:
            logger.error(f"[WeChatService] 获取 access_token 异常: {e}")
            return None

    async def send_subscribe_message(
        self, openid: str, template_id: str, data: dict
    ) -> bool | None:
        """发送订阅消息；True=成功，False=明确失败，None=结果未知。"""
        if not settings.WECHAT_APPID or settings.WECHAT_APPID == "touristappid":
            logger.debug("[WeChatService] appid 为占位符，跳过发送订阅消息")
            return False

        token = await self.get_access_token()
        if token is None:
            return None
        if not token:
            return False

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.weixin.qq.com/cgi-bin/message/subscribe/send",
                    params={"access_token": token},
                    json={
                        "touser": openid,
                        "template_id": template_id,
                        "data": data,
                    },
                )
                result = resp.json()
                if result.get("errcode") == 0:
                    logger.info(f"[WeChatService] 订阅消息发送成功: {openid}")
                    return True
                else:
                    logger.warning(f"[WeChatService] 订阅消息发送失败: {result}")
                    return False
        except Exception as e:
            logger.error(f"[WeChatService] 发送订阅消息异常: {e}")
            return None


wechat_service = WeChatService()
