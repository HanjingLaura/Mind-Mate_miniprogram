"""Mind-Mate 后端配置模块 — 从 .env 统一读取所有环境变量"""

import os
from pathlib import Path
from urllib.parse import quote_plus
from dotenv import load_dotenv

# 加载 backend/.env
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)


def _default_database_url() -> str:
    """Build a CloudBase MySQL URL when no explicit DATABASE_URL is supplied."""
    username = os.getenv("MYSQL_USERNAME", "")
    password = os.getenv("MYSQL_PASSWORD", "")
    address = os.getenv("MYSQL_ADDRESS", "")
    database = os.getenv("MYSQL_DATABASE", "mindmate")
    if username and password and address:
        return (
            f"mysql+pymysql://{quote_plus(username)}:{quote_plus(password)}"
            f"@{address}/{quote_plus(database)}?charset=utf8mb4"
        )
    return "sqlite:///./mind_mate.db"


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Read a bounded integer setting without making startup fail on bad .env input."""
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class Settings:
    """全局配置，所有密钥从环境变量动态读取，禁止硬编码"""

    APP_ENV: str = os.getenv("APP_ENV", "development").strip().lower()

    # 智谱 GLM
    ZHIPU_API_KEY: str = os.getenv("ZHIPU_API_KEY", "")
    ZHIPU_BASE_URL: str = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    ZHIPU_MODEL: str = os.getenv("ZHIPU_MODEL", "glm-4")
    ZHIPU_VISION_MODEL: str = os.getenv("ZHIPU_VISION_MODEL", "glm-4.6v-flash")

    # 阿里云百炼（OpenAI 兼容接口）。auto 会优先走百炼，智谱作为降级。
    DASHSCOPE_API_KEY: str = os.getenv("DASHSCOPE_API_KEY", "") or os.getenv("BAILIAN_API_KEY", "")
    BAILIAN_BASE_URL: str = os.getenv(
        "BAILIAN_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    BAILIAN_MODEL: str = os.getenv("BAILIAN_MODEL", "qwen-plus")
    BAILIAN_VISION_MODEL: str = os.getenv("BAILIAN_VISION_MODEL", "qwen-vl-plus")
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "auto").strip().lower()

    # Agent 主动介入频控，避免模型建议变成无限打扰。
    AGENT_IDLE_NUDGE_MINUTES: int = _env_int("AGENT_IDLE_NUDGE_MINUTES", 120, 15)
    AGENT_MAX_DAILY_INTERVENTIONS: int = _env_int("AGENT_MAX_DAILY_INTERVENTIONS", 3, 0)
    AGENT_RUN_TIMEOUT_SECONDS: int = _env_int("AGENT_RUN_TIMEOUT_SECONDS", 45, 5)
    AGENT_RUN_LEASE_SECONDS: int = _env_int("AGENT_RUN_LEASE_SECONDS", 180, 30)
    AGENT_PROVIDER_COOLDOWN_SECONDS: int = _env_int("AGENT_PROVIDER_COOLDOWN_SECONDS", 30, 0)
    MODEL_REQUEST_TIMEOUT_SECONDS: int = _env_int("MODEL_REQUEST_TIMEOUT_SECONDS", 20, 5)

    # 管理员内测
    ADMIN_SECRET_KEY: str = os.getenv("ADMIN_SECRET_KEY", "")
    ADMIN_BYPASS_ENABLED: bool = os.getenv("ADMIN_BYPASS_ENABLED", "false").lower() == "true"

    # 调度器：生产默认由云定时任务调用内部 cron 入口。
    # 只有本地开发或明确的单实例部署才应开启内嵌调度器。
    EMBEDDED_SCHEDULER_ENABLED: bool = (
        os.getenv("EMBEDDED_SCHEDULER_ENABLED", "false").lower() == "true"
    )
    INTERNAL_CRON_SECRET: str = os.getenv("INTERNAL_CRON_SECRET", "")

    # 微信小程序
    WECHAT_APPID: str = os.getenv("WECHAT_APPID", "")
    WECHAT_SECRET: str = os.getenv("WECHAT_SECRET", "")
    WECHAT_SUBSCRIBE_TEMPLATE_ID: str = os.getenv("WECHAT_SUBSCRIBE_TEMPLATE_ID", "")

    # CloudBase injects X-WX-OPENID. Client-supplied openids are accepted
    # without that trusted header only when local development opts in.
    ALLOW_INSECURE_DEV_OPENID: bool = (
        os.getenv("ALLOW_INSECURE_DEV_OPENID", "false").lower() == "true"
    )

    # 跨端 App 会话签名；登录后前端以 Bearer Token 访问接口。
    # Never ship a deterministic signing secret. Production must provide one;
    # local development falls back to a process-local ephemeral key.
    APP_SECRET: str = os.getenv("APP_SECRET", "").strip()

    # 微信支付 V3
    WECHAT_MCHID: str = os.getenv("WECHAT_MCHID", "")
    WECHAT_APIV3_KEY: str = os.getenv("WECHAT_APIV3_KEY", "")
    WECHAT_CERT_SERIAL_NO: str = os.getenv("WECHAT_CERT_SERIAL_NO", "")
    WECHAT_NOTIFY_URL: str = os.getenv("WECHAT_NOTIFY_URL", "")
    WECHAT_PRIVATE_KEY_PATH: str = os.getenv("WECHAT_PRIVATE_KEY_PATH", "")
    WECHAT_PRIVATE_KEY_B64: str = os.getenv("WECHAT_PRIVATE_KEY_B64", "")
    WECHAT_PAY_PUBLIC_KEY_ID: str = os.getenv("WECHAT_PAY_PUBLIC_KEY_ID", "")
    WECHAT_PAY_PUBLIC_KEY_PATH: str = os.getenv("WECHAT_PAY_PUBLIC_KEY_PATH", "")
    WECHAT_PAY_PUBLIC_KEY_B64: str = os.getenv("WECHAT_PAY_PUBLIC_KEY_B64", "")
    WECHAT_PLATFORM_CERT_PATH: str = os.getenv("WECHAT_PLATFORM_CERT_PATH", "")
    WECHAT_PLATFORM_CERT_B64: str = os.getenv("WECHAT_PLATFORM_CERT_B64", "")

    # 数据库
    DATABASE_URL: str = os.getenv("DATABASE_URL", "") or _default_database_url()

    # 跨天结算边界：凌晨 4:00
    DAY_RESET_HOUR: int = 4

    # VIP 定价（单位：分）
    VIP_PRICE_CENTS: int = 199  # ¥1.99


settings = Settings()
