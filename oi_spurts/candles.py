"""First cash-session 5-minute candle, built from Kotak 1-minute bars."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))
OPEN = time(9, 15)
LAST_MINUTE = time(9, 19)
CLOSE = time(9, 20)
SESSION_END = time(15, 30)


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class OpeningCandle:
    open: float
    high: float
    low: float
    close: float
    bars: int
    volume: float = 0.0


def parse_bars(rows: list[Any]) -> list[Bar]:
    parsed: list[Bar] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        ts = _parse_ts(row[0])
        if ts is None:
            continue
        try:
            open_, high, low, close = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        except (TypeError, ValueError):
            continue
        volume = 0.0
        if len(row) > 5 and row[5] not in (None, ""):
            try:
                volume = float(row[5])
            except (TypeError, ValueError):
                volume = 0.0
        parsed.append(Bar(ts=ts, open=open_, high=high, low=low, close=close, volume=volume))
    parsed.sort(key=lambda bar: bar.ts)
    return parsed


def opening_candle(bars: list[Bar], session_date) -> OpeningCandle | None:
    """9:15–9:20 candle. Incomplete until the 9:19 minute has printed."""
    window = [
        bar
        for bar in bars
        if bar.ts.date() == session_date and OPEN <= bar.ts.time() < CLOSE
    ]
    if not window:
        return None
    if not any(bar.ts.time() >= LAST_MINUTE for bar in window):
        return None
    window.sort(key=lambda bar: bar.ts)
    return OpeningCandle(
        open=window[0].open,
        high=max(bar.high for bar in window),
        low=min(bar.low for bar in window),
        close=window[-1].close,
        bars=len(window),
        volume=sum(bar.volume for bar in window),
    )


def previous_close(bars: list[Bar], session_date) -> float | None:
    """Last traded close from the most recent session before today."""
    prior_days = sorted({bar.ts.date() for bar in bars if bar.ts.date() < session_date})
    if not prior_days:
        return None
    last_day = prior_days[-1]
    day_bars = [bar for bar in bars if bar.ts.date() == last_day]
    if not day_bars:
        return None
    day_bars.sort(key=lambda bar: bar.ts)
    return day_bars[-1].close


def aggregate_5m(bars: list[Bar]) -> list[Bar]:
    """Cash-session 5-minute bars, oldest first. Incomplete current bucket kept."""
    buckets: dict[datetime, list[Bar]] = {}
    for bar in bars:
        if not _in_session(bar.ts):
            continue
        key = _bucket(bar.ts, 5)
        buckets.setdefault(key, []).append(bar)
    out: list[Bar] = []
    for key in sorted(buckets):
        rows = sorted(buckets[key], key=lambda item: item.ts)
        out.append(
            Bar(
                ts=key,
                open=rows[0].open,
                high=max(item.high for item in rows),
                low=min(item.low for item in rows),
                close=rows[-1].close,
                volume=sum(item.volume for item in rows),
            )
        )
    return out


def _in_session(ts: datetime) -> bool:
    stamp = ts.timetz().replace(tzinfo=None) if ts.tzinfo else ts.time()
    return OPEN <= stamp < SESSION_END


def _bucket(ts: datetime, minutes: int) -> datetime:
    open_ts = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    delta_min = int((ts - open_ts).total_seconds() // 60)
    slot = (delta_min // minutes) * minutes
    return open_ts + timedelta(minutes=slot)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        ts = value
    else:
        raw = str(value).strip()
        if raw.endswith("+0530"):
            raw = raw[:-5] + "+05:30"
        elif raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)
