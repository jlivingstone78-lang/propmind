import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start(poll_fn, interval_minutes: int = 5):
    global _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        poll_fn,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="gmail_poll",
        name="Poll Gmail inbox",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started — polling every %d minutes", interval_minutes)


def stop():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
