"""Lightweight daily technical analysis for heatmap candidates (Yahoo NSE OHLC)."""
from __future__ import annotations

from typing import Any, Literal

import httpx
import pandas as pd

Side = Literal["loser", "winner"]

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class TechAnalysisError(Exception):
    pass


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    line = ema12 - ema26
    signal = line.ewm(span=9, adjust=False).mean()
    hist = line - signal
    return line, signal, hist


def fetch_daily_ohlc(symbol: str, range_: str = "3mo") -> pd.DataFrame:
    symbol = symbol.strip().upper()
    url = YAHOO_CHART.format(symbol=symbol)
    with httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=True) as client:
        resp = client.get(url, params={"interval": "1d", "range": range_})
    if resp.status_code >= 400:
        raise TechAnalysisError(f"Yahoo OHLC failed for {symbol} ({resp.status_code})")
    payload = resp.json() or {}
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        err = ((payload.get("chart") or {}).get("error") or {}).get("description")
        raise TechAnalysisError(err or f"No Yahoo chart data for {symbol}")
    ts = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    df = pd.DataFrame(
        {
            "ts": pd.to_datetime(ts, unit="s"),
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume"),
        }
    ).dropna(subset=["close"])
    if len(df) < 30:
        raise TechAnalysisError(f"Not enough daily bars for {symbol} ({len(df)})")
    return df.reset_index(drop=True)


def compute_indicators(df: pd.DataFrame) -> dict[str, Any]:
    close = df["close"].astype(float)
    volume = df["volume"].fillna(0).astype(float)
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    rsi = _rsi(close)
    macd_line, macd_signal, macd_hist = _macd(close)
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    high20 = float(close.tail(20).max())
    low20 = float(close.tail(20).min())
    vol_avg20 = float(volume.tail(20).mean() or 0)
    last_vol = float(volume.iloc[-1] or 0)

    rsi_v = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None
    sma20_v = float(sma20.iloc[-1]) if pd.notna(sma20.iloc[-1]) else None
    sma50_v = float(sma50.iloc[-1]) if pd.notna(sma50.iloc[-1]) else None
    hist_v = float(macd_hist.iloc[-1]) if pd.notna(macd_hist.iloc[-1]) else None
    hist_prev = float(macd_hist.iloc[-2]) if pd.notna(macd_hist.iloc[-2]) else None

    return {
        "close": last,
        "prev_close": prev,
        "day_change_pct": ((last / prev) - 1.0) * 100 if prev else None,
        "rsi14": round(rsi_v, 2) if rsi_v is not None else None,
        "sma20": round(sma20_v, 2) if sma20_v is not None else None,
        "sma50": round(sma50_v, 2) if sma50_v is not None else None,
        "above_sma20": bool(sma20_v is not None and last > sma20_v),
        "above_sma50": bool(sma50_v is not None and last > sma50_v),
        "macd_hist": round(hist_v, 4) if hist_v is not None else None,
        "macd_hist_rising": bool(
            hist_v is not None and hist_prev is not None and hist_v > hist_prev
        ),
        "dist_from_20d_high_pct": round(((last / high20) - 1.0) * 100, 2) if high20 else None,
        "dist_from_20d_low_pct": round(((last / low20) - 1.0) * 100, 2) if low20 else None,
        "volume_vs_avg20": round(last_vol / vol_avg20, 2) if vol_avg20 else None,
        "bars": len(df),
    }


def score_setup(side: Side, ind: dict[str, Any]) -> tuple[float, list[str]]:
    """
    Side-aware score 0..100.
    - loser: bounce / mean-reversion quality
    - winner: continuation quality (not exhausted)
    """
    score = 50.0
    reasons: list[str] = []
    rsi = ind.get("rsi14")
    hist = ind.get("macd_hist")
    rising = ind.get("macd_hist_rising")
    vol_x = ind.get("volume_vs_avg20")
    dist_high = ind.get("dist_from_20d_high_pct")
    dist_low = ind.get("dist_from_20d_low_pct")

    if side == "loser":
        if rsi is not None:
            if rsi <= 30:
                score += 22
                reasons.append(f"RSI {rsi:.1f} oversold")
            elif rsi <= 40:
                score += 16
                reasons.append(f"RSI {rsi:.1f} weak / bounce zone")
            elif rsi <= 50:
                score += 6
                reasons.append(f"RSI {rsi:.1f} neutral-weak")
            else:
                score -= 10
                reasons.append(f"RSI {rsi:.1f} not oversold for loser")
        if not ind.get("above_sma20"):
            score += 6
            reasons.append("Below SMA20 (pullback)")
        if ind.get("above_sma50"):
            score += 8
            reasons.append("Still above SMA50 (higher-timeframe support)")
        elif ind.get("sma50") is not None:
            score -= 4
            reasons.append("Below SMA50")
        if rising and hist is not None and hist > -abs(hist or 0) * 2:
            score += 12
            reasons.append("MACD histogram rising (momentum turning)")
        elif hist is not None and hist < 0 and not rising:
            score -= 6
            reasons.append("MACD still falling")
        if dist_low is not None and dist_low <= 3:
            score += 10
            reasons.append("Near 20D low (reversal area)")
        if vol_x is not None and vol_x >= 1.3:
            score += 6
            reasons.append(f"Volume {vol_x:.1f}x avg (capitulation interest)")
    else:
        if rsi is not None:
            if 55 <= rsi <= 68:
                score += 20
                reasons.append(f"RSI {rsi:.1f} healthy trend")
            elif 50 <= rsi < 55:
                score += 12
                reasons.append(f"RSI {rsi:.1f} constructive")
            elif 68 < rsi <= 75:
                score += 4
                reasons.append(f"RSI {rsi:.1f} extended")
            elif rsi > 75:
                score -= 14
                reasons.append(f"RSI {rsi:.1f} overbought")
            else:
                score -= 8
                reasons.append(f"RSI {rsi:.1f} weak for winner")
        if ind.get("above_sma20"):
            score += 10
            reasons.append("Above SMA20")
        else:
            score -= 8
            reasons.append("Lost SMA20")
        if ind.get("above_sma50"):
            score += 10
            reasons.append("Above SMA50")
        if hist is not None and hist > 0:
            score += 10
            reasons.append("MACD hist positive")
        if rising:
            score += 8
            reasons.append("MACD hist rising")
        if dist_high is not None and dist_high >= -1.5:
            score += 6
            reasons.append("Holding near 20D highs")
        elif dist_high is not None and dist_high < -5:
            score -= 6
            reasons.append("Faded from 20D high")
        if vol_x is not None and vol_x >= 1.2:
            score += 6
            reasons.append(f"Volume {vol_x:.1f}x avg (participation)")

    score = max(0.0, min(100.0, score))
    return round(score, 1), reasons


def analyze_symbol(symbol: str, side: Side, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = meta or {}
    try:
        df = fetch_daily_ohlc(symbol)
        ind = compute_indicators(df)
        score, reasons = score_setup(side, ind)
        return {
            "ok": True,
            "symbol": symbol.upper(),
            "side": side,
            "pct": meta.get("pct"),
            "ltp": meta.get("ltp") or ind.get("close"),
            "score": score,
            "bias": "bounce" if side == "loser" else "continuation",
            "indicators": ind,
            "reasons": reasons,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "symbol": symbol.upper(),
            "side": side,
            "pct": meta.get("pct"),
            "ltp": meta.get("ltp"),
            "score": 0.0,
            "bias": "bounce" if side == "loser" else "continuation",
            "indicators": {},
            "reasons": [],
            "error": str(exc),
        }


def _best_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    ok = [a for a in rows if a.get("ok")]
    if not ok:
        return None
    return sorted(
        ok,
        key=lambda a: (a.get("score") or 0.0, abs(a.get("pct") or 0.0)),
        reverse=True,
    )[0]


def analyze_candidates(
    losers: list[dict[str, Any]],
    winners: list[dict[str, Any]],
) -> dict[str, Any]:
    analyses: list[dict[str, Any]] = []
    for row in losers:
        analyses.append(analyze_symbol(row["symbol"], "loser", row))
    for row in winners:
        analyses.append(analyze_symbol(row["symbol"], "winner", row))

    loser_analyses = [a for a in analyses if a.get("side") == "loser"]
    winner_analyses = [a for a in analyses if a.get("side") == "winner"]
    selected_loser = _best_of(loser_analyses)
    selected_winner = _best_of(winner_analyses)

    ranked = sorted(
        [a for a in analyses if a.get("ok")],
        key=lambda a: (a.get("score") or 0.0, abs(a.get("pct") or 0.0)),
        reverse=True,
    )
    return {
        "analyses": analyses,
        "ranked": ranked,
        "selected_loser": selected_loser,
        "selected_winner": selected_winner,
        # Keep legacy key as the stronger of the two (UI prefers the pair fields).
        "selected": (
            selected_loser
            if (selected_loser and not selected_winner)
            else selected_winner
            if (selected_winner and not selected_loser)
            else (
                selected_loser
                if (selected_loser and selected_winner and selected_loser["score"] >= selected_winner["score"])
                else selected_winner
            )
        ),
        "method": (
            "Daily OHLC (Yahoo .NS): RSI14, SMA20/50, MACD hist, "
            "20D high/low distance, volume vs 20D avg. "
            "Best bounce among top losers + best continuation among top winners."
        ),
    }
