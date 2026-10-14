"""Scheduler for the Nifty tester. It does not touch the stock-option scheduler."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from index_tester import db
from index_tester.engine import engine

logger = logging.getLogger("index_tester")
_scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def startup_index_tester() -> None:
    db.init_db()
    if _scheduler.running:
        return
    _scheduler.add_job(
        engine.tick,
        IntervalTrigger(seconds=20),
        id="nifty_index_tester",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Nifty index tester ready (paper only, separate database)")


def shutdown_index_tester() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
