"""Daily SMA / RSI / MACD checks, same family as the stock-option heatmap TA."""
from __future__ import annotations

from typing import Any

from app.tech_analysis import TechAnalysisError, analyze_symbol, compute_indicators, fetch_daily_ohlc


class DailyTaError(Exception):
    pass


def load_daily(symbol: str) -> dict[str, Any]:
    """Yahoo daily indicators used by the stock option suggest flow."""
    try:
        result = analyze_symbol(symbol, side="winner")
    except TechAnalysisError as exc:
        raise DailyTaError(str(exc)) from exc
    if not result.get("ok"):
        raise DailyTaError(result.get("error") or f"Daily TA failed for {symbol}")
    ind = result.get("indicators") or {}
    if not ind:
        raise DailyTaError(f"No daily indicators for {symbol}")
    return ind


def score_daily(band: str, ind: dict[str, Any]) -> tuple[float, list[str]]:
    """
    OI-spurts sides are continuation, not bounce:
    - gainer → buy call (winner / trend-up)
    - loser → buy put (trend-down), opposite of the heatmap loser bounce score
    """
    if band == "gainer":
        from app.tech_analysis import score_setup

        return score_setup("winner", ind)
    return _score_put_continuation(ind)


def passes_daily_gates(band: str, ind: dict[str, Any], *, min_score: float) -> tuple[bool, str]:
    score, _ = score_daily(band, ind)
    rsi = ind.get("rsi14")
    if band == "gainer":
        if not ind.get("above_sma20"):
            return False, "lost daily SMA20"
        if rsi is not None and rsi > 75:
            return False, f"daily RSI {rsi:.1f} overbought"
        if ind.get("macd_hist") is not None and ind["macd_hist"] < 0 and not ind.get("macd_hist_rising"):
            return False, "daily MACD hist negative and falling"
    else:
        if ind.get("above_sma20"):
            return False, "still above daily SMA20 (put needs weakness)"
        if rsi is not None and rsi < 20:
            return False, f"daily RSI {rsi:.1f} already crashed"
        if rsi is not None and rsi > 50:
            return False, f"daily RSI {rsi:.1f} too strong for a put"
        if ind.get("macd_hist") is not None and ind["macd_hist"] > 0 and ind.get("macd_hist_rising"):
            return False, "daily MACD rising against the put"
    if score < min_score:
        return False, f"daily TA score {score:g} below {min_score:g}"
    return True, f"daily TA score {score:g}"


def _score_put_continuation(ind: dict[str, Any]) -> tuple[float, list[str]]:
    score = 50.0
    reasons: list[str] = []
    rsi = ind.get("rsi14")
    hist = ind.get("macd_hist")
    rising = ind.get("macd_hist_rising")
    vol_x = ind.get("volume_vs_avg20")
    dist_high = ind.get("dist_from_20d_high_pct")

    if rsi is not None:
        if 30 <= rsi <= 45:
            score += 18
            reasons.append(f"RSI {rsi:.1f} weak / trending down")
        elif 45 < rsi <= 50:
            score += 8
            reasons.append(f"RSI {rsi:.1f} softening")
        elif rsi < 30:
            score += 4
            reasons.append(f"RSI {rsi:.1f} very weak")
        else:
            score -= 12
            reasons.append(f"RSI {rsi:.1f} not weak enough for puts")
    if not ind.get("above_sma20"):
        score += 12
        reasons.append("Below SMA20")
    else:
        score -= 10
        reasons.append("Still above SMA20")
    if not ind.get("above_sma50"):
        score += 8
        reasons.append("Below SMA50")
    if hist is not None and hist < 0:
        score += 10
        reasons.append("MACD hist negative")
    if rising is False:
        score += 8
        reasons.append("MACD hist falling")
    elif rising:
        score -= 8
        reasons.append("MACD hist rising")
    if dist_high is not None and dist_high <= -3:
        score += 6
        reasons.append("Faded from 20D high")
    if vol_x is not None and vol_x >= 1.2:
        score += 6
        reasons.append(f"Volume {vol_x:.1f}x avg")
    score = max(0.0, min(100.0, score))
    return round(score, 1), reasons
