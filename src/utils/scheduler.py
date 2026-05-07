"""APScheduler 기반 작업 스케줄러"""
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class AutomationScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    def add_job(self, func, interval_minutes: int, job_id: str):
        self.scheduler.add_job(
            func,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=job_id,
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        logger.info("스케줄 등록: %s (매 %d분)", job_id, interval_minutes)

    def start(self):
        self.scheduler.start()
        logger.info("스케줄러 시작")

    def stop(self):
        self.scheduler.shutdown(wait=False)
        logger.info("스케줄러 종료")
