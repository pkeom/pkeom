"""APScheduler 기반 작업 스케줄러"""
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class AutomationScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        self._run_now_time: datetime | None = None  # 모든 즉시 실행 job이 공유하는 시각

    def add_job(self, func, interval_minutes: int, job_id: str,
                run_now: bool = True, start_delay_seconds: int = 0):
        """
        run_now=True: start() 직후 첫 실행 후 interval마다 반복.
        start_delay_seconds: 첫 실행을 N초 뒤로 지연 (collector 완료 후 placer 실행 등).
        run_now=False: 첫 interval 경과 후 실행 (기본 APScheduler 동작).
        """
        from datetime import timedelta
        kwargs = {}
        if run_now:
            if self._run_now_time is None:
                self._run_now_time = datetime.now(timezone.utc)
            kwargs["next_run_time"] = self._run_now_time + timedelta(seconds=start_delay_seconds)
        # run_now=False: next_run_time 생략 → APScheduler가 trigger로 첫 실행 시각 계산
        # next_run_time=None을 명시하면 job이 영구 paused 상태가 되므로 절대 넘기지 않음.
        self.scheduler.add_job(
            func,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=job_id,
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=60,
            **kwargs,
        )
        logger.info("스케줄 등록: %s (매 %d분)", job_id, interval_minutes)

    def next_run_times(self) -> dict[str, datetime]:
        """각 job의 다음 실행 시각 반환 (로컬 타임존)"""
        result = {}
        for job in self.scheduler.get_jobs():
            result[job.id] = job.next_run_time
        return result

    def start(self):
        self.scheduler.start()
        logger.info("스케줄러 시작")

    def stop(self):
        self.scheduler.shutdown(wait=True)
        logger.info("스케줄러 종료")
