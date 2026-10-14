"""Paper loop. Buys ATM calls and puts, then sells those same contracts."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from oi_spurts import db
from oi_spurts.candles import opening_candle, parse_bars, previous_close
from oi_spurts.config import (
    ENTRY_DEADLINE_MINUTE,
    ENTRY_MINUTE,
    FLAT_MINUTE,
    MAX_ENTRIES,
    PREMIUM_STOP_PCT,
    TIME_CUT_MINUTE,
    TOP_N,
    clock_minute,
    is_market_session,
)
from oi_spurts.neo import NeoError, neo
from oi_spurts.nse import NseError, load_board
from oi_spurts.strategy import (
    initial_stop_from_risk,
    match_spurts,
    premium_stop_hit,
    stock_underwater,
    stop_hit,
    top_bands,
    trail_levels,
)
from oi_spurts.ta import Reject, SetupView, evaluate_setup, pick_entries, setup_payload

logger = logging.getLogger("oi_spurts")


class Skip(Exception):
    """This symbol will not be bought today."""


class RetryLater(Exception):
    """Try this symbol again before the entry window closes."""


class Engine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.board: dict[str, Any] = {}
        self.last_error: str | None = None
        self._quotes: dict[str, float] = {}
        self._board_until = 0.0

    def start(self) -> dict[str, Any]:
        with self._lock:
            db.set_paused(False)
            db.set_enabled(True)
            db.set_error(None)
            self.last_error = None
            self._tick(db.now_ist())
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            db.set_paused(True)
            db.set_enabled(False)
            self._mark_open(db.now_ist())
            for trade in db.open_trades():
                self._exit(trade, "stopped")
            return self.status()

    def refresh(self) -> dict[str, Any]:
        with self._lock:
            now = db.now_ist()
            try:
                self._load_board(now)
                self._mark_open(now)
            except (NseError, NeoError) as exc:
                self._fail(str(exc))
            except Exception as exc:  # noqa: BLE001 — show data failures on the page
                logger.exception("oi spurts refresh failed")
                self._fail(str(exc))
            return self.status()

    def paper_today(self) -> dict[str, Any]:
        """Same-day redo only: flatten current paper trades, then rebuy from the 09:20 rules.

        Closes whatever is open, clears today's paper trades, re-checks the real
        09:15–09:20 candle + TA, and buys up to MAX_ENTRIES at the current option
        LTP. Tomorrow still auto-buys only inside the 09:20–09:24 window.
        """
        with self._lock:
            now = db.now_ist()
            if now.weekday() >= 5:
                self._fail("Weekend — no paper entries")
                return self._status()
            if not is_market_session(now):
                self._fail("Market session is closed — redo today's entries only during market hours")
                return self._status()
            db.set_paused(False)
            db.set_enabled(True)
            db.set_error(None)
            self.last_error = None
            self._mark_open(now)
            open_before = db.open_trades()
            for trade in open_before:
                self._exit(trade, "redo_today")
            removed = db.clear_today_paper()
            db.insert_signal(
                None,
                "scan",
                "redo_today",
                (
                    f"Same-day redo: closed {len(open_before)} open trade(s), "
                    f"cleared {removed} paper trade(s), rechecking 09:20 setup"
                ),
            )
            self._scan_and_buy(now)
            self._manage_open(now)
            bought = db.trades_today_count()
            if bought:
                db.insert_signal(
                    None,
                    "scan",
                    "paper_today",
                    f"Redo bought {bought} name(s) with the live TA strategy",
                )
            elif not self.last_error:
                db.insert_signal(
                    None,
                    "scan",
                    "paper_today",
                    "Redo finished — no name passed TA / top-2 score",
                )
            return self._status()

    def tick(self) -> None:
        with self._lock:
            now = db.now_ist()
            self._sync_market_hours(now)
            self._tick(now)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status()

    def _status(self) -> dict[str, Any]:
        db.init_db()
        now = db.now_ist()
        self._sync_market_hours(now)
        if db.open_trades():
            try:
                self._mark_open(now)
            except NeoError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("quote refresh for status failed")
        minute = clock_minute(now)
        state = db.state_row()
        return {
            "ok": True,
            "paper_only": True,
            "name": "OI Spurts Paper",
            "top_n": TOP_N,
            "max_entries": MAX_ENTRIES,
            "enabled": db.enabled(),
            "paused_today": db.paused_today(),
            "market_session": is_market_session(now),
            "entry_open": ENTRY_MINUTE <= minute < ENTRY_DEADLINE_MINUTE and now.weekday() < 5,
            "clock": now.strftime("%H:%M:%S"),
            "scan_status": state.get("scan_status") if state.get("scan_date") == db.today_ist() else None,
            "neo": neo.status(),
            "board": self.board,
            "positions": [_position_view(trade, self._quotes) for trade in db.open_trades()],
            "score": self._score_live(),
            "trades": db.list_trades(40),
            "signals": db.list_signals(20),
            "trades_today": db.trades_today_count(),
            "can_paper_today": (
                is_market_session(now)
                and now.weekday() < 5
                and not db.paused_today()
            ),
            "last_error": self.last_error or state.get("last_error"),
        }

    def _sync_market_hours(self, now: datetime) -> None:
        if db.paused_today():
            if db.enabled():
                db.set_enabled(False)
            return
        if is_market_session(now):
            if not db.enabled():
                db.set_enabled(True)
            return
        if db.enabled() and not db.open_trades():
            db.set_enabled(False)

    def _tick(self, now: datetime) -> None:
        self._manage_open(now)
        if db.paused_today() or not db.enabled():
            return
        if now.weekday() >= 5:
            return
        minute = clock_minute(now)
        if minute >= ENTRY_DEADLINE_MINUTE:
            if not db.entry_closed_today():
                db.mark_scan_closed()
            self._preview(now)
            return
        if minute < ENTRY_MINUTE:
            self._preview(now)
            return
        self._scan_and_buy(now)

    def _preview(self, now: datetime) -> None:
        try:
            self._load_board(now, max_age=60)
            self.last_error = None
            db.set_error(None)
        except (NseError, NeoError) as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("oi spurts preview failed")
            self._fail(str(exc))

    def _scan_and_buy(self, now: datetime) -> None:
        try:
            self._load_board(now, max_age=15)
        except (NseError, NeoError) as exc:
            self._fail(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("oi spurts scan failed")
            self._fail(str(exc))
            return
        self.last_error = None
        db.set_error(None)
        slots_left = MAX_ENTRIES - db.trades_today_count()
        if slots_left <= 0:
            return

        passed: list[SetupView] = []
        for match in self.board.get("matches") or []:
            symbol = str(match["symbol"])
            if db.traded_today(symbol):
                continue
            try:
                passed.append(self._evaluate(match, now))
            except Skip as exc:
                self._note_once(symbol, "skip", "skipped", str(exc))
            except RetryLater as exc:
                self._note_once(symbol, "wait", "waiting", str(exc))
            except NeoError as exc:
                self._note_once(symbol, "wait", "waiting", str(exc))
                self._fail(str(exc))

        ranked = pick_entries(passed, slots_left=slots_left)
        self.board = {
            **self.board,
            "candidates": [setup_payload(view) for view in sorted(passed, key=lambda row: row.score, reverse=True)],
            "picked": [setup_payload(view) for view in ranked],
            "max_entries": MAX_ENTRIES,
        }
        for view in ranked:
            if db.traded_today(view.symbol):
                continue
            if db.trades_today_count() >= MAX_ENTRIES:
                break
            try:
                self._execute_buy(view, now)
            except RetryLater as exc:
                self._note_once(view.symbol, "wait", "waiting", str(exc))
            except NeoError as exc:
                self._note_once(view.symbol, "wait", "waiting", str(exc))
                self._fail(str(exc))

    def _evaluate(self, match: dict[str, Any], now: datetime) -> SetupView:
        symbol = str(match["symbol"])
        stock = neo.resolve_stock(symbol)
        day = now.date()
        bars = parse_bars(
            neo.historical(
                stock["exchange_segment"],
                stock["token"],
                (day - timedelta(days=10)).isoformat(),
                day.isoformat(),
            )
        )
        candle = opening_candle(bars, day)
        if candle is None:
            raise RetryLater(f"{symbol}: the 9:15–9:20 candle is not complete yet")
        prev = match.get("prev_close")
        if prev is None:
            prev = previous_close(bars, day)
        if prev is None:
            raise RetryLater(f"{symbol}: yesterday's close is not available yet")
        try:
            view = evaluate_setup(
                match=match,
                candle=candle,
                prev_close=float(prev),
                bars=bars,
            )
        except Reject as exc:
            raise Skip(f"{symbol}: {exc}") from exc
        if view.risk_distance <= 0:
            raise Skip(f"{symbol}: capped stop left no risk distance")
        return view

    def _execute_buy(self, view: SetupView, now: datetime) -> None:
        symbol = view.symbol
        contract = neo.resolve_atm(symbol, view.option_type)
        premium = neo.ltp(contract["exchange_segment"], contract["token"])
        if premium is None or premium <= 0:
            raise RetryLater(f"{symbol}: option price is not available yet")
        stock = contract["stock"]
        levels = trail_levels(
            contract["option_type"],
            entry=view.candle.close,
            risk=view.risk_distance,
            initial_stop=view.stop_price,
            best_price=view.candle.close,
        )
        debit = round(float(premium) * int(contract["lot_size"]), 2)
        trade_id = db.insert_trade(
            {
                "trade_date": db.today_ist(),
                "symbol": symbol,
                "band": view.band,
                "option_type": contract["option_type"],
                "stock_pct": view.stock_pct,
                "oi_pct": view.oi_pct,
                "strike": contract["strike"],
                "expiry": contract["expiry"],
                "trd_symbol": contract["trd_symbol"],
                "option_token": contract["token"],
                "option_segment": contract["exchange_segment"],
                "stock_token": stock["token"],
                "stock_segment": stock["exchange_segment"],
                "lot_size": contract["lot_size"],
                "buy_premium": float(premium),
                "debit": debit,
                "stock_entry": view.candle.close,
                "candle_open": view.candle.open,
                "candle_close": view.candle.close,
                "stop_price": levels.stop,
                "target_price": levels.target,
                "risk_distance": view.risk_distance,
                "trail_stage": levels.stage,
                "best_price": view.candle.close,
                "mark_premium": float(premium),
                "status": "open",
                "opened_at": now.isoformat(timespec="seconds"),
            }
        )
        self._quotes[contract["token"]] = float(premium)
        self._quotes[stock["token"]] = float(contract["spot"])
        detail = (
            f"Paper buy {symbol} {contract['strike']:g} {contract['option_type']} "
            f"x {contract['lot_size']} @ {premium:g} · debit ₹{debit:,.2f} · "
            f"score {view.score:.1f} · RSI {view.rsi14:.1f} · MACD {view.macd_hist:g} · "
            f"stop {levels.stop:g} · next target {levels.target:g}"
        )
        db.insert_signal(symbol, "buy", "entered", detail)
        logger.info("paper entry id=%s %s", trade_id, detail)

    def _load_board(self, now: datetime, *, max_age: float = 0) -> None:
        if self.board and max_age and time.monotonic() < self._board_until:
            return
        raw = load_board()
        gainers, losers = top_bands(raw["stocks"], TOP_N)
        matches = match_spurts(gainers, losers, raw["spurts"])
        self.board = {
            "as_of": now.isoformat(timespec="seconds"),
            "gainers": gainers,
            "losers": losers,
            "spurts_count": len(raw["spurts"]),
            "matches": matches,
        }
        db.save_snapshot(json.dumps(self.board))
        self._board_until = time.monotonic() + (max_age or 20)

    def _manage_open(self, now: datetime) -> None:
        if not db.open_trades():
            return
        self._mark_open(now)
        minute = clock_minute(now)
        flat = minute >= FLAT_MINUTE or now.weekday() >= 5
        for trade in db.open_trades():
            stock_ltp = self._quotes.get(str(trade["stock_token"]))
            option_ltp = self._quotes.get(str(trade["option_token"]))
            if stock_ltp is None:
                continue
            best = float(trade["best_price"])
            if _better(str(trade["option_type"]), stock_ltp, best):
                best = stock_ltp
            risk = float(trade["risk_distance"])
            planned_stop = initial_stop_from_risk(
                str(trade["option_type"]),
                float(trade["stock_entry"]),
                risk,
            )
            levels = trail_levels(
                str(trade["option_type"]),
                entry=float(trade["stock_entry"]),
                risk=risk,
                initial_stop=planned_stop,
                best_price=best,
            )
            db.update_levels(
                int(trade["id"]),
                stop_price=levels.stop,
                target_price=levels.target,
                trail_stage=levels.stage,
                best_price=best,
                mark_premium=option_ltp if option_ltp is not None else trade.get("mark_premium"),
            )
            trade = {
                **trade,
                "stop_price": levels.stop,
                "target_price": levels.target,
                "trail_stage": levels.stage,
                "best_price": best,
            }
            if flat:
                self._exit(trade, "clock", stock_ltp=stock_ltp, option_ltp=option_ltp)
                continue
            if option_ltp is not None and premium_stop_hit(
                float(trade["buy_premium"]),
                float(option_ltp),
                PREMIUM_STOP_PCT,
            ):
                self._exit(trade, "premium_stop", stock_ltp=stock_ltp, option_ltp=option_ltp)
                continue
            if (
                minute >= TIME_CUT_MINUTE
                and stock_underwater(str(trade["option_type"]), float(trade["stock_entry"]), stock_ltp)
            ):
                self._exit(trade, "time_cut", stock_ltp=stock_ltp, option_ltp=option_ltp)
                continue
            if stop_hit(str(trade["option_type"]), stock_ltp, levels.stop):
                reason = "trailed_stop" if levels.stage >= 1 else "stock_stop"
                self._exit(trade, reason, stock_ltp=stock_ltp, option_ltp=option_ltp)

    def _mark_open(self, now: datetime) -> None:
        trades = db.open_trades()
        if not trades:
            return
        instruments = []
        for trade in trades:
            instruments.append((str(trade["stock_segment"]), str(trade["stock_token"])))
            instruments.append((str(trade["option_segment"]), str(trade["option_token"])))
        try:
            self._quotes.update(neo.ltps(instruments))
        except NeoError as exc:
            self._fail(str(exc))
        stale = [trade for trade in trades if str(trade["trade_date"]) < now.date().isoformat()]
        for trade in stale:
            self._exit(trade, "stale_open")

    def _exit(
        self,
        trade: dict[str, Any],
        reason: str,
        *,
        stock_ltp: float | None = None,
        option_ltp: float | None = None,
    ) -> None:
        if str(trade.get("status") or "open") == "closed":
            return
        if option_ltp is None:
            option_ltp = self._quotes.get(str(trade["option_token"]))
        if option_ltp is None:
            option_ltp = trade.get("mark_premium")
        if option_ltp is None:
            self._fail(f"{trade['symbol']}: exit {reason} is waiting for an option price")
            return
        if stock_ltp is None:
            stock_ltp = self._quotes.get(str(trade["stock_token"]))
        if stock_ltp is None:
            stock_ltp = float(trade["stock_entry"])
        buy = float(trade["buy_premium"])
        lot = int(trade["lot_size"])
        pnl = round((float(option_ltp) - buy) * lot, 2)
        db.close_trade(
            int(trade["id"]),
            sell_premium=float(option_ltp),
            stock_exit=float(stock_ltp),
            reason=reason,
            pnl=pnl,
        )
        db.insert_signal(
            str(trade["symbol"]),
            "sell",
            reason,
            (
                f"Paper sell {trade['symbol']} {trade['strike']:g} {trade['option_type']} "
                f"@ {float(option_ltp):g} · P&L ₹{pnl:,.2f}"
            ),
        )

    def _note_once(self, symbol: str, action: str, reason: str, detail: str) -> None:
        if db.noted(symbol, reason):
            return
        db.insert_signal(symbol, action, reason, detail)

    def _fail(self, message: str) -> None:
        self.last_error = message
        db.set_error(message)
        logger.warning("%s", message)

    def _score_live(self) -> dict[str, Any]:
        base = db.score()
        open_pnl = 0.0
        for trade in db.open_trades():
            ltp = self._quotes.get(str(trade["option_token"]))
            if ltp is None:
                mark = trade.get("mark_premium")
                if mark is None:
                    continue
                ltp = float(mark)
            open_pnl += (float(ltp) - float(trade["buy_premium"])) * int(trade["lot_size"])
        open_pnl = round(open_pnl, 2)
        closed = float(base.get("closed_pnl") if base.get("closed_pnl") is not None else base.get("pnl") or 0)
        debit = float(base.get("debit") or 0)
        today_pnl = round(closed + open_pnl, 2)
        roi_pct = round((today_pnl / debit) * 100.0, 2) if debit > 0 else None
        return {
            **base,
            "open_pnl": open_pnl,
            "today_pnl": today_pnl,
            "roi_pct": roi_pct,
            "pnl": closed,
        }


def _better(option_type: str, price: float, best: float) -> bool:
    if option_type == "CE":
        return price > best
    return price < best


def _position_view(trade: dict[str, Any], quotes: dict[str, float]) -> dict[str, Any]:
    option_ltp = quotes.get(str(trade["option_token"]))
    stock_ltp = quotes.get(str(trade["stock_token"]))
    unrealized = None
    roi_pct = None
    debit = float(trade.get("debit") or 0)
    if option_ltp is not None:
        unrealized = round((float(option_ltp) - float(trade["buy_premium"])) * int(trade["lot_size"]), 2)
        if debit > 0:
            roi_pct = round((unrealized / debit) * 100.0, 2)
    r_now = None
    if stock_ltp is not None and float(trade["risk_distance"]) > 0:
        if trade["option_type"] == "CE":
            r_now = (float(stock_ltp) - float(trade["stock_entry"])) / float(trade["risk_distance"])
        else:
            r_now = (float(trade["stock_entry"]) - float(stock_ltp)) / float(trade["risk_distance"])
    return {
        **trade,
        "stock_ltp": stock_ltp,
        "option_ltp": option_ltp,
        "unrealized": unrealized,
        "roi_pct": roi_pct,
        "r_multiple": r_now,
    }


engine = Engine()
