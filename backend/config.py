"""Mind-Mate 后端配置模块 — 从 .env 统一读取所有环境变量"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 加载 backend/.env
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)


class Settings:
    """全局配置，所有密钥从环境变量动态读取，禁止硬编码"""

    # 智谱 AI
    ZHIPU_API_KEY: str = os.getenv("ZHIPU_API_KEY", "")
    ZHIPU_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    ZHIPU_MODEL: str = "glm-4-flash"

    # 管理员内测
    ADMIN_SECRET_KEY: str = os.getenv("ADMIN_SECRET_KEY", "")

    # 微信小程序
    WECHAT_APPID: str = os.getenv("WECHAT_APPID", "")
    WECHAT_SECRET: str = os.getenv("WECHAT_SECRET", "")

    # 微信支付 V3
    WECHAT_MCHID: str = os.getenv("WECHAT_MCHID", "")
    WECHAT_APIV3_KEY: str = os.getenv("WECHAT_APIV3_KEY", "")
    WECHAT_CERT_SERIAL_NO: str = os.getenv("WECHAT_CERT_SERIAL_NO", "")
    WECHAT_NOTIFY_URL: str = os.getenv("WECHAT_NOTIFY_URL", "")

    # 数据库
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./mind_mate.db")

    # 跨天结算边界：凌晨 4:00
    DAY_RESET_HOUR: int = 4

    # VIP 定价（单位：分）
    VIP_PRICE_CENTS: int = 199  # ¥1.99


settings = Settings()
