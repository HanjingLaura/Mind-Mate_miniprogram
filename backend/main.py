"""Mind-Mate 后端主入口

扩展方向：
- 增加 WebSocket 支持
- 增加健康检查端点
- 增加中间件链（CORS、认证、日志）
"""

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from database import engine, Base
from routers import chat, tasks, user, pay, admin, telemetry, upload
from config import settings
from scheduler import run_supervision_job_once, start_scheduler, shutdown_scheduler
from services.model_gateway import describe_routes, ModelProviderUnavailable

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def ensure_runtime_schema():
    """补齐开发期 SQLite 老库缺失字段；正式库由 migrations.runner 管理。"""
    if not str(engine.url).startswith("sqlite"):
        return

    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(subscribe_auths)")).fetchall()
        columns = {row[1] for row in rows}
        if rows and "scene" not in columns:
            conn.execute(text("ALTER TABLE subscribe_auths ADD COLUMN scene VARCHAR(64) DEFAULT 'general'"))
        if rows and "used_at" not in columns:
            conn.execute(text("ALTER TABLE subscribe_auths ADD COLUMN used_at DATETIME"))
        message_rows = conn.execute(text("PRAGMA table_info(messages)")).fetchall()
        message_columns = {row[1] for row in message_rows}
        if message_rows and "request_id" not in message_columns:
            conn.execute(text("ALTER TABLE messages ADD COLUMN request_id VARCHAR(128)"))
        if message_rows:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_message_request "
                "ON messages (conversation_id, request_id) WHERE request_id IS NOT NULL"
            ))
        run_rows = conn.execute(text("PRAGMA table_info(agent_runs)")).fetchall()
        run_columns = {row[1] for row in run_rows}
        if run_rows and "lease_token" not in run_columns:
            conn.execute(text("ALTER TABLE agent_runs ADD COLUMN lease_token VARCHAR(64) DEFAULT '' NOT NULL"))
        reminder_rows = conn.execute(text("PRAGMA table_info(scheduled_reminders)")).fetchall()
        reminder_columns = {row[1] for row in reminder_rows}
        if reminder_rows and "processing_token" not in reminder_columns:
            conn.execute(text("ALTER TABLE scheduled_reminders ADD COLUMN processing_token VARCHAR(64) DEFAULT '' NOT NULL"))


def ensure_production_schema():
    """Fail closed when a persistent database has not run versioned migrations."""
    if settings.APP_ENV != "production":
        return
    if str(engine.url).startswith("sqlite"):
        raise RuntimeError("生产环境禁止使用 SQLite，请配置持久化 MySQL DATABASE_URL")
    if not inspect(engine).has_table("schema_migrations"):
        raise RuntimeError("生产数据库未初始化 schema_migrations，请先运行 python -m migrations.runner")
    with engine.connect() as conn:
        versions = {
            row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))
        }
    required = {"001_agent_idempotency", "002_conversation_ownership", "003_reminder_leases"}
    missing = required - versions
    if missing:
        raise RuntimeError(f"生产数据库缺少迁移版本: {', '.join(sorted(missing))}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期 — 启动时建表，关闭时清理"""
    if settings.APP_ENV == "production" and not settings.APP_SECRET:
        raise RuntimeError("生产环境必须配置 APP_SECRET")
    ensure_production_schema()
    if settings.APP_ENV != "production":
        Base.metadata.create_all(bind=engine)
        ensure_runtime_schema()
    logger.info("数据库表已初始化")
    if settings.EMBEDDED_SCHEDULER_ENABLED:
        start_scheduler()
    else:
        logger.info("内嵌监督调度器已关闭；等待云定时任务调用")
    yield
    if settings.EMBEDDED_SCHEDULER_ENABLED:
        shutdown_scheduler()
    logger.info("应用关闭")


app = FastAPI(
    title="心智同行 Mind-Mate API",
    description="对抗拖延的硬核数字损友",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — 允许小程序和开发环境访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(chat.router)
app.include_router(tasks.router)
app.include_router(user.router)
app.include_router(pay.router)
app.include_router(admin.router)
app.include_router(telemetry.router)
app.include_router(upload.router)

_upload_dir = Path(__file__).resolve().parent / "uploads"
_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_upload_dir)), name="uploads")


@app.get("/")
async def root():
    return {"app": "心智同行 Mind-Mate", "version": "0.1.0", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/agent")
async def agent_health():
    """Expose safe runtime capabilities without returning API keys."""
    try:
        model = describe_routes()
    except ModelProviderUnavailable as exc:
        model = {"active": None, "fallbacks": [], "error": str(exc)}
    return {
        "status": "ok",
        "model": model,
        "scheduler": {"embedded": settings.EMBEDDED_SCHEDULER_ENABLED, "interval_minutes": 1},
        "closed_loop": ["understand", "plan", "act", "verify", "reflect"],
    }


@app.post("/internal/cron/supervision")
async def run_supervision_cron(
    x_internal_cron_secret: str = Header("", alias="X-Internal-Cron-Secret"),
):
    """供云定时任务调用的单次监督轮询；不接受查询参数密钥。"""
    expected = settings.INTERNAL_CRON_SECRET
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="内部定时任务未配置")
    if not hmac.compare_digest(x_internal_cron_secret, expected):
        raise HTTPException(status_code=401, detail="无权调用定时任务")

    result = await run_supervision_job_once()
    return {
        "status": result["status"],
        "finalized": result["finalized"],
        "delivered_count": len(result["results"]),
    }
