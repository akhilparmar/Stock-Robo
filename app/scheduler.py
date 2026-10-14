from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import db
from app.auto_live import maybe_auto_live_tick
from app.paper_broker import (
    close_incomplete_exits,
    execute_paper_buy,
    maybe_exit_on_pct,
    resume_pending_priority,
)
from app.slots import SLOTS, is_timed, parse_hhmm

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

scheduler = BackgroundScheduler(timezone=IST)


def _minutes_now() -> int:
    now = datetime.now(IST)
    return now.hour * 60 + now.minute


def start_scheduler() -> None:
    if scheduler.running:
        return

    for slot_id, meta in SLOTS.items():
        if meta.get("entry_mode") != "timed" or not meta.get("buy_time"):
            logger.info(
                "Slot %s entry_mode=%s — no timed buy job",
                slot_id,
                meta.get("entry_mode"),
            )
            continue
        buy_h, buy_m = parse_hhmm(str(meta["buy_time"]))
        scheduler.add_job(
            _safe_buy,
            CronTrigger(day_of_week="mon-fri", hour=buy_h, minute=buy_m, second=0, timezone=IST),
            id=f"paper_buy_{slot_id}",
            kwargs={"slot": slot_id},
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Scheduled slot %s IST buy=%s exit=+/-%s%% eod=%s",
            slot_id,
            meta["buy_time"],
            meta.get("exit_pct"),
            meta.get("eod_exit_time"),
        )

    scheduler.add_job(
        _monitor,
        IntervalTrigger(seconds=10, timezone=IST),
        id="paper_monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _auto_live_scan,
        IntervalTrigger(minutes=5, timezone=IST),
        id="auto_live_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Morning carry-forward: finish yesterday's open stock before new entries.
    scheduler.add_job(
        _morning_resume_pending,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=15, second=5, timezone=IST),
        id="morning_resume_pending",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started for slots: %s + +/-%% monitor + auto-live + morning pending resume",
        ", ".join(SLOTS),
    )


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _safe_buy(slot: str) -> None:
    db.reopen_incomplete_exits(slot)
    pending = db.list_incomplete_exits(slot)
    if pending:
        p = pending[0]
        logger.info(
            "Skip timed buy slot=%s — finishing pending #%s first (%s)",
            slot,
            p.get("id"),
            p.get("option_trd_symbol"),
        )
        resume_pending_priority(slot)
        return
    logger.info("Paper buy trigger slot=%s at %s", slot, datetime.now(IST).isoformat())
    result = execute_paper_buy(slot)
    logger.info("Paper buy result slot=%s: %s", slot, result)


def _morning_resume_pending() -> None:
    logger.info("Morning pending-resume: complete overnight opens before new buys")
    for slot_id in SLOTS:
        result = resume_pending_priority(slot_id)
        if result.get("actions"):
            logger.info("Morning resume slot=%s: %s", slot_id, result)


def _auto_live_scan() -> None:
    # Never scan for a new buy while an incomplete exit still exists.
    for slot_id in SLOTS:
        db.reopen_incomplete_exits(slot_id)
        if db.list_incomplete_exits(slot_id):
            resume_pending_priority(slot_id)
            logger.info("Auto-live deferred — pending exit still open on %s", slot_id)
            return
    result = maybe_auto_live_tick()
    if result and not result.get("skipped"):
        logger.info("Auto-live scan result: %s", result.get("call_name") or result)


_last_watch_log_m: int | None = None


def _monitor() -> None:
    global _last_watch_log_m
    now_m = _minutes_now()
    # Market hours window (IST): until shortly after EOD exit.
    if now_m > 15 * 60 + 30:
        return

    # Before open: keep orphans as bought so we never "forget" them overnight.
    if now_m < 9 * 60:
        for slot_id in SLOTS:
            n = db.reopen_incomplete_exits(slot_id)
            if n:
                logger.info("Pre-open: reopened %s incomplete exit(s) on %s", n, slot_id)
        return

    for slot_id, meta in SLOTS.items():
        # Priority #1 every tick: finish same pending stock (incl. yesterday).
        resume = resume_pending_priority(slot_id)
        if resume.get("actions"):
            for action in resume["actions"]:
                if action.get("ok"):
                    logger.info("Carry/pending exit slot=%s: %s", slot_id, action)
                elif action.get("error"):
                    logger.warning(
                        "Carry exit retry slot=%s trade=%s: %s",
                        slot_id,
                        (action.get("trade") or {}).get("id"),
                        action.get("error"),
                    )

        still_pending = db.list_incomplete_exits(slot_id)

        if is_timed(slot_id) and not still_pending:
            buy_time = meta.get("buy_time")
            if buy_time:
                buy_h, buy_m = parse_hhmm(str(buy_time))
                if now_m >= buy_h * 60 + buy_m:
                    buy_result = execute_paper_buy(slot_id)
                    if not buy_result.get("skipped"):
                        logger.info("Catch-up buy slot=%s: %s", slot_id, buy_result)

        exit_result = maybe_exit_on_pct(slot_id)
        if exit_result.get("ok"):
            for action in exit_result.get("actions") or []:
                if action.get("trail_armed"):
                    logger.info(
                        "Trail armed #%s lock=+%s%% next=+%s%% (chg=%s%%)",
                        action.get("trade_id"),
                        action.get("trail_sl_pct"),
                        action.get("trail_target_pct"),
                        action.get("change_pct"),
                    )
                elif action.get("trail_ratchet"):
                    logger.info(
                        "Trail ratchet #%s lock=+%s%% next=+%s%% (chg=%s%%)",
                        action.get("trade_id"),
                        action.get("trail_sl_pct"),
                        action.get("trail_target_pct"),
                        action.get("change_pct"),
                    )
                elif action.get("ok") and action.get("exit_reason"):
                    logger.info("Pct/trail exit slot=%s: %s", slot_id, action)
                elif action.get("error"):
                    logger.warning("Exit check error slot=%s: %s", slot_id, action.get("error"))
        elif still_pending and (
            _last_watch_log_m is None or now_m - _last_watch_log_m >= 1
        ):
            # Once a minute so the terminal isn't silent while waiting for TP/SL.
            _last_watch_log_m = now_m
            for trade in still_pending:
                logger.info(
                    "Watching open #%s %s buy=%s qty=%s (exit +/-%s%% or EOD %s)",
                    trade.get("id"),
                    trade.get("option_trd_symbol"),
                    trade.get("buy_price"),
                    trade.get("qty"),
                    meta.get("exit_pct"),
                    meta.get("eod_exit_time"),
                )

        eod = meta.get("eod_exit_time")
        if eod:
            eod_h, eod_m = parse_hhmm(str(eod))
            # Only flat-close after EOD — never abandon mid-day; keep retrying live quotes.
            if now_m >= eod_h * 60 + eod_m:
                for eod_result in close_incomplete_exits(
                    slot_id, allow_flat=True, exit_reason="eod"
                ):
                    if not eod_result.get("skipped"):
                        logger.info("EOD/cleanup exit slot=%s: %s", slot_id, eod_result)


def next_run_times(slot: str | None = None) -> dict[str, str | None]:
    from app.slots import normalize_slot

    slot_id = normalize_slot(slot)
    buy_job = scheduler.get_job(f"paper_buy_{slot_id}")
    return {
        "buy": buy_job.next_run_time.isoformat() if buy_job and buy_job.next_run_time else None,
        "sell": None,
        "monitor": "every 10s (pending-first / +/-5% / EOD)",
    }
