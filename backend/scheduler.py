"""三段式主动监督调度器入口

使用 APScheduler 每 5 分钟轮询一次，检查是否有用户
到达其自定义的监督时间点，并生成催促消息。

扩展方向：
- 改为 Celery 异步任务
- 增加 WebSocket 推送
- 增加催促频率可配置
"""

import logging
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import SessionLocal
from services.scheduler_service import run_supervision_cycle, finalize_previous_day_tasks

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def supervision_job():
    """定时监督检查任务"""
    try:
        db = SessionLocal()
        try:
            # 固化前一天未完成任务
            finalized = finalize_previous_day_tasks(db)
            if finalized > 0:
                logger.info(f"已固化 {finalized} 个昨日未完成任务")

            # 运行监督检查
            results = await run_supervision_cycle(db)
            for r in results:
                logger.info(f"催促 [{r['type']}] -> 用户 {r['user_id']}: {r['message'][:50]}...")

        finally:
            db.close()

    except Exception as e:
        logger.error(f"监督任务异常: {e}")


def start_scheduler():
    """启动调度器"""
    scheduler.add_job(supervision_job, "interval", minutes=5, id="supervision")
    scheduler.start()
    logger.info("监督调度器已启动 (每5分钟检查)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_scheduler()
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        scheduler.shutdown()
