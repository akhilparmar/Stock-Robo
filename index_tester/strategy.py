"""Nifty buy-only rules. A call or a put is bought. Options are never written."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from index_tester.config import (
    ADX_MIN,
    ENTRY_WINDOWS,
    PREMIUM_FIRST_LOCK_PCT,
    PREMIUM_STOP_PCT,
    PREMIUM_TAKE_PCT,
    PREMIUM_TRAIL_PCT,
)

REASONS: dict[str, str] = {
    "indicators_not_ready": "Waiting for enough Nifty candles to trust the indicators.",
    "position_open": "A Nifty option is already open. One position at a time.",
    "outside_entry_window": "Outside the entry window (9:30–11:30 and 13:15–14:45 IST).",
    "adx_too_low": "15-minute ADX is below 22, so the index is not trending.",
    "call_above_vwap_ema_orb": "Buy call. Above VWAP, both EMAs up, 5-minute close above the opening range.",
    "call_above_vwap_ema_cpr": "Buy call. Above VWAP, both EMAs up, 5-minute close above the CPR top.",
    "put_below_vwap_ema_orb": "Buy put. Below VWAP, both EMAs down, 5-minute close under the opening range.",
    "put_below_vwap_ema_cpr": "Buy put. Below VWAP, both EMAs down, 5-minute close under the CPR bottom.",
    "no_call_break": "Call bias is on, but price has not cleared the opening range or CPR top.",
    "no_put_break": "Put bias is on, but price has not broken the opening range or CPR bottom.",
    "mixed_trend": "VWAP and the EMAs do not agree, so there is no side to buy.",
    "stale_bar": "The last closed 5-minute bar is too old to enter.",
    "supertrend_flip": "5-minute Supertrend flipped against the open option.",
    "index_stop": "Nifty hit the adaptive ATR stop.",
    "index_target": "Nifty reached the adaptive market-regime target.",
    "premium_target": (
        f"Took a smaller profit: option premium rose {PREMIUM_TAKE_PCT:g}% from the buy price."
    ),
    "premium_trail": (
        f"Trailing stop hit: locked profit, then trailed {PREMIUM_TRAIL_PCT:g}% under the peak premium."
    ),
    "premium_stop": (
        f"Emergency exit: option premium fell {PREMIUM_STOP_PCT:g}% from the buy price."
    ),
    "clock": "Flat at 15:15 IST.",
    "stopped": "Stopped by hand. The open option was closed.",
    "stale_open": "Position was still open after the session, so it was closed.",
    "over_budget": "That contract costs more than the ₹20,000 budget, so it was skipped.",
    "option_ltp_missing": "Kotak did not return an option price, so nothing was bought.",
    "buy_failed": "Rules said buy, but the paper fill did not go through.",
}


@dataclass
class BarView:
    ready: bool
    close: float
    vwap: float
    ema9: float
    ema21: float
    ema9_15: float
    ema21_15: float
    adx15: float
    atr5: float
    supertrend_dir: int
    prev_supertrend_dir: int
    orb_high: float | None
    orb_low: float | None
    cpr_top: float
    cpr_bottom: float
    bar_ts: str
    bar_close: datetime | None = None
    session_date: str = ""
    instrument: str = ""


@dataclass
class Decision:
    action: str
    reason: str
    bias: str

    @property
    def text(self) -> str:
        return REASONS.get(self.reason, self.reason)


@dataclass(frozen=True)
class TradePlan:
    """Risk levels fixed at entry from volatility and trend strength."""

    regime: str
    stop_mult: float
    reward_ratio: float
    risk_distance: float
    stop: float
    target: float


def in_entry_window(minute: int) -> bool:
    return any(start <= minute <= end for start, end in ENTRY_WINDOWS)


def atm_strike(price: float, step: int = 50) -> float:
    return float(step * int((float(price) / step) + 0.5))


def debit_allowed(premium: float, lot_size: int, budget: float = 20_000.0) -> bool:
    return float(premium) * int(lot_size) <= float(budget)


def build_trade_plan(
    side: str,
    index_price: float,
    atr_value: float,
    adx_value: float,
) -> TradePlan:
    """Smaller index targets (0.5R–1R). Premium trail banks early gains."""
    if adx_value < 28:
        regime, stop_mult, reward_ratio = "developing", 0.9, 0.5
    elif adx_value < 40:
        regime, stop_mult, reward_ratio = "trending", 1.1, 0.75
    else:
        regime, stop_mult, reward_ratio = "strong_trend", 1.25, 1.0

    risk = max(float(atr_value) * stop_mult, 1.0)
    if side == "CE":
        stop = float(index_price) - risk
        target = float(index_price) + risk * reward_ratio
    else:
        stop = float(index_price) + risk
        target = float(index_price) - risk * reward_ratio
    return TradePlan(
        regime=regime,
        stop_mult=stop_mult,
        reward_ratio=reward_ratio,
        risk_distance=risk,
        stop=stop,
        target=target,
    )


def index_stop(
    side: str,
    index_price: float,
    atr_value: float,
    mult: float = 1.25,
) -> float:
    """Backward-compatible helper; new trades use build_trade_plan()."""
    distance = float(atr_value) * float(mult)
    if side == "CE":
        return float(index_price) - distance
    return float(index_price) + distance


def index_stop_hit(side: str, index_price: float, stop: float) -> bool:
    if side == "CE":
        return float(index_price) <= float(stop)
    return float(index_price) >= float(stop)


def index_target_hit(side: str, index_price: float, target: float) -> bool:
    if side == "CE":
        return float(index_price) >= float(target)
    return float(index_price) <= float(target)


def breakeven_trigger_hit(
    side: str,
    index_price: float,
    entry: float,
    risk_distance: float,
) -> bool:
    return profit_r(side, index_price, entry, risk_distance) >= 1.0


def profit_r(
    side: str,
    index_price: float,
    entry: float,
    risk_distance: float,
) -> float:
    risk = float(risk_distance)
    if risk <= 0:
        return 0.0
    if side == "CE":
        move = float(index_price) - float(entry)
    else:
        move = float(entry) - float(index_price)
    return move / risk


def trailing_stop_for_stage(
    side: str,
    entry: float,
    risk_distance: float,
    stage: int,
) -> float:
    """Legacy index R trail helper (kept for tests). Live exits use premium trail."""
    locked_r = 0.0 if stage <= 1 else 0.5
    distance = float(risk_distance) * locked_r
    if side == "CE":
        return float(entry) + distance
    return float(entry) - distance


def premium_stop_hit(buy_premium: float, last_premium: float, stop_pct: float = PREMIUM_STOP_PCT) -> bool:
    if buy_premium <= 0:
        return False
    return float(last_premium) <= float(buy_premium) * (1 - stop_pct / 100.0)


def premium_take_hit(
    buy_premium: float,
    last_premium: float,
    take_pct: float = PREMIUM_TAKE_PCT,
) -> bool:
    if buy_premium <= 0:
        return False
    return float(last_premium) >= float(buy_premium) * (1 + take_pct / 100.0)


def premium_trail_hit(last_premium: float, premium_stop: float | None) -> bool:
    if premium_stop is None:
        return False
    return float(last_premium) <= float(premium_stop)


@dataclass(frozen=True)
class PremiumTrailUpdate:
    peak_premium: float
    premium_stop: float | None
    trail_stage: int
    changed: bool
    note: str


def next_premium_trail(
    buy_premium: float,
    last_premium: float,
    *,
    peak_premium: float | None,
    premium_stop: float | None,
    trail_stage: int = 0,
    first_lock_pct: float = PREMIUM_FIRST_LOCK_PCT,
    trail_pct: float = PREMIUM_TRAIL_PCT,
) -> PremiumTrailUpdate:
    """On first green tick lock breakeven; at +1% lock that floor; trail 1% under peak."""
    buy = float(buy_premium)
    last = float(last_premium)
    prev_peak = float(peak_premium) if peak_premium is not None else buy
    peak = max(prev_peak, buy, last)
    stage = int(trail_stage or 0)
    stop = float(premium_stop) if premium_stop is not None else None
    note = ""
    changed = False
    profit_lock = buy * (1 + first_lock_pct / 100.0)

    if stage < 1 and last > buy:
        stop = buy if stop is None else max(stop, buy)
        stage = 1
        changed = True
        note = f"First profit: locked breakeven premium floor at {buy:.2f}."

    if stage >= 1 and peak >= profit_lock - 1e-9:
        if stop is None or profit_lock > stop + 1e-9:
            stop = profit_lock if stop is None else max(stop, profit_lock)
            stage = max(stage, 2)
            changed = True
            note = f"Locked +{first_lock_pct:g}% premium floor at {profit_lock:.2f}."

    if stage >= 1:
        trailed = peak * (1 - trail_pct / 100.0)
        new_stop = trailed if stop is None else max(stop, trailed)
        if stop is None or new_stop > stop + 1e-9:
            stop = new_stop
            if stage >= 2:
                stage = max(stage, 3)
            changed = True
            note = (
                f"Premium peak {peak:.2f}; trailing stop at {stop:.2f} "
                f"(-{trail_pct:g}% under peak)."
            )
        elif peak > prev_peak + 1e-9:
            changed = True
            if not note:
                note = f"Premium peak updated to {peak:.2f}."

    return PremiumTrailUpdate(
        peak_premium=round(peak, 4),
        premium_stop=None if stop is None else round(stop, 4),
        trail_stage=stage,
        changed=changed,
        note=note,
    )


def supertrend_exit_reason(side: str, prev_dir: int, direction: int) -> str | None:
    if side == "CE" and prev_dir == 1 and direction == -1:
        return "supertrend_flip"
    if side == "PE" and prev_dir == -1 and direction == 1:
        return "supertrend_flip"
    return None


def _bias(view: BarView) -> str:
    bull = view.close > view.vwap and view.ema9 > view.ema21 and view.ema9_15 > view.ema21_15
    bear = view.close < view.vwap and view.ema9 < view.ema21 and view.ema9_15 < view.ema21_15
    if bull and not bear:
        return "call"
    if bear and not bull:
        return "put"
    return "mixed"


def decide_entry(
    view: BarView,
    minute: int,
    *,
    position_open: bool,
    adx_min: float = ADX_MIN,
) -> Decision:
    """Many round trips are allowed. Only one open option at a time."""
    if not view.ready:
        return Decision("hold", "indicators_not_ready", "mixed")
    bias = _bias(view)
    if position_open:
        return Decision("hold", "position_open", bias)
    if view.adx15 < adx_min:
        return Decision("hold", "adx_too_low", bias)
    if not in_entry_window(minute):
        return Decision("hold", "outside_entry_window", bias)
    if bias == "call":
        if view.orb_high is not None and view.close > view.orb_high:
            return Decision("buy_ce", "call_above_vwap_ema_orb", "call")
        if view.close > view.cpr_top:
            return Decision("buy_ce", "call_above_vwap_ema_cpr", "call")
        return Decision("hold", "no_call_break", "call")
    if bias == "put":
        if view.orb_low is not None and view.close < view.orb_low:
            return Decision("buy_pe", "put_below_vwap_ema_orb", "put")
        if view.close < view.cpr_bottom:
            return Decision("buy_pe", "put_below_vwap_ema_cpr", "put")
        return Decision("hold", "no_put_break", "put")
    return Decision("hold", "mixed_trend", "mixed")
