"""OI + candle + stock-style TA filters, then a score. OI-spurts package only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oi_spurts.candles import Bar, OpeningCandle, aggregate_5m
from oi_spurts.config import (
    BODY_MIN,
    EMA_FAST,
    EMA_SLOW,
    EMA_TREND,
    MAX_ENTRIES,
    MAX_STOCK_STOP_PCT,
    MIN_OI_PCT,
    MIN_SETUP_SCORE,
    RSI_GAINER_MAX,
    RSI_GAINER_MIN,
    RSI_LOSER_MAX,
    RSI_LOSER_MIN,
    RSI_PERIOD,
    SMA_FAST,
    VOLUME_MIN_RATIO,
)
from oi_spurts.indicators import body_ratio, ema, macd_hist, rsi, sma
from oi_spurts.strategy import (
    candle_agrees,
    capped_stop,
    entry_risk,
    gap_agrees,
)


@dataclass(frozen=True)
class SetupView:
    symbol: str
    band: str
    option_type: str
    stock_pct: float | None
    oi_pct: float | None
    prev_close: float
    candle: OpeningCandle
    stop_price: float
    risk_distance: float
    ema9: float
    ema21: float
    ema50: float
    sma20: float
    rsi14: float
    macd_hist: float
    body: float
    vol_ratio: float | None
    score: float
    reasons: tuple[str, ...]


class Reject(Exception):
    """Hard gate failed. Do not buy this name today for this scan."""


def evaluate_setup(
    *,
    match: dict[str, Any],
    candle: OpeningCandle,
    prev_close: float,
    bars: list[Bar],
) -> SetupView:
    band = str(match["band"])
    option_type = str(match["option_type"])
    oi_pct = _maybe_float(match.get("oi_pct"))
    if oi_pct is None or abs(oi_pct) < MIN_OI_PCT:
        raise Reject(f"OI % too small ({oi_pct}, need |OI| ≥ {MIN_OI_PCT:g})")

    if not gap_agrees(band, candle.open, prev_close):
        raise Reject(
            f"open {candle.open:g} vs prev close {prev_close:g} does not gap with the {band} side"
        )
    if not candle_agrees(band, candle.open, candle.close):
        raise Reject(
            f"5-minute candle {candle.open:g} → {candle.close:g} does not match the {band} side"
        )

    body = body_ratio(candle.open, candle.high, candle.low, candle.close)
    if body < BODY_MIN:
        raise Reject(f"candle body too weak ({body:.0%} of range, need ≥{BODY_MIN:.0%})")

    frame = aggregate_5m(bars)
    need = max(EMA_TREND, SMA_FAST, 26 + 9) + 2
    if len(frame) < need:
        raise Reject("not enough history for EMA/SMA/MACD yet")

    closes = [bar.close for bar in frame]
    ema9 = ema(closes, EMA_FAST)[-1]
    ema21 = ema(closes, EMA_SLOW)[-1]
    ema50 = ema(closes, EMA_TREND)[-1]
    sma20 = sma(closes, SMA_FAST)[-1]
    rsi14 = rsi(closes, RSI_PERIOD)[-1]
    hist_series = macd_hist(closes)
    hist = hist_series[-1]
    hist_prev = hist_series[-2] if len(hist_series) >= 2 else None
    if None in (ema9, ema21, ema50, sma20, rsi14, hist):
        raise Reject("indicators not ready")

    if band == "gainer":
        if candle.close <= ema9:
            raise Reject(f"close {candle.close:g} is not above EMA{EMA_FAST} {ema9:g}")
        if ema9 <= ema21:
            raise Reject(f"EMA stack not up (EMA9 {ema9:g} ≤ EMA21 {ema21:g})")
        if candle.close <= sma20:
            raise Reject(f"close {candle.close:g} is not above SMA{SMA_FAST} {sma20:g}")
        if candle.close <= ema50:
            raise Reject(f"close {candle.close:g} is not above EMA{EMA_TREND} {ema50:g}")
        if hist <= 0:
            raise Reject(f"MACD hist not positive ({hist:g})")
        if not (RSI_GAINER_MIN <= rsi14 <= RSI_GAINER_MAX):
            raise Reject(f"RSI {rsi14:.1f} outside gainer band {RSI_GAINER_MIN:g}-{RSI_GAINER_MAX:g}")
    else:
        if candle.close >= ema9:
            raise Reject(f"close {candle.close:g} is not below EMA{EMA_FAST} {ema9:g}")
        if ema9 >= ema21:
            raise Reject(f"EMA stack not down (EMA9 {ema9:g} ≥ EMA21 {ema21:g})")
        if candle.close >= sma20:
            raise Reject(f"close {candle.close:g} is not below SMA{SMA_FAST} {sma20:g}")
        if candle.close >= ema50:
            raise Reject(f"close {candle.close:g} is not below EMA{EMA_TREND} {ema50:g}")
        if hist >= 0:
            raise Reject(f"MACD hist not negative ({hist:g})")
        if not (RSI_LOSER_MIN <= rsi14 <= RSI_LOSER_MAX):
            raise Reject(f"RSI {rsi14:.1f} outside loser band {RSI_LOSER_MIN:g}-{RSI_LOSER_MAX:g}")

    vol_ratio = _volume_ratio(candle, frame)
    if vol_ratio is not None and vol_ratio < VOLUME_MIN_RATIO:
        raise Reject(
            f"opening volume weak ({vol_ratio:.2f}x avg, need ≥{VOLUME_MIN_RATIO:g}x)"
        )

    stop_price = capped_stop(option_type, candle.close, candle.open, MAX_STOCK_STOP_PCT)
    risk = entry_risk(option_type, candle.close, stop_price)
    if risk <= 0:
        raise Reject("capped stop left no risk distance")

    score, reasons = _score(
        band=band,
        stock_pct=match.get("stock_pct"),
        oi_pct=oi_pct,
        body=body,
        ema9=float(ema9),
        ema21=float(ema21),
        ema50=float(ema50),
        sma20=float(sma20),
        rsi14=float(rsi14),
        macd_hist=float(hist),
        macd_rising=bool(hist_prev is not None and hist > hist_prev),
        vol_ratio=vol_ratio,
        open_=candle.open,
        prev_close=prev_close,
        stop_price=stop_price,
        entry=candle.close,
    )
    if score < MIN_SETUP_SCORE:
        raise Reject(f"setup score too low ({score:.1f} < {MIN_SETUP_SCORE:g})")

    return SetupView(
        symbol=str(match["symbol"]),
        band=band,
        option_type=option_type,
        stock_pct=_maybe_float(match.get("stock_pct")),
        oi_pct=oi_pct,
        prev_close=float(prev_close),
        candle=candle,
        stop_price=stop_price,
        risk_distance=risk,
        ema9=float(ema9),
        ema21=float(ema21),
        ema50=float(ema50),
        sma20=float(sma20),
        rsi14=float(rsi14),
        macd_hist=float(hist),
        body=body,
        vol_ratio=vol_ratio,
        score=score,
        reasons=tuple(reasons),
    )


def pick_entries(setups: list[SetupView], *, slots_left: int) -> list[SetupView]:
    if slots_left <= 0:
        return []
    ranked = sorted(setups, key=lambda row: row.score, reverse=True)
    return ranked[: min(slots_left, MAX_ENTRIES)]


def setup_payload(view: SetupView) -> dict[str, Any]:
    return {
        "symbol": view.symbol,
        "band": view.band,
        "option_type": view.option_type,
        "stock_pct": view.stock_pct,
        "oi_pct": view.oi_pct,
        "score": round(view.score, 2),
        "body": round(view.body, 3),
        "ema9": round(view.ema9, 2),
        "ema21": round(view.ema21, 2),
        "ema50": round(view.ema50, 2),
        "sma20": round(view.sma20, 2),
        "rsi14": round(view.rsi14, 1),
        "macd_hist": round(view.macd_hist, 4),
        "vol_ratio": None if view.vol_ratio is None else round(view.vol_ratio, 2),
        "stop_price": round(view.stop_price, 2),
        "risk_distance": round(view.risk_distance, 2),
        "prev_close": view.prev_close,
        "candle_open": view.candle.open,
        "candle_close": view.candle.close,
        "reasons": list(view.reasons),
    }


def _score(
    *,
    band: str,
    stock_pct: Any,
    oi_pct: float,
    body: float,
    ema9: float,
    ema21: float,
    ema50: float,
    sma20: float,
    rsi14: float,
    macd_hist: float,
    macd_rising: bool,
    vol_ratio: float | None,
    open_: float,
    prev_close: float,
    stop_price: float,
    entry: float,
) -> tuple[float, list[str]]:
    """Side-aware score inspired by the stock-option heatmap TA."""
    score = 50.0
    reasons: list[str] = []

    oi = abs(float(oi_pct))
    move = abs(_maybe_float(stock_pct) or 0.0)
    score += min(oi, 20.0) * 1.0
    score += min(move, 6.0) * 3.0
    reasons.append(f"OI {oi:.1f}%")
    reasons.append(f"move {move:.2f}%")
    score += body * 12.0
    reasons.append(f"body {body:.0%}")

    if band == "gainer":
        if 55 <= rsi14 <= 68:
            score += 16
            reasons.append(f"RSI {rsi14:.1f} healthy trend")
        elif 50 <= rsi14 < 55:
            score += 8
            reasons.append(f"RSI {rsi14:.1f} constructive")
        elif rsi14 > 68:
            score += 2
            reasons.append(f"RSI {rsi14:.1f} extended")
        if ema9 > ema21 > ema50:
            score += 10
            reasons.append("EMA 9>21>50")
        if entry > sma20:
            score += 8
            reasons.append("Above SMA20")
        if macd_hist > 0:
            score += 8
            reasons.append("MACD hist +")
        if macd_rising:
            score += 6
            reasons.append("MACD rising")
    else:
        if 32 <= rsi14 <= 42:
            score += 16
            reasons.append(f"RSI {rsi14:.1f} weak / continuation")
        elif 28 <= rsi14 < 32:
            score += 8
            reasons.append(f"RSI {rsi14:.1f} washed")
        elif rsi14 < 28:
            score += 2
            reasons.append(f"RSI {rsi14:.1f} extreme")
        if ema9 < ema21 < ema50:
            score += 10
            reasons.append("EMA 9<21<50")
        if entry < sma20:
            score += 8
            reasons.append("Below SMA20")
        if macd_hist < 0:
            score += 8
            reasons.append("MACD hist -")
        if macd_rising is False:
            score += 6
            reasons.append("MACD still falling")

    if vol_ratio is not None:
        score += min(vol_ratio, 2.2) * 5.0
        reasons.append(f"vol {vol_ratio:.2f}x")

    gap_pct = abs(float(open_) - float(prev_close)) / float(prev_close) * 100.0 if prev_close else 0.0
    score += min(gap_pct, 2.5) * 2.0
    reasons.append(f"gap {gap_pct:.2f}%")

    stop_pct = abs(float(entry) - float(stop_price)) / float(entry) * 100.0 if entry else 0.0
    reasons.append(f"stop {stop_pct:.2f}%")

    score = max(0.0, min(100.0, score))
    return round(score, 1), reasons


def _volume_ratio(candle: OpeningCandle, frame: list[Bar]) -> float | None:
    if candle.volume <= 0:
        return None
    prior = [bar.volume for bar in frame[:-1] if bar.volume > 0][-20:]
    if len(prior) < 5:
        return None
    avg = sum(prior) / len(prior)
    if avg <= 0:
        return None
    return float(candle.volume) / avg


def _maybe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
