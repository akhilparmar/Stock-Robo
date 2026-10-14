"""Intraday Nifty indicators. Computed on the index, never on the option premium."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr_smooth = atr(df, period)
    plus_s = plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    minus_s = minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * plus_s / tr_smooth.replace(0, np.nan)
    minus_di = 100 * minus_s / tr_smooth.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    """Return +1 for an up trend and -1 for a down trend."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    atr_s = atr(df, period)
    hl2 = (high + low) / 2
    basic_upper = hl2 + multiplier * atr_s
    basic_lower = hl2 - multiplier * atr_s
    direction = pd.Series(index=df.index, dtype="float64")
    final_upper = pd.Series(index=df.index, dtype="float64")
    final_lower = pd.Series(index=df.index, dtype="float64")
    for i in range(len(df)):
        if pd.isna(atr_s.iloc[i]):
            direction.iloc[i] = pd.NA
            continue
        if i == 0 or pd.isna(direction.iloc[i - 1]):
            final_upper.iloc[i] = basic_upper.iloc[i]
            final_lower.iloc[i] = basic_lower.iloc[i]
            direction.iloc[i] = 1
            continue
        if basic_upper.iloc[i] < final_upper.iloc[i - 1] or close.iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]
        if basic_lower.iloc[i] > final_lower.iloc[i - 1] or close.iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]
        if direction.iloc[i - 1] == 1 and close.iloc[i] < final_lower.iloc[i]:
            direction.iloc[i] = -1
        elif direction.iloc[i - 1] == -1 and close.iloc[i] > final_upper.iloc[i]:
            direction.iloc[i] = 1
        else:
            direction.iloc[i] = direction.iloc[i - 1]
    return direction


def session_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3
    volume = df["volume"].astype(float).clip(lower=0)
    if float(volume.sum()) <= 0:
        weight = pd.Series(1.0, index=df.index)
    else:
        weight = volume.where(volume > 0, 0.0)
        if float(weight.sum()) <= 0:
            weight = pd.Series(1.0, index=df.index)
    pv = (typical * weight).cumsum()
    vv = weight.cumsum().replace(0, pd.NA)
    return pv / vv


def cpr(high: float, low: float, close: float) -> tuple[float, float, float]:
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = (2 * pivot) - bc
    return max(tc, bc), min(tc, bc), pivot
