"""Build a closed-bar view of Nifty from 1-minute candles."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from index_tester.indicators import adx, cpr, ema, session_vwap, supertrend, atr
from index_tester.strategy import BarView

IST = timezone(timedelta(hours=5, minutes=30))


def parse_candles(rows: list[Any]) -> pd.DataFrame:
    parsed: list[dict[str, Any]] = []
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
        parsed.append(
            {
                "ts": ts,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    frame = pd.DataFrame(parsed)
    if frame.empty:
        return frame
    frame = frame.sort_values("ts").drop_duplicates("ts", keep="last")
    return frame.reset_index(drop=True)


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


def _session_open(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.normalize() + pd.Timedelta(hours=9, minutes=15)


def _in_session(ts: pd.Timestamp) -> bool:
    open_ = _session_open(ts)
    return open_ <= ts < open_ + pd.Timedelta(hours=6, minutes=15)


def _bucket(ts: pd.Timestamp, minutes: int) -> pd.Timestamp:
    open_ = _session_open(ts)
    delta_min = int((ts - open_).total_seconds() // 60)
    slot = (delta_min // minutes) * minutes
    return open_ + pd.Timedelta(minutes=slot)


def aggregate(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    work = frame.loc[frame["ts"].map(_in_session)].copy()
    if work.empty:
        return work
    work["bucket"] = work["ts"].map(lambda ts: _bucket(pd.Timestamp(ts), minutes))
    grouped = (
        work.groupby("bucket", sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
        .rename(columns={"bucket": "ts"})
    )
    return grouped


def _closed(frame: pd.DataFrame, minutes: int, now: datetime) -> pd.DataFrame:
    if frame.empty:
        return frame
    close_ts = frame["ts"] + pd.Timedelta(minutes=minutes)
    now_ts = pd.Timestamp(now)
    return frame.loc[close_ts <= now_ts].reset_index(drop=True)


def pick_session(now: datetime, dates: list[datetime.date]) -> datetime.date | None:
    if not dates:
        return None
    today = now.date()
    minute = now.hour * 60 + now.minute
    if today in dates and minute >= 9 * 60 + 15:
        return today
    prior = [day for day in dates if day < today]
    if prior:
        return prior[-1]
    return sorted(dates)[-1]


def prepare(frame: pd.DataFrame, now: datetime, instrument: str = "") -> BarView:
    empty = BarView(
        ready=False,
        close=0,
        vwap=0,
        ema9=0,
        ema21=0,
        ema9_15=0,
        ema21_15=0,
        adx15=0,
        atr5=0,
        supertrend_dir=0,
        prev_supertrend_dir=0,
        orb_high=None,
        orb_low=None,
        cpr_top=0,
        cpr_bottom=0,
        bar_ts="",
        bar_close=None,
        instrument=instrument,
    )
    if frame.empty:
        return empty
    work = frame.copy()
    work["ts"] = pd.to_datetime(work["ts"])
    work = work.loc[work["ts"].map(_in_session)]
    if work.empty:
        return empty
    work["session"] = work["ts"].dt.date
    session = pick_session(now, sorted(work["session"].unique()))
    if session is None:
        return empty

    prior_days = [day for day in sorted(work["session"].unique()) if day < session]
    if not prior_days:
        return empty
    prev_day = prior_days[-1]
    prev = work.loc[work["session"] == prev_day]
    top, bottom, _pivot = cpr(float(prev["high"].max()), float(prev["low"].min()), float(prev["close"].iloc[-1]))

    session_bars = work.loc[work["session"] == session]
    clock = session_bars["ts"].dt.hour * 60 + session_bars["ts"].dt.minute
    orb_bars = session_bars.loc[(clock >= 9 * 60 + 15) & (clock < 9 * 60 + 30)]
    orb_high = float(orb_bars["high"].max()) if not orb_bars.empty else None
    orb_low = float(orb_bars["low"].min()) if not orb_bars.empty else None

    history = work.loc[work["session"] <= session]
    five = _closed(aggregate(history, 5), 5, now)
    fifteen = _closed(aggregate(history, 15), 15, now)
    session_five = five.loc[five["ts"].dt.date == session] if not five.empty else five
    if session_five.empty or fifteen.empty:
        empty.session_date = session.isoformat()
        empty.orb_high = orb_high
        empty.orb_low = orb_low
        empty.cpr_top = top
        empty.cpr_bottom = bottom
        return empty

    five = five.copy()
    five["ema9"] = ema(five["close"], 9)
    five["ema21"] = ema(five["close"], 21)
    five["atr"] = atr(five, 14)
    five["st"] = supertrend(five, 10, 3.0)
    fifteen = fifteen.copy()
    fifteen["ema9"] = ema(fifteen["close"], 9)
    fifteen["ema21"] = ema(fifteen["close"], 21)
    fifteen["adx"] = adx(fifteen, 14)

    last = five.iloc[-1]
    prev_st = int(five["st"].iloc[-2]) if len(five) > 1 and pd.notna(five["st"].iloc[-2]) else 0
    last15 = fifteen.iloc[-1]
    upto = session_bars.loc[session_bars["ts"] < pd.Timestamp(last["ts"]) + pd.Timedelta(minutes=5)]
    vwap_series = session_vwap(upto) if not upto.empty else pd.Series(dtype=float)
    vwap_value = float(vwap_series.iloc[-1]) if not vwap_series.empty else float("nan")

    needed = [last["ema9"], last["ema21"], last["atr"], last["st"], last15["ema9"], last15["ema21"], last15["adx"], vwap_value]
    ready = all(pd.notna(value) for value in needed)
    bar_start = pd.Timestamp(last["ts"]).to_pydatetime()
    bar_close = bar_start + timedelta(minutes=5)
    return BarView(
        ready=bool(ready),
        close=float(last["close"]),
        vwap=float(vwap_value) if pd.notna(vwap_value) else 0.0,
        ema9=float(last["ema9"]) if pd.notna(last["ema9"]) else 0.0,
        ema21=float(last["ema21"]) if pd.notna(last["ema21"]) else 0.0,
        ema9_15=float(last15["ema9"]) if pd.notna(last15["ema9"]) else 0.0,
        ema21_15=float(last15["ema21"]) if pd.notna(last15["ema21"]) else 0.0,
        adx15=float(last15["adx"]) if pd.notna(last15["adx"]) else 0.0,
        atr5=float(last["atr"]) if pd.notna(last["atr"]) else 0.0,
        supertrend_dir=int(last["st"]) if pd.notna(last["st"]) else 0,
        prev_supertrend_dir=prev_st,
        orb_high=orb_high,
        orb_low=orb_low,
        cpr_top=top,
        cpr_bottom=bottom,
        bar_ts=bar_start.isoformat(timespec="minutes"),
        bar_close=bar_close,
        session_date=session.isoformat(),
        instrument=instrument,
    )
