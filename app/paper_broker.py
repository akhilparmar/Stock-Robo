from __future__ import annotations

import threading
from typing import Any

from app import db
from app.neo_client import NeoError, neo_client
from app.slots import EXIT_PCT, TRAIL_STEP_PCT, all_slots, is_immediate, normalize_slot, slot_meta


class SlotState:
    """In-memory day state for one strategy tab/slot."""

    def __init__(self, slot: str) -> None:
        self.slot = normalize_slot(slot)
        self.resolved: dict[str, Any] | None = None
        self.qty: int = 1
        self.armed: bool = False
        self.last_quotes: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.option_name: str | None = None


class AppState:
    """Holds independent state for each strategy slot."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.slots: dict[str, SlotState] = {s["id"]: SlotState(s["id"]) for s in all_slots()}

    def get(self, slot: str | None = None) -> SlotState:
        return self.slots[normalize_slot(slot)]

    def snapshot(self, slot: str | None = None) -> dict[str, Any]:
        with self._lock:
            slot_id = normalize_slot(slot)
            state = self.get(slot_id)
            meta = slot_meta(slot_id)
            exit_pct = float(meta.get("exit_pct") or EXIT_PCT)
            today_trades = db.list_today_trades(slot_id)
            today = today_trades[0] if today_trades else None
            # Include overnight carry (any date still without sell).
            db.reopen_incomplete_exits(slot_id)
            pending_exits = db.list_incomplete_exits(slot_id)
            open_trades = pending_exits or [t for t in today_trades if t.get("status") == "bought"]
            levels = None
            ref = open_trades[0] if open_trades else today
            if ref and ref.get("buy_price") is not None:
                buy = float(ref["buy_price"])
                trail_on = bool(ref.get("trail_active"))
                if trail_on and ref.get("trail_sl_pct") is not None:
                    sl_pct = float(ref["trail_sl_pct"])
                    tp_pct = float(ref.get("trail_target_pct") or (sl_pct + TRAIL_STEP_PCT))
                    levels = {
                        "buy": buy,
                        "tp": round(buy * (1 + tp_pct / 100.0), 4),
                        "sl": round(buy * (1 + sl_pct / 100.0), 4),
                        "exit_pct": exit_pct,
                        "trail_active": True,
                        "trail_sl_pct": sl_pct,
                        "trail_target_pct": tp_pct,
                        "trail_step_pct": TRAIL_STEP_PCT,
                    }
                else:
                    levels = {
                        "buy": buy,
                        "tp": round(buy * (1 + exit_pct / 100.0), 4),
                        "sl": round(buy * (1 - exit_pct / 100.0), 4),
                        "exit_pct": exit_pct,
                        "trail_active": False,
                        "trail_step_pct": TRAIL_STEP_PCT,
                    }
            armed = state.armed or any(t.get("status") == "armed" for t in today_trades)
            carry = None
            if pending_exits:
                p0 = pending_exits[0]
                carry = {
                    "id": p0.get("id"),
                    "trade_date": p0.get("trade_date"),
                    "symbol": p0.get("option_trd_symbol") or p0.get("option_name"),
                    "buy_price": p0.get("buy_price"),
                    "qty": p0.get("qty"),
                    "fail_reason": p0.get("fail_reason"),
                }
            return {
                "connected": neo_client.connected,
                "paper_mode": neo_client.settings.paper_mode,
                "slot": slot_id,
                "slots": all_slots(),
                "entry_mode": meta.get("entry_mode"),
                "buy_time": meta.get("buy_time"),
                "label": meta.get("label"),
                "exit_pct": exit_pct,
                "eod_exit_time": meta.get("eod_exit_time"),
                "exit_mode": f"-{exit_pct:g}% SL · +{exit_pct:g}% then +{TRAIL_STEP_PCT:g}% trail",
                "trail_step_pct": TRAIL_STEP_PCT,
                "pending_exits": pending_exits,
                "pending_carry": carry,
                "today_pnl": db.today_pnl(slot_id),
                "trade_date": db.today_ist(),
                "exit_levels": levels,
                "resolved": state.resolved,
                "qty": state.qty,
                "option_name": state.option_name,
                "armed": armed,
                "today": today,
                "today_trades": today_trades,
                "open_count": len(open_trades),
                "last_quotes": state.last_quotes,
                "last_error": state.last_error,
                "neo": neo_client.status(),
            }

    def set_resolved(
        self,
        pair: dict[str, Any],
        qty: int = 1,
        slot: str | None = None,
        option_name: str | None = None,
    ) -> None:
        with self._lock:
            state = self.get(slot)
            state.resolved = pair
            state.qty = max(1, int(qty))
            state.last_error = None
            if option_name is not None:
                state.option_name = option_name

    def set_quotes(self, quotes: dict[str, Any], slot: str | None = None) -> None:
        with self._lock:
            state = self.get(slot)
            state.last_quotes = quotes
            state.last_error = None

    def set_error(self, message: str, slot: str | None = None) -> None:
        with self._lock:
            self.get(slot).last_error = message

    def _resolved_payload(self, slot_id: str) -> dict[str, Any]:
        state = self.get(slot_id)
        token_ok = bool(neo_client.settings.neo_access_token)
        if not neo_client.connected and not (neo_client.settings.paper_mode and token_ok):
            raise ValueError("NEO_ACCESS_TOKEN missing in .env")
        if not state.resolved:
            raise ValueError("Enter option name first (example: WIPRO call 160)")
        if not neo_client.settings.paper_mode:
            raise ValueError("Only paper mode is supported in v1")
        stock = state.resolved["stock"]
        option = state.resolved["option"]
        return {
            "trade_date": db.today_ist(),
            "slot": slot_id,
            "stock_symbol": stock["symbol"],
            "option_type": option["option_type"],
            "strike": option["strike"],
            "expiry": option["expiry"],
            "stock_token": stock["token"],
            "stock_trd_symbol": stock["trd_symbol"],
            "option_token": option["token"],
            "option_trd_symbol": option["trd_symbol"],
            "qty": state.qty,
        }

    def arm(self, slot: str | None = None) -> dict[str, Any]:
        """Timed slot: upsert today's armed trade."""
        with self._lock:
            slot_id = normalize_slot(slot)
            if is_immediate(slot_id):
                raise ValueError("Live tab buys immediately on save — no separate arm.")
            today = db.get_trade_by_date(db.today_ist(), slot_id)
            if today and today["status"] in {"bought", "completed"}:
                return today
            trade = db.upsert_armed_trade(self._resolved_payload(slot_id))
            self.get(slot_id).armed = True
            return trade

    def open_immediate_trade(self, slot: str | None = None) -> dict[str, Any]:
        """Immediate slot: insert a new armed trade only when none are waiting."""
        with self._lock:
            slot_id = normalize_slot(slot)
            if not is_immediate(slot_id):
                raise ValueError("Only the Live tab supports immediate multi-trade.")
            db.reopen_incomplete_exits(slot_id)
            pending = db.list_incomplete_exits(slot_id)
            if pending:
                p = pending[0]
                raise ValueError(
                    f"Finishing pending #{p['id']} "
                    f"({p.get('option_trd_symbol') or 'option'} from {p.get('trade_date')}) "
                    "before any new buy."
                )
            armed = db.list_armed_trades(slot_id)
            if armed:
                raise ValueError("A buy is already in progress — wait for it to finish.")
            trade = db.insert_armed_trade(self._resolved_payload(slot_id))
            self.get(slot_id).armed = True
            return trade

    def disarm(self, slot: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            slot_id = normalize_slot(slot)
            if is_immediate(slot_id):
                raise ValueError("Live tab has no armed wait — disarm is for 09:40 only.")
            trade = db.disarm_today(slot_id)
            self.get(slot_id).armed = False
            return trade


app_state = AppState()


def _instruments_from_trade(trade: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    stock = {
        "token": trade["stock_token"],
        "trd_symbol": trade["stock_trd_symbol"],
        "exchange_segment": "nse_cm",
    }
    option = {
        "token": trade["option_token"],
        "trd_symbol": trade["option_trd_symbol"],
        "exchange_segment": "nse_fo",
    }
    return stock, option


def execute_paper_buy(
    slot: str | None = None,
    trade_id: int | None = None,
) -> dict[str, Any]:
    if trade_id is not None:
        trade = db.get_trade(trade_id)
    else:
        slot_id = normalize_slot(slot)
        armed = db.list_armed_trades(slot_id)
        trade = armed[0] if armed else None
    if not trade or trade["status"] != "armed":
        return {"skipped": True, "reason": "No armed trade"}
    slot_id = normalize_slot(trade["slot"])
    tid = int(trade["id"])
    stock, option = _instruments_from_trade(trade)
    try:
        quotes = neo_client.get_ltps(stock, option)
        option_ltp = quotes["option"]["ltp"]
        stock_ltp = quotes["stock"]["ltp"]
        if option_ltp is None:
            raise NeoError("Option LTP unavailable at buy time")
        updated = db.mark_bought(
            tid,
            float(option_ltp),
            float(stock_ltp) if stock_ltp is not None else None,
        )
        app_state.set_quotes(quotes, slot_id)
        return {"ok": True, "slot": slot_id, "trade": updated, "quotes": quotes}
    except Exception as exc:  # noqa: BLE001
        failed = db.mark_failed(tid, str(exc))
        app_state.set_error(str(exc), slot_id)
        return {"ok": False, "slot": slot_id, "error": str(exc), "trade": failed}


def execute_paper_sell(
    slot: str | None = None,
    trade_id: int | None = None,
    *,
    exit_reason: str = "manual",
    force: bool = False,
) -> dict[str, Any]:
    if trade_id is not None:
        trade = db.get_trade(trade_id)
    else:
        slot_id = normalize_slot(slot)
        open_trades = db.list_open_trades(slot_id)
        trade = open_trades[0] if open_trades else None
    if not trade or trade["status"] != "bought":
        return {"skipped": True, "reason": "No bought trade"}
    slot_id = normalize_slot(trade["slot"])
    tid = int(trade["id"])
    stock, option = _instruments_from_trade(trade)
    try:
        quotes = neo_client.get_ltps(stock, option)
        option_ltp = quotes["option"]["ltp"]
        stock_ltp = quotes["stock"]["ltp"]
        if option_ltp is None:
            raise NeoError("Option LTP unavailable at sell time")
        buy_price = float(trade["buy_price"] or 0)
        qty = int(trade["qty"] or 1)
        pnl = (float(option_ltp) - buy_price) * qty
        updated = db.mark_sold(
            tid,
            float(option_ltp),
            float(stock_ltp) if stock_ltp is not None else None,
            pnl,
            exit_reason=exit_reason,
        )
        if not db.list_armed_trades(slot_id) and not db.list_open_trades(slot_id):
            app_state.get(slot_id).armed = False
        app_state.set_quotes(quotes, slot_id)
        return {
            "ok": True,
            "slot": slot_id,
            "exit_reason": exit_reason,
            "forced": force,
            "trade": updated,
            "quotes": quotes,
        }
    except Exception as exc:  # noqa: BLE001
        # Keep as bought so monitor/EOD can retry — never leave a filled buy as failed.
        noted = db.note_sell_error(tid, str(exc))
        app_state.set_error(str(exc), slot_id)
        return {"ok": False, "slot": slot_id, "error": str(exc), "trade": noted}


def execute_eod_sells(slot: str | None = None) -> list[dict[str, Any]]:
    slot_id = normalize_slot(slot)
    db.reopen_incomplete_exits(slot_id)
    results = []
    for trade in db.list_incomplete_exits(slot_id):
        results.append(
            execute_paper_sell(trade_id=int(trade["id"]), exit_reason="eod", force=True)
        )
    return results


def close_incomplete_exits(
    slot: str | None = None,
    *,
    allow_flat: bool = True,
    exit_reason: str = "eod",
) -> list[dict[str, Any]]:
    """
    Close any bought (or orphan failed-with-buy) positions — including overnight carry.
    If quotes fail and allow_flat, square at buy price so nothing stays pending forever.
    """
    slot_id = normalize_slot(slot)
    db.reopen_incomplete_exits(slot_id)
    results: list[dict[str, Any]] = []
    for trade in db.list_incomplete_exits(slot_id):
        tid = int(trade["id"])
        reason = exit_reason
        today = db.today_ist()
        if trade.get("trade_date") and str(trade["trade_date"]) < str(today):
            reason = "carry" if exit_reason in {"eod", "carry"} else exit_reason
        result = execute_paper_sell(trade_id=tid, exit_reason=reason, force=True)
        if result.get("ok"):
            results.append(result)
            continue
        if not allow_flat or trade.get("buy_price") is None:
            results.append(result)
            continue
        buy_price = float(trade["buy_price"])
        updated = db.mark_sold(
            tid,
            buy_price,
            trade.get("stock_ltp_at_buy"),
            0.0,
            exit_reason="eod_flat",
        )
        results.append(
            {
                "ok": True,
                "slot": slot_id,
                "exit_reason": "eod_flat",
                "forced": True,
                "trade": updated,
                "note": f"Flat close after sell error: {result.get('error')}",
            }
        )
    return results


def resume_pending_priority(slot: str | None = None) -> dict[str, Any]:
    """
    Next-day priority #1: reopen orphans and finish the same open stock/option
    before any new entry. Uses live quote; does not flat-close (caller may EOD flat).
    """
    slot_id = normalize_slot(slot)
    reopened = db.reopen_incomplete_exits(slot_id)
    pending = db.list_incomplete_exits(slot_id)
    if not pending:
        return {"ok": True, "slot": slot_id, "pending": 0, "reopened": reopened, "actions": []}

    today = db.today_ist()
    actions: list[dict[str, Any]] = []
    for trade in pending:
        tid = int(trade["id"])
        is_carry = bool(trade.get("trade_date") and str(trade["trade_date"]) < str(today))
        if is_carry:
            # Overnight leftover: square with live LTP first thing.
            actions.append(
                execute_paper_sell(trade_id=tid, exit_reason="carry", force=True)
            )
        else:
            # Same-day open: normal TP/SL path handled by maybe_exit_on_pct.
            pass
    return {
        "ok": True,
        "slot": slot_id,
        "pending": len(db.list_incomplete_exits(slot_id)),
        "reopened": reopened,
        "actions": actions,
    }


def maybe_exit_on_pct(slot: str | None = None) -> dict[str, Any]:
    """
    Exit rules per open trade:
    - Hard SL at -EXIT_PCT until trail activates.
    - At +EXIT_PCT: do NOT take profit; start trail ladder.
      SL lock = +5%, next target = +6%; each +1% hit raises both by 1%.
    - In trail mode: exit only if LTP falls to locked SL (or EOD elsewhere).
    """
    slot_id = normalize_slot(slot)
    meta = slot_meta(slot_id)
    exit_pct = float(meta.get("exit_pct") or EXIT_PCT)
    step = float(TRAIL_STEP_PCT)
    db.reopen_incomplete_exits(slot_id)
    open_trades = db.list_open_trades(slot_id)
    if not open_trades:
        return {"skipped": True, "reason": f"No open bought trade ({slot_id})"}

    actions: list[dict[str, Any]] = []
    for trade in open_trades:
        if trade.get("buy_price") is None:
            continue
        buy_price = float(trade["buy_price"])
        if buy_price <= 0:
            continue
        tid = int(trade["id"])
        stock, option = _instruments_from_trade(trade)
        try:
            quotes = neo_client.get_ltps(stock, option)
            option_ltp = quotes["option"]["ltp"]
            if option_ltp is None:
                continue
            app_state.set_quotes(quotes, slot_id)
            ltp = float(option_ltp)
            change_pct = ((ltp - buy_price) / buy_price) * 100.0
            trail_on = bool(trade.get("trail_active"))
            trail_sl = (
                float(trade["trail_sl_pct"])
                if trade.get("trail_sl_pct") is not None
                else None
            )
            trail_tp = (
                float(trade["trail_target_pct"])
                if trade.get("trail_target_pct") is not None
                else None
            )

            if not trail_on:
                if change_pct <= -exit_pct:
                    actions.append(execute_paper_sell(trade_id=tid, exit_reason="sl"))
                    continue
                if change_pct < exit_pct:
                    continue
                # Activate trail at +5% (or higher if quote jumped).
                trail_sl = exit_pct
                trail_tp = exit_pct + step
                while change_pct >= trail_tp - 1e-9:
                    trail_sl = trail_tp
                    trail_tp = trail_sl + step
                db.update_trail(
                    tid,
                    trail_active=True,
                    trail_sl_pct=trail_sl,
                    trail_target_pct=trail_tp,
                )
                actions.append(
                    {
                        "ok": True,
                        "slot": slot_id,
                        "trade_id": tid,
                        "trail_armed": True,
                        "change_pct": round(change_pct, 4),
                        "trail_sl_pct": trail_sl,
                        "trail_target_pct": trail_tp,
                    }
                )
                continue

            # Trail mode: ratchet if new target hit, else exit on locked SL.
            assert trail_sl is not None and trail_tp is not None
            ratcheted = False
            while change_pct >= trail_tp - 1e-9:
                trail_sl = trail_tp
                trail_tp = trail_sl + step
                ratcheted = True
            if ratcheted:
                db.update_trail(
                    tid,
                    trail_active=True,
                    trail_sl_pct=trail_sl,
                    trail_target_pct=trail_tp,
                )
                actions.append(
                    {
                        "ok": True,
                        "slot": slot_id,
                        "trade_id": tid,
                        "trail_ratchet": True,
                        "change_pct": round(change_pct, 4),
                        "trail_sl_pct": trail_sl,
                        "trail_target_pct": trail_tp,
                    }
                )
            # Strict < so sitting exactly on the lock does not instant-exit.
            if change_pct < trail_sl - 1e-9:
                actions.append(
                    execute_paper_sell(trade_id=tid, exit_reason="trail_sl")
                )
        except Exception as exc:  # noqa: BLE001
            app_state.set_error(str(exc), slot_id)
            actions.append({"ok": False, "slot": slot_id, "trade_id": tid, "error": str(exc)})

    if not actions:
        return {
            "skipped": True,
            "reason": "Within band / trailing",
            "slot": slot_id,
            "exit_pct": exit_pct,
            "trail_step_pct": step,
        }
    return {"ok": True, "slot": slot_id, "actions": actions}
