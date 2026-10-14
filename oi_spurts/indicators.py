"""Intraday TA helpers for the OI-spurts opener only."""
from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], span: int) -> list[float | None]:
    if span <= 0:
        raise ValueError("span must be positive")
    out: list[float | None] = []
    alpha = 2.0 / (span + 1.0)
    prev: float | None = None
    for value in values:
        price = float(value)
        if prev is None:
            prev = price
        else:
            prev = alpha * price + (1.0 - alpha) * prev
        out.append(prev)
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    closes = [float(v) for v in values]
    out: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) < period:
        return out
    total = sum(closes[:period])
    out[period - 1] = total / period
    for i in range(period, len(closes)):
        total += closes[i] - closes[i - period]
        out[i] = total / period
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    closes = [float(v) for v in values]
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def macd_hist(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> list[float | None]:
    """MACD histogram, same idea as the stock-option heatmap TA."""
    closes = [float(v) for v in values]
    out: list[float | None] = [None] * len(closes)
    if len(closes) < slow + signal:
        return out
    fast_line = ema(closes, fast)
    slow_line = ema(closes, slow)
    macd_line: list[float] = []
    for i, close in enumerate(closes):
        f = fast_line[i]
        s = slow_line[i]
        if f is None or s is None:
            macd_line.append(0.0)
        else:
            macd_line.append(f - s)
    signal_line = ema(macd_line, signal)
    for i in range(len(closes)):
        if i < slow - 1:
            continue
        sig = signal_line[i]
        if sig is None:
            continue
        out[i] = macd_line[i] - sig
    return out


def body_ratio(open_: float, high: float, low: float, close: float) -> float:
    span = float(high) - float(low)
    if span <= 0:
        return 0.0
    return abs(float(close) - float(open_)) / span


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
