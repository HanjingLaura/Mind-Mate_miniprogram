"""Mind-Mate 后端主入口

扩展方向：
- 增加 WebSocket 支持
- 增加健康检查端点
- 增加中间件链（CORS、认证、日志）
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import engine, Base
from routers import chat, tasks, user, pay, admin

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期 — 启动时建表，关闭时清理"""
    Base.metadata.create_all(bind=engine)
    logger.info("数据库表已初始化")
    yield
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(chat.router)
app.include_router(tasks.router)
app.include_router(user.router)
app.include_router(pay.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    return {"app": "心智同行 Mind-Mate", "version": "0.1.0", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok"}
