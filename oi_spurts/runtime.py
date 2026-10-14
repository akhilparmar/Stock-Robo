"""Scheduler for the OI-spurts tester. It does not touch the other schedulers."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from oi_spurts import db
from oi_spurts.engine import engine

logger = logging.getLogger("oi_spurts")
_scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def startup() -> None:
    db.init_db()
    if _scheduler.running:
        return
    _scheduler.add_job(
        engine.tick,
        IntervalTrigger(seconds=20),
        id="oi_spurts_paper",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("OI spurts paper tester ready (own database, paper only)")


def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
