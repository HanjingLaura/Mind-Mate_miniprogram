"""三段式主动监督调度器入口

使用 APScheduler 每 1 分钟轮询一次，检查是否有用户
到达其自定义的监督时间点，并生成催促消息。

扩展方向：
- 改为 Celery 异步任务
- 增加 WebSocket 推送
- 增加催促频率可配置
"""

import logging
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

from database import SessionLocal, engine
from services.scheduler_service import run_supervision_cycle, finalize_previous_day_tasks

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
SUPERVISION_LOCK_NAME = "mind_mate_supervision_cycle_v1"


async def run_supervision_job_once() -> dict:
    """Run one supervision cycle with a cross-instance MySQL lock.

    ``GET_LOCK`` belongs to a physical MySQL connection, so a dedicated
    autocommit connection holds only the advisory lock.  Business writes use a
    normal independent Session: binding them to a connection that already ran
    ``GET_LOCK`` would leave an outer SQLAlchemy 2 transaction open and could
    roll back every apparent ``db.commit()`` when that connection closes.
    SQLite intentionally skips the distributed lock for local/tests.
    """
    dialect = engine.dialect.name
    acquired = False
    lock_connection = None

    try:
        if dialect == "mysql":
            lock_connection = engine.connect().execution_options(
                isolation_level="AUTOCOMMIT",
            )
            acquired = lock_connection.execute(
                text("SELECT GET_LOCK(:name, 0)"),
                {"name": SUPERVISION_LOCK_NAME},
            ).scalar() == 1
            if not acquired:
                logger.info("监督调度跳过：另一实例正在执行")
                return {"status": "skipped_locked", "finalized": 0, "results": []}

        db = SessionLocal()
        try:
            finalized = finalize_previous_day_tasks(db)
            if finalized > 0:
                logger.info(f"已固化 {finalized} 个昨日未完成任务")

            results = await run_supervision_cycle(db)
            for result in results:
                logger.info(
                    "催促 [%s] -> 用户 %s: %s...",
                    result["type"],
                    result["user_id"],
                    result["message"][:50],
                )
            return {
                "status": "completed",
                "finalized": finalized,
                "results": results,
            }
        finally:
            db.close()
    finally:
        if acquired and lock_connection is not None:
            try:
                lock_connection.execute(
                    text("SELECT RELEASE_LOCK(:name)"),
                    {"name": SUPERVISION_LOCK_NAME},
                )
            except Exception as exc:
                logger.error("释放监督调度锁失败: %s", exc)
        if lock_connection is not None:
            lock_connection.close()


async def supervision_job():
    """定时监督检查任务"""
    try:
        await run_supervision_job_once()
    except Exception as e:
        logger.error(f"监督任务异常: {e}")


def start_scheduler():
    """启动调度器"""
    if scheduler.running:
        return
    scheduler.add_job(
        supervision_job,
        "interval",
        minutes=1,
        id="supervision",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.start()
    logger.info("监督调度器已启动 (每1分钟检查)")


def shutdown_scheduler():
    """关闭调度器"""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("监督调度器已关闭")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_scheduler()
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        scheduler.shutdown()
