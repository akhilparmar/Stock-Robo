"""Paper loop for the Nifty index tester. Buys a call or a put, then closes that same contract."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from index_tester import db
from index_tester.config import (
    FLAT_MINUTE,
    MAX_BUDGET,
    STRIKE_STEP,
    is_market_session,
)
from index_tester.market import parse_candles, prepare
from index_tester.neo import NeoError, neo
from index_tester.strategy import (
    REASONS,
    Decision,
    atm_strike,
    build_trade_plan,
    debit_allowed,
    decide_entry,
    index_stop_hit,
    index_target_hit,
    next_premium_trail,
    premium_stop_hit,
    premium_take_hit,
    premium_trail_hit,
    supertrend_exit_reason,
)

logger = logging.getLogger("index_tester")


class Engine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.snapshot: dict[str, Any] = {}
        self.last_error: str | None = None
        self._view = None
        self._index_ltp: float | None = None
        self._cached: list[Any] | None = None
        self._cache_until = 0.0
        self._cache_key = ""

    def start(self) -> dict[str, Any]:
        """Manual resume. Normal paper mode turns itself on at market open."""
        with self._lock:
            db.set_paused(False)
            db.set_enabled(True)
            db.set_error(None)
            self.last_error = None
            self._tick(force_bar=False, now=db.now_ist())
            return self.status()

    def stop(self) -> dict[str, Any]:
        """Pause paper auto for the rest of today and flatten any open option."""
        with self._lock:
            db.set_paused(True)
            db.set_enabled(False)
            position = db.open_trade()
            if position:
                self._exit(position, "stopped")
            return self.status()

    def refresh(self) -> dict[str, Any]:
        with self._lock:
            try:
                self._load_view(db.now_ist())
                position = db.open_trade()
                if position is not None:
                    self._option_ltp(position)
            except NeoError as exc:
                self._fail(str(exc))
            except Exception as exc:  # noqa: BLE001 — show data failures on the page
                logger.exception("nifty refresh failed")
                self._fail(str(exc))
            return self.status()

    def tick(self) -> None:
        with self._lock:
            now = db.now_ist()
            self._sync_market_hours(now)
            position = db.open_trade()
            if not db.enabled() and position is None:
                return
            self._tick(force_bar=False, now=now)

    def status(self) -> dict[str, Any]:
        db.init_db()
        now = db.now_ist()
        self._sync_market_hours(now)
        position = db.open_trade()
        session = is_market_session(now)
        paused = db.paused_today()
        enabled = db.enabled()
        return {
            "ok": True,
            "paper_only": True,
            "symbol": "NIFTY",
            "max_budget": MAX_BUDGET,
            "enabled": enabled,
            "market_session": session,
            "paused_today": paused,
            "auto_mode": "market_hours",
            "neo": neo.status(),
            "market": self.snapshot,
            "position": _position_view(position, self.snapshot.get("option_ltp")),
            "score": db.score(),
            "trades": db.list_trades(30),
            "signals": db.list_signals(12),
            "last_error": self.last_error or db.state_row().get("last_error"),
        }

    def _sync_market_hours(self, now: datetime) -> None:
        """Paper auto turns on at 09:15 IST. No Start click needed."""
        if db.paused_today():
            if db.enabled():
                db.set_enabled(False)
            return
        if is_market_session(now):
            if not db.enabled():
                db.set_enabled(True)
                logger.info("Nifty paper auto ON for market hours")
            return
        if db.enabled() and db.open_trade() is None:
            db.set_enabled(False)

    def _tick(self, *, force_bar: bool, now: datetime | None = None) -> None:
        now = now or db.now_ist()
        try:
            self._load_view(now)
        except NeoError as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("nifty tick failed")
            self._fail(str(exc))

        position = db.open_trade()
        if position is not None:
            self._manage(position, now)
            return
        if not db.enabled():
            return
        view = self._view
        if view is None or not view.ready or view.bar_close is None:
            bar_ts = view.bar_ts if view is not None else None
            if bar_ts and bar_ts == db.last_bar():
                return
            if bar_ts:
                db.set_last_bar(bar_ts)
            self._record(Decision("hold", "indicators_not_ready", "mixed"), bar_ts)
            return
        if not force_bar and view.bar_ts == db.last_bar():
            return
        db.set_last_bar(view.bar_ts)
        age = now - view.bar_close
        if age > timedelta(minutes=6):
            self._record(Decision("hold", "stale_bar", _bias_name(view)), view.bar_ts)
            return
        minute = view.bar_close.hour * 60 + view.bar_close.minute
        decision = decide_entry(view, minute, position_open=False)
        if decision.action == "buy_ce":
            self._buy("CE", decision.reason)
        elif decision.action == "buy_pe":
            self._buy("PE", decision.reason)
        else:
            self._record(decision, view.bar_ts)

    def _manage(self, position: dict[str, Any], now: datetime) -> None:
        if position["trade_date"] < now.date().isoformat():
            self._exit(position, "stale_open")
            return
        minute = now.hour * 60 + now.minute
        if minute >= FLAT_MINUTE:
            self._exit(position, "clock")
            return
        premium = self._option_ltp(position)
        if premium is not None:
            trail = next_premium_trail(
                float(position["buy_premium"]),
                float(premium),
                peak_premium=position.get("peak_premium"),
                premium_stop=position.get("premium_stop"),
                trail_stage=int(position.get("trail_stage") or 0),
            )
            if trail.changed:
                moved = db.update_premium_trail(
                    int(position["id"]),
                    peak_premium=trail.peak_premium,
                    premium_stop=trail.premium_stop,
                    stage=trail.trail_stage,
                )
                if moved:
                    position = moved
                    if trail.note:
                        db.insert_signal(
                            self.snapshot.get("bar_ts"),
                            "protect",
                            "premium_trail",
                            trail.note,
                        )
            if int(position.get("trail_stage") or 0) >= 1 and premium_trail_hit(
                float(premium), position.get("premium_stop")
            ):
                self._exit(position, "premium_trail", premium=premium)
                return
            if premium_take_hit(float(position["buy_premium"]), float(premium)):
                self._exit(position, "premium_target", premium=premium)
                return
            if int(position.get("trail_stage") or 0) < 1 and premium_stop_hit(
                float(position["buy_premium"]), premium
            ):
                self._exit(position, "premium_stop", premium=premium)
                return
        index_px = self._index_ltp
        stop = position.get("index_stop")
        target = position.get("index_target")
        if (
            index_px is not None
            and target is not None
            and index_target_hit(position["side"], index_px, float(target))
        ):
            self._exit(position, "index_target", premium=premium, index_px=index_px)
            return
        if index_px is not None and stop is not None and index_stop_hit(position["side"], index_px, float(stop)):
            self._exit(position, "index_stop", premium=premium, index_px=index_px)
            return
        view = self._view
        if view is None or not view.ready or view.bar_close is None:
            return
        if now - view.bar_close > timedelta(minutes=6):
            return
        if view.bar_ts == db.last_bar():
            return
        db.set_last_bar(view.bar_ts)
        reason = supertrend_exit_reason(position["side"], view.prev_supertrend_dir, view.supertrend_dir)
        if reason:
            self._exit(position, reason, premium=premium, index_px=index_px)

    def _buy(self, side: str, reason: str) -> None:
        view = self._view
        if view is None:
            return
        try:
            ref = self._strike_reference(view.close)
            strike = atm_strike(ref, STRIKE_STEP)
            contract = neo.resolve_option(side, strike)
            premium = neo.ltp(contract["exchange_segment"], contract["token"])
        except NeoError as exc:
            self._fail(str(exc))
            self._note(view.bar_ts, "hold", "option_ltp_missing")
            db.insert_signal(
                view.bar_ts,
                "hold",
                "buy_failed",
                f"Rules said buy {side}, but the paper fill failed: {exc}",
            )
            return
        if premium is None or premium <= 0:
            self._note(view.bar_ts, "hold", "option_ltp_missing")
            db.insert_signal(
                view.bar_ts,
                "hold",
                "buy_failed",
                f"Rules said buy {side} {strike:g}, but Kotak gave no option price.",
            )
            return
        debit = float(premium) * int(contract["lot_size"])
        if not debit_allowed(premium, contract["lot_size"], MAX_BUDGET):
            text = f"Skipped Nifty {side} {strike:g}. One lot costs ₹{debit:,.0f}, over the ₹{MAX_BUDGET:,.0f} budget."
            db.insert_signal(view.bar_ts, "hold", "over_budget", text)
            self.snapshot = {**self.snapshot, "reason": "over_budget", "reason_text": text}
            return
        index_px = self._index_ltp if self._index_ltp is not None else view.close
        plan = build_trade_plan(side, index_px, view.atr5, view.adx15)
        opened = db.insert_trade(
            {
                "trade_date": view.session_date,
                "side": side,
                "strike": strike,
                "expiry": contract["expiry"],
                "trd_symbol": contract["trd_symbol"],
                "token": contract["token"],
                "exchange_segment": contract["exchange_segment"],
                "lot_size": contract["lot_size"],
                "buy_premium": float(premium),
                "debit": round(debit, 2),
                "index_at_buy": index_px,
                "index_stop": round(plan.stop, 2),
                "index_target": round(plan.target, 2),
                "risk_distance": round(plan.risk_distance, 2),
                "reward_ratio": plan.reward_ratio,
                "market_regime": plan.regime,
                "entry_reason": reason,
                "opened_at": db.now_ist().isoformat(timespec="seconds"),
            }
        )
        self.snapshot = {**self.snapshot, "option_ltp": float(premium)}
        db.insert_signal(
            view.bar_ts,
            "bought",
            reason,
            (
                f"Paper bought {opened['trd_symbol']} @ {premium:g} "
                f"(debit ₹{debit:,.0f}); {plan.regime} market, "
                f"{plan.reward_ratio:g}R target."
            ),
        )
        logger.info("Paper buy %s @ %s", opened["trd_symbol"], premium)

    def _strike_reference(self, fallback: float) -> float:
        """ATM strike from futures LTP. Spot index quotes often fail on Neo."""
        if self._index_ltp:
            return float(self._index_ltp)
        try:
            chart = neo.chart_instrument()
            price = neo.ltp(chart["exchange_segment"], chart["token"])
            if price:
                return float(price)
        except NeoError:
            pass
        return float(fallback)

    def _exit(
        self,
        position: dict[str, Any],
        reason: str,
        premium: float | None = None,
        index_px: float | None = None,
    ) -> None:
        try:
            if premium is None:
                premium = self._option_ltp(position)
            if index_px is None:
                index_px = self._index_ltp
        except NeoError as exc:
            self._fail(str(exc))
            return
        if premium is None:
            self._fail("Option price missing, so the paper position is still open")
            return
        pnl = (float(premium) - float(position["buy_premium"])) * int(position["lot_size"])
        closed = db.close_trade(
            int(position["id"]),
            sell_premium=float(premium),
            index_at_sell=index_px,
            exit_reason=reason,
            pnl=round(pnl, 2),
        )
        self._note(self.snapshot.get("bar_ts"), "exit", reason)
        logger.info("Paper close %s reason=%s pnl=%s", position.get("trd_symbol"), reason, pnl)
        if closed:
            self.snapshot = {**self.snapshot, "option_ltp": float(premium)}

    def _option_ltp(self, position: dict[str, Any]) -> float | None:
        price = neo.ltp(position["exchange_segment"], position["token"])
        if price is not None:
            self.snapshot = {**self.snapshot, "option_ltp": price}
        return price

    def _load_view(self, now: datetime) -> None:
        chart = neo.chart_instrument()
        candles = self._candles(chart)
        view = prepare(parse_candles(candles), now, instrument=str(chart.get("name") or "NIFTY"))
        self._view = view
        self._index_ltp = neo.ltp(chart["exchange_segment"], chart["token"])
        self.last_error = None
        db.set_error(None)
        decision = self._display_decision(view, now)
        payload = _market_payload(view, self._index_ltp, decision)
        self.snapshot = payload
        db.save_snapshot(json.dumps(payload))

    def _display_decision(self, view, now: datetime) -> Decision:
        if not view.ready or view.bar_close is None:
            return Decision("hold", "indicators_not_ready", "mixed")
        minute = view.bar_close.hour * 60 + view.bar_close.minute
        return decide_entry(
            view,
            minute,
            position_open=db.open_trade() is not None,
        )

    def _candles(self, chart: dict[str, Any]) -> list[Any]:
        key = f"{chart['exchange_segment']}|{chart['token']}"
        now = time.monotonic()
        if self._cached is not None and key == self._cache_key and now < self._cache_until:
            return self._cached
        today = date.today()
        rows = neo.historical(
            chart["exchange_segment"],
            chart["token"],
            (today - timedelta(days=12)).isoformat(),
            today.isoformat(),
        )
        self._cached = rows
        self._cache_key = key
        self._cache_until = now + 45
        return rows

    def _record(self, decision: Decision, bar_ts: str | None) -> None:
        self._note(bar_ts, decision.action, decision.reason)
        self.snapshot = {
            **self.snapshot,
            "bias": decision.bias,
            "reason": decision.reason,
            "reason_text": decision.text,
        }

    def _note(self, bar_ts: str | None, action: str, reason: str) -> None:
        db.insert_signal(bar_ts, action, reason, REASONS.get(reason, reason))

    def _fail(self, message: str) -> None:
        self.last_error = message
        db.set_error(message)
        logger.warning("index tester: %s", message)


def _bias_name(view) -> str:
    if not view.ready:
        return "mixed"
    if view.close > view.vwap and view.ema9 > view.ema21 and view.ema9_15 > view.ema21_15:
        return "call"
    if view.close < view.vwap and view.ema9 < view.ema21 and view.ema9_15 < view.ema21_15:
        return "put"
    return "mixed"


def _market_payload(view, ltp: float | None, decision: Decision) -> dict[str, Any]:
    def r(value: float) -> float | None:
        if value is None:
            return None
        return round(float(value), 2)

    return {
        "ready": view.ready,
        "instrument": view.instrument,
        "session_date": view.session_date,
        "bar_ts": view.bar_ts,
        "ltp": r(ltp) if ltp is not None else r(view.close) if view.close else None,
        "close": r(view.close),
        "vwap": r(view.vwap),
        "ema9": r(view.ema9),
        "ema21": r(view.ema21),
        "ema9_15": r(view.ema9_15),
        "ema21_15": r(view.ema21_15),
        "adx15": r(view.adx15),
        "atr5": r(view.atr5),
        "supertrend": "up" if view.supertrend_dir == 1 else "down" if view.supertrend_dir == -1 else None,
        "orb_high": r(view.orb_high) if view.orb_high is not None else None,
        "orb_low": r(view.orb_low) if view.orb_low is not None else None,
        "cpr_top": r(view.cpr_top),
        "cpr_bottom": r(view.cpr_bottom),
        "bias": decision.bias,
        "reason": decision.reason,
        "reason_text": decision.text,
    }


def _position_view(position: dict[str, Any] | None, option_ltp: float | None) -> dict[str, Any] | None:
    if not position:
        return None
    buy = float(position["buy_premium"])
    lot = int(position["lot_size"])
    pnl = None
    if option_ltp is not None:
        pnl = round((float(option_ltp) - buy) * lot, 2)
    return {
        "id": position["id"],
        "side": position["side"],
        "strike": position["strike"],
        "expiry": position["expiry"],
        "trd_symbol": position["trd_symbol"],
        "lot_size": lot,
        "buy_premium": buy,
        "debit": position["debit"],
        "index_at_buy": position.get("index_at_buy"),
        "index_stop": position.get("index_stop"),
        "index_target": position.get("index_target"),
        "risk_distance": position.get("risk_distance"),
        "reward_ratio": position.get("reward_ratio"),
        "market_regime": position.get("market_regime"),
        "breakeven_moved": bool(position.get("breakeven_moved")),
        "trail_stage": int(position.get("trail_stage") or 0),
        "peak_premium": position.get("peak_premium"),
        "premium_stop": position.get("premium_stop"),
        "entry_reason": REASONS.get(position.get("entry_reason") or "", position.get("entry_reason")),
        "opened_at": position["opened_at"],
        "ltp": option_ltp,
        "pnl": pnl,
    }


engine = Engine()
