from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.neo_client import NeoClient, NeoError

IST = ZoneInfo("Asia/Kolkata")


def _parse_candle_ts(value: str) -> datetime:
    # Neo returns like 2026-09-03T09:20:00+0530
    raw = value.strip()
    if raw.endswith("+0530"):
        raw = raw[:-5] + "+05:30"
    return datetime.fromisoformat(raw)


def _hhmm_to_min(value: str) -> int:
    h, m = map(int, value.split(":"))
    return h * 60 + m


def _first_bar_on_or_after(bars: dict[str, list[Any]], start_min: int, end_min: int) -> tuple[str | None, list[Any] | None]:
    candidates = []
    for key in bars:
        try:
            mins = _hhmm_to_min(key)
        except ValueError:
            continue
        if start_min <= mins <= end_min:
            candidates.append((mins, key))
    if not candidates:
        return None, None
    candidates.sort()
    key = candidates[0][1]
    return key, bars[key]


def backtest_four_minute(
    neo: NeoClient,
    *,
    segment: str,
    token: str,
    trd_symbol: str,
    days: int = 29,
    buy_hhmm: str | None = None,
    sell_hhmm: str | None = None,
    qty: int = 1,
) -> dict[str, Any]:
    """
    Paper backtest for the 4-minute strategy using Neo 1-min candles.

    - Prefer exact 09:20 / 09:24 opens when present
    - Else use first available bar from 09:20..09:30 for buy, then first bar
      about 4 minutes later (up to +10 min) for sell — needed for illiquid options
    """
    settings = get_settings()
    buy_hhmm = buy_hhmm or settings.buy_time
    sell_hhmm = sell_hhmm or settings.sell_time
    buy_start = _hhmm_to_min(buy_hhmm)
    sell_target = _hhmm_to_min(sell_hhmm)
    hold_mins = max(1, sell_target - buy_start)

    todate = date.today()
    fromdate = todate - timedelta(days=max(1, min(days, 29)))
    candles = neo.historical_candles(
        exchange_segment=segment,
        instrument_token=token,
        fromdate=fromdate.isoformat(),
        todate=todate.isoformat(),
        interval="1min",
    )

    by_day: dict[str, dict[str, list[Any]]] = defaultdict(dict)
    for row in candles:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        ts = _parse_candle_ts(str(row[0]))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        else:
            ts = ts.astimezone(IST)
        day = ts.date().isoformat()
        key = f"{ts.hour:02d}:{ts.minute:02d}"
        by_day[day][key] = row

    results: list[dict[str, Any]] = []

    for day in sorted(by_day.keys()):
        bars = by_day[day]
        # Exact preferred
        buy_key = buy_hhmm if buy_hhmm in bars else None
        buy_bar = bars.get(buy_hhmm) if buy_key else None
        if not buy_bar:
            buy_key, buy_bar = _first_bar_on_or_after(bars, buy_start, buy_start + 10)

        if not buy_bar or not buy_key:
            results.append(
                {
                    "date": day,
                    "status": "skipped",
                    "reason": f"No buy candle near {buy_hhmm}",
                }
            )
            continue

        buy_min = _hhmm_to_min(buy_key)
        sell_exact = sell_hhmm if sell_hhmm in bars and _hhmm_to_min(sell_hhmm) > buy_min else None
        if sell_exact:
            sell_key, sell_bar = sell_exact, bars[sell_exact]
        else:
            sell_key, sell_bar = _first_bar_on_or_after(
                bars,
                buy_min + hold_mins,
                buy_min + hold_mins + 10,
            )

        if not sell_bar or not sell_key:
            results.append(
                {
                    "date": day,
                    "status": "skipped",
                    "reason": f"No sell candle ~{hold_mins}m after {buy_key}",
                }
            )
            continue

        buy_price = float(buy_bar[1])
        sell_price = float(sell_bar[1])
        pnl = (sell_price - buy_price) * qty
        results.append(
            {
                "date": day,
                "status": "ok",
                "buy_time_used": buy_key,
                "sell_time_used": sell_key,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "pnl": round(pnl, 4),
            }
        )

    ok_rows = [r for r in results if r["status"] == "ok"]
    wins = sum(1 for r in ok_rows if r["pnl"] > 0)
    losses = sum(1 for r in ok_rows if r["pnl"] < 0)
    flats = sum(1 for r in ok_rows if r["pnl"] == 0)
    total_pnl = round(sum(r["pnl"] for r in ok_rows), 4)

    return {
        "ok": True,
        "instrument": {
            "segment": segment,
            "token": token,
            "trd_symbol": trd_symbol,
        },
        "fromdate": fromdate.isoformat(),
        "todate": todate.isoformat(),
        "buy_time": buy_hhmm,
        "sell_time": sell_hhmm,
        "qty": qty,
        "days_with_data": len(ok_rows),
        "days_skipped": len(results) - len(ok_rows),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": round((wins / len(ok_rows) * 100), 2) if ok_rows else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": round(total_pnl / len(ok_rows), 4) if ok_rows else 0.0,
        "results": results,
        "note": (
            "Uses Neo 1-min candle opens. Exact 09:20/09:24 preferred; "
            "if missing (common on illiquid options), uses next available bar "
            "and ~4 minutes later. FO history is usually only for active contracts."
        ),
    }


def backtest_resolved_option(
    neo: NeoClient,
    resolved: dict[str, Any],
    *,
    days: int = 29,
    qty: int = 1,
    include_stock: bool = True,
) -> dict[str, Any]:
    if not resolved or "option" not in resolved:
        raise NeoError("Resolve an option first")

    option = resolved["option"]
    option_bt = backtest_four_minute(
        neo,
        segment=option["exchange_segment"],
        token=option["token"],
        trd_symbol=option["trd_symbol"],
        days=days,
        qty=qty,
    )

    stock_bt = None
    if include_stock and "stock" in resolved:
        stock = resolved["stock"]
        try:
            stock_bt = backtest_four_minute(
                neo,
                segment=stock["exchange_segment"],
                token=stock["token"],
                trd_symbol=stock["trd_symbol"],
                days=days,
                qty=1,
            )
        except NeoError as exc:
            stock_bt = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "option": option_bt,
        "stock": stock_bt,
    }
