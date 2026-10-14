"""Pure rules for the OI-spurts paper strategy.

A name must be in the top 5 Nifty 200 gainers or the top 5 losers, and also on
the NSE OI-spurts list. The first 5-minute candle has to agree with that side,
and the open must gap with yesterday's close (above for a gainer, below for a
loser).
The paper order is one ATM call for a gainer, or one ATM put for a loser.
The stop starts at the candle open. The first target is the same distance
(1:1). After each new 1R is reached the stop locks the previous R and the
target steps one R further. The option is sold only when the stock comes back
to that stop, or at the session flat.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Trail:
    stage: int
    stop: float
    target: float
    r_multiple: float


def top_bands(stocks: list[dict[str, Any]], n: int = 5) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Highest positive % changes, and the most negative % changes."""
    gainers = [row for row in stocks if _pct(row) > 0]
    losers = [row for row in stocks if _pct(row) < 0]
    gainers.sort(key=_pct, reverse=True)
    losers.sort(key=_pct)
    return gainers[:n], losers[:n]


def match_spurts(
    gainers: list[dict[str, Any]],
    losers: list[dict[str, Any]],
    spurts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stocks that are in a top band and also on the OI-spurts list."""
    by_symbol = {str(row.get("symbol") or "").upper(): row for row in spurts}
    found: list[dict[str, Any]] = []
    for band, rows in (("gainer", gainers), ("loser", losers)):
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            spurt = by_symbol.get(symbol)
            if spurt is None:
                continue
            found.append(
                {
                    "symbol": symbol,
                    "band": band,
                    "option_type": "CE" if band == "gainer" else "PE",
                    "stock_pct": _pct(row),
                    "ltp": row.get("ltp"),
                    "prev_close": row.get("prev_close"),
                    "oi_pct": spurt.get("oi_pct"),
                }
            )
    return found


def candle_agrees(band: str, open_: float, close: float) -> bool:
    """Gainer needs a green 5-minute candle. Loser needs a red one."""
    if band == "gainer":
        return float(close) > float(open_)
    if band == "loser":
        return float(close) < float(open_)
    return False


def gap_agrees(band: str, open_: float, prev_close: float) -> bool:
    """Gainer must open above yesterday's close. Loser must open below it."""
    if band == "gainer":
        return float(open_) > float(prev_close)
    if band == "loser":
        return float(open_) < float(prev_close)
    return False


def risk_distance(open_: float, close: float) -> float:
    return abs(float(close) - float(open_))


def capped_stop(
    option_type: str,
    entry: float,
    candle_open: float,
    max_stop_pct: float,
) -> float:
    """Tighter of candle-open stop and a max % stock risk from entry."""
    side = _side(option_type)
    entry = float(entry)
    candle_open = float(candle_open)
    cap = abs(float(max_stop_pct)) / 100.0
    if side == "CE":
        pct_stop = entry * (1.0 - cap)
        # Higher stop = less risk for a call.
        return max(candle_open, pct_stop)
    pct_stop = entry * (1.0 + cap)
    return min(candle_open, pct_stop)


def entry_risk(option_type: str, entry: float, stop: float) -> float:
    side = _side(option_type)
    if side == "CE":
        return max(0.0, float(entry) - float(stop))
    return max(0.0, float(stop) - float(entry))


def initial_stop_from_risk(option_type: str, entry: float, risk: float) -> float:
    side = _side(option_type)
    if side == "CE":
        return float(entry) - float(risk)
    return float(entry) + float(risk)


def trail_levels(
    option_type: str,
    entry: float,
    risk: float,
    initial_stop: float,
    best_price: float,
) -> Trail:
    """Stage 0 keeps the planned stop and a 1R target.

    Each later stage locks the previous R and pushes the target one R ahead.
    Reaching the target does not close the trade.
    """
    side = _side(option_type)
    distance = float(risk)
    if distance <= 0:
        raise ValueError("risk must be positive")
    r_multiple = _profit_r(side, float(best_price), float(entry), distance)
    stage = max(0, math.floor(r_multiple + 1e-9))
    if stage == 0:
        stop = float(initial_stop)
        target = _at_r(side, float(entry), distance, 1)
    else:
        stop = _at_r(side, float(entry), distance, stage - 1)
        target = _at_r(side, float(entry), distance, stage + 1)
    return Trail(stage=stage, stop=stop, target=target, r_multiple=r_multiple)


def stop_hit(option_type: str, price: float, stop: float) -> bool:
    if _side(option_type) == "CE":
        return float(price) <= float(stop)
    return float(price) >= float(stop)


def premium_stop_hit(buy_premium: float, last_premium: float, stop_pct: float) -> bool:
    if buy_premium <= 0:
        return False
    return float(last_premium) <= float(buy_premium) * (1.0 - float(stop_pct) / 100.0)


def stock_underwater(option_type: str, entry: float, price: float) -> bool:
    side = _side(option_type)
    if side == "CE":
        return float(price) < float(entry)
    return float(price) > float(entry)


def _side(option_type: str) -> str:
    side = option_type.strip().upper()
    if side not in {"CE", "PE"}:
        raise ValueError("option_type must be CE or PE")
    return side


def _profit_r(side: str, price: float, entry: float, risk: float) -> float:
    if side == "CE":
        return (float(price) - float(entry)) / float(risk)
    return (float(entry) - float(price)) / float(risk)


def _at_r(side: str, entry: float, risk: float, r_value: float) -> float:
    move = float(risk) * float(r_value)
    if side == "CE":
        return float(entry) + move
    return float(entry) - move


def _pct(row: dict[str, Any]) -> float:
    return float(row.get("pct") or 0)
