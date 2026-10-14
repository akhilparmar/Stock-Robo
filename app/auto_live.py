"""Auto-pick best heatmap TA stock and paper-buy nearest ATM option on Live tab."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app import db
from app.neo_client import NeoError, neo_client
from app.nse_heatmap import NseHeatmapError, suggest_extreme_stocks
from app.paper_broker import app_state, execute_paper_buy
from app.slots import DEFAULT_SLOT, EXIT_PCT, is_immediate, normalize_slot

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Only enter when TA score is at least this strong.
MIN_SCORE = 60.0
# New Live entries only inside this IST window (EOD exit is 15:10).
ENTRY_START = (9, 15)
ENTRY_END = (15, 0)
LIVE_SLOT = DEFAULT_SLOT
# Background auto-scan cooldown (minutes) so we don't spam Neo/NSE.
AUTO_COOLDOWN_MIN = 20

_lock = threading.RLock()
_last_auto_at: datetime | None = None
_last_auto_result: dict[str, Any] | None = None
_auto_enabled: bool = True


class AutoLiveError(Exception):
    pass


def _minutes_now() -> int:
    now = datetime.now(IST)
    return now.hour * 60 + now.minute


def _in_entry_window() -> bool:
    now_m = _minutes_now()
    start = ENTRY_START[0] * 60 + ENTRY_START[1]
    end = ENTRY_END[0] * 60 + ENTRY_END[1]
    return start <= now_m <= end


def _side_to_option(side: str, bias: str | None = None) -> str:
    """
    Heatmap TA bias:
    - loser / bounce → call (expect recovery)
    - winner / continuation → call (expect upside to continue)
    Put only if side is explicitly bearish fade (not used by current TA labels).
    """
    b = (bias or "").lower()
    if b == "fade" or side == "short":
        return "PE"
    return "CE"


def pick_best_candidate(suggest: dict[str, Any]) -> dict[str, Any]:
    selected = suggest.get("selected_stock") or suggest.get("selected")
    if not selected and suggest.get("ta"):
        selected = suggest["ta"].get("selected")
    if not selected or not selected.get("ok"):
        raise AutoLiveError("No TA-qualified stock from heatmap right now")
    score = float(selected.get("score") or 0)
    if score < MIN_SCORE:
        raise AutoLiveError(
            f"Best score is {score:g} ({selected.get('symbol')}) — need at least {MIN_SCORE:g} to auto-buy"
        )
    return selected


def _build_plan(option_ltp: float | None, spot: float, atm_strike: float, option_type: str) -> dict[str, Any]:
    from app.slots import TRAIL_STEP_PCT

    exit_pct = float(EXIT_PCT)
    step = float(TRAIL_STEP_PCT)
    entry = float(option_ltp) if option_ltp is not None else None
    tp = round(entry * (1 + exit_pct / 100.0), 4) if entry else None
    sl = round(entry * (1 - exit_pct / 100.0), 4) if entry else None
    return {
        "spot": spot,
        "atm_strike": atm_strike,
        "option_type": option_type,
        "entry_option_ltp": entry,
        "tp_pct": exit_pct,
        "sl_pct": exit_pct,
        "tp_price": tp,
        "sl_price": sl,
        "trail_step_pct": step,
        "risk_reward": "trail after +5%",
        "exit_rule": (
            f"Hard SL -{exit_pct:g}%. At +{exit_pct:g}% start trail: "
            f"lock SL, raise target +{step:g}% each rung. EOD 15:10."
        ),
        "rationale": (
            f"ATM {option_type} near spot {spot:.2f} (strike {atm_strike:g}). "
            f"No hard take-profit — after +{exit_pct:g}% trail by +{step:g}% steps."
        ),
    }


def set_auto_enabled(enabled: bool) -> bool:
    global _auto_enabled
    with _lock:
        _auto_enabled = bool(enabled)
        return _auto_enabled


def auto_status() -> dict[str, Any]:
    db.reopen_incomplete_exits(LIVE_SLOT)
    pending = db.list_incomplete_exits(LIVE_SLOT)
    with _lock:
        return {
            "enabled": _auto_enabled,
            "min_score": MIN_SCORE,
            "entry_window": f"{ENTRY_START[0]:02d}:{ENTRY_START[1]:02d}-{ENTRY_END[0]:02d}:{ENTRY_END[1]:02d}",
            "in_window": _in_entry_window(),
            "cooldown_min": AUTO_COOLDOWN_MIN,
            "last_auto_at": _last_auto_at.isoformat(timespec="seconds") if _last_auto_at else None,
            "last_auto_result": _last_auto_result,
            "open_live_trades": len(pending),
            "pending_exits": pending,
            "finishing_pending_first": bool(pending),
        }


def auto_live_paper_buy(*, max_pct: float = 3.0, force: bool = False) -> dict[str, Any]:
    """
    Heatmap + TA → best stock → ATM CE/PE → immediate paper buy on Live tab.
    """
    global _last_auto_at, _last_auto_result

    slot_id = normalize_slot(LIVE_SLOT)
    if not is_immediate(slot_id):
        raise AutoLiveError("Auto-buy is only for the Live · multi tab")

    if not force and not _in_entry_window():
        raise AutoLiveError("Outside entry window (09:15–15:00 IST)")

    # Always finish the same pending/overnight position before any new buy.
    db.reopen_incomplete_exits(slot_id)
    pending = db.list_incomplete_exits(slot_id)
    if pending:
        p = pending[0]
        raise AutoLiveError(
            f"Finishing pending #{p['id']} "
            f"({p.get('option_trd_symbol') or 'option'} from {p.get('trade_date')}) first — no new buy"
        )
    if db.list_armed_trades(slot_id):
        raise AutoLiveError("A buy is already in progress")

    try:
        suggest = suggest_extreme_stocks(max_pct=max_pct)
    except NseHeatmapError as exc:
        raise AutoLiveError(str(exc)) from exc

    pick = pick_best_candidate(suggest)
    symbol = str(pick["symbol"]).upper()
    side = str(pick.get("side") or "winner")
    bias = pick.get("bias") or ("bounce" if side == "loser" else "continuation")
    option_type = _side_to_option(side, bias)

    try:
        atm = neo_client.resolve_atm_pair(symbol, option_type)
    except NeoError as exc:
        raise AutoLiveError(f"ATM option resolve failed for {symbol} {option_type}: {exc}") from exc

    pair = {"stock": atm["stock"], "option": atm["option"]}
    lot = int(pair["option"].get("lot_size") or 1)
    qty = max(1, lot)
    call_name = pair["option"]["call_name"]
    app_state.set_resolved(pair, qty=qty, slot=slot_id, option_name=call_name)

    try:
        trade = app_state.open_immediate_trade(slot_id)
        buy_result = execute_paper_buy(trade_id=int(trade["id"]))
    except ValueError as exc:
        raise AutoLiveError(str(exc)) from exc
    except NeoError as exc:
        raise AutoLiveError(str(exc)) from exc

    if buy_result.get("error"):
        raise AutoLiveError(str(buy_result["error"]))

    bought_trade = buy_result.get("trade") or trade
    option_ltp = None
    if buy_result.get("quotes"):
        option_ltp = buy_result["quotes"].get("option", {}).get("ltp")
    if option_ltp is None:
        option_ltp = bought_trade.get("buy_price")

    plan = _build_plan(
        float(option_ltp) if option_ltp is not None else None,
        float(atm["spot"]),
        float(atm["atm_strike"]),
        option_type,
    )

    result = {
        "ok": True,
        "slot": slot_id,
        "pick": {
            "symbol": symbol,
            "side": side,
            "bias": bias,
            "score": pick.get("score"),
            "pct": pick.get("pct"),
            "reasons": pick.get("reasons") or [],
            "option_type": option_type,
        },
        "plan": plan,
        "spot": atm.get("spot"),
        "atm_strike": atm.get("atm_strike"),
        "call_name": call_name,
        "resolved": pair,
        "qty": qty,
        "lot_size": lot,
        "expiry": pair["option"]["expiry"],
        "trade": bought_trade,
        "quotes": buy_result.get("quotes"),
        "bought": bool(bought_trade.get("status") == "bought"),
        "suggest_sectors": {
            "lowest": suggest.get("lowest_sector"),
            "highest": suggest.get("highest_sector"),
        },
    }
    with _lock:
        _last_auto_at = datetime.now(IST)
        _last_auto_result = {
            "ok": True,
            "at": _last_auto_at.isoformat(timespec="seconds"),
            "symbol": symbol,
            "call_name": call_name,
            "score": pick.get("score"),
            "bought": result["bought"],
        }
    return result


def maybe_auto_live_tick() -> dict[str, Any] | None:
    """Background: if enabled, in window, idle Live tab → try one auto paper buy."""
    global _last_auto_at, _last_auto_result

    with _lock:
        if not _auto_enabled:
            return None
        if _last_auto_at is not None:
            age_min = (datetime.now(IST) - _last_auto_at).total_seconds() / 60.0
            if age_min < AUTO_COOLDOWN_MIN:
                return None

    if not _in_entry_window():
        return None
    slot_id = normalize_slot(LIVE_SLOT)
    db.reopen_incomplete_exits(slot_id)
    if db.list_incomplete_exits(slot_id) or db.list_armed_trades(slot_id):
        return None

    try:
        result = auto_live_paper_buy(force=False)
        logger.info("Auto-live paper buy: %s", result.get("call_name"))
        return result
    except AutoLiveError as exc:
        with _lock:
            _last_auto_at = datetime.now(IST)
            _last_auto_result = {
                "ok": False,
                "at": _last_auto_at.isoformat(timespec="seconds"),
                "error": str(exc),
            }
        logger.info("Auto-live skip: %s", exc)
        return {"ok": False, "skipped": True, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _last_auto_at = datetime.now(IST)
            _last_auto_result = {
                "ok": False,
                "at": _last_auto_at.isoformat(timespec="seconds"),
                "error": str(exc),
            }
        logger.exception("Auto-live failed")
        return {"ok": False, "error": str(exc)}
