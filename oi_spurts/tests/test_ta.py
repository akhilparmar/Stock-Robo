"""Soft TA gates and scoring for the OI-spurts opener."""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from oi_spurts.candles import Bar, OpeningCandle, aggregate_5m
from oi_spurts.indicators import body_ratio, ema, macd_hist, rsi, sma
from oi_spurts.ta import Reject, evaluate_setup, pick_entries

IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: date, hour: int, minute: int, open_: float, close: float, volume: float = 1000.0) -> Bar:
    ts = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
    high = max(open_, close) + 0.4
    low = min(open_, close) - 0.4
    return Bar(ts=ts, open=open_, high=high, low=low, close=close, volume=volume)


def _history(*, bullish: bool = True) -> tuple[list[Bar], float, float, float]:
    """Trend + late pullback so EMA/MACD align and RSI stays in the live band."""
    bars: list[Bar] = []
    day = date(2026, 10, 13)
    price = 100.0
    closes: list[float] = []
    for i in range(90):
        wave = i % 5
        if i >= 78:
            # Cool RSI before the open without wrecking the EMA stack.
            step = -0.18 if i % 2 == 0 else -0.054
            if not bullish:
                step = -step
        elif bullish:
            step = 0.12 if wave < 3 else -0.12
        else:
            step = -0.12 if wave < 3 else 0.12
        price += step
        closes.append(price)
    minute = 9 * 60 + 15
    prev = closes[0]
    for i, close in enumerate(closes):
        hour, mins = divmod(minute, 60)
        if hour >= 15 and mins >= 30:
            minute = 9 * 60 + 15
            hour, mins = 9, 15
        bars.append(_bar(day, hour, mins, prev, close, volume=900 + i * 5))
        prev = close
        minute += 5
    today = date(2026, 10, 14)
    end = closes[-1] + (1.3 if bullish else -1.3)
    open_ = closes[-1] + (0.1 if bullish else -0.1)
    bars.append(_bar(today, 9, 15, open_, end, volume=2800))
    bars.append(_bar(today, 9, 16, end, end, volume=250))
    bars.append(_bar(today, 9, 17, end, end, volume=250))
    bars.append(_bar(today, 9, 18, end, end, volume=250))
    bars.append(_bar(today, 9, 19, end, end, volume=250))
    return bars, open_, end, closes[-1]


class IndicatorTests(unittest.TestCase):
    def test_body_rsi_ema_macd_sma(self) -> None:
        self.assertGreaterEqual(body_ratio(100, 110, 100, 108), 0.8)
        values = [100 + i * 0.2 for i in range(40)]
        self.assertIsNotNone(ema(values, 9)[-1])
        self.assertIsNotNone(sma(values, 20)[-1])
        self.assertIsNotNone(rsi(values, 14)[-1])
        self.assertIsNotNone(macd_hist(values)[-1])


class SetupTests(unittest.TestCase):
    def test_gainer_setup_passes_and_scores(self) -> None:
        bars, open_, end, prev = _history(bullish=True)
        candle = OpeningCandle(
            open=open_,
            high=max(open_, end) + 0.5,
            low=min(open_, end) - 0.5,
            close=end,
            bars=5,
            volume=3800,
        )
        view = evaluate_setup(
            match={
                "symbol": "TCS",
                "band": "gainer",
                "option_type": "CE",
                "stock_pct": 3.2,
                "oi_pct": 8.5,
            },
            candle=candle,
            prev_close=prev - 0.5,
            bars=bars,
        )
        self.assertEqual(view.symbol, "TCS")
        self.assertGreaterEqual(view.score, 40)
        self.assertGreater(view.candle.close, view.ema9)
        self.assertGreater(view.ema9, view.ema21)
        self.assertGreater(view.macd_hist, 0)
        self.assertLess(view.risk_distance, abs(view.candle.close - view.candle.open) + 1e-9)

    def test_weak_body_is_rejected(self) -> None:
        bars, open_, end, prev = _history(bullish=True)
        candle = OpeningCandle(open=end, high=end + 8, low=end - 8, close=end + 0.2, bars=5, volume=3800)
        with self.assertRaises(Reject):
            evaluate_setup(
                match={
                    "symbol": "TCS",
                    "band": "gainer",
                    "option_type": "CE",
                    "stock_pct": 2.0,
                    "oi_pct": 5.0,
                },
                candle=candle,
                prev_close=prev - 1,
                bars=bars,
            )

    def test_pick_top_two(self) -> None:
        bars, open_, end, prev = _history(bullish=True)
        candle = OpeningCandle(
            open=open_,
            high=max(open_, end) + 0.5,
            low=min(open_, end) - 0.5,
            close=end,
            bars=5,
            volume=3800,
        )
        a = evaluate_setup(
            match={"symbol": "AAA", "band": "gainer", "option_type": "CE", "stock_pct": 5.0, "oi_pct": 12.0},
            candle=candle,
            prev_close=prev - 0.5,
            bars=bars,
        )
        b = evaluate_setup(
            match={"symbol": "BBB", "band": "gainer", "option_type": "CE", "stock_pct": 2.0, "oi_pct": 4.0},
            candle=candle,
            prev_close=prev - 0.5,
            bars=bars,
        )
        c = evaluate_setup(
            match={"symbol": "CCC", "band": "gainer", "option_type": "CE", "stock_pct": 3.0, "oi_pct": 7.0},
            candle=candle,
            prev_close=prev - 0.5,
            bars=bars,
        )
        picked = pick_entries([a, b, c], slots_left=2)
        self.assertEqual(len(picked), 2)
        self.assertEqual(picked[0].symbol, "AAA")

    def test_aggregate_builds_five_minute_bars(self) -> None:
        day = date(2026, 10, 14)
        bars = [
            _bar(day, 9, 15, 100, 101),
            _bar(day, 9, 16, 101, 102),
            _bar(day, 9, 19, 102, 103),
        ]
        frame = aggregate_5m(bars)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame[0].open, 100)
        self.assertEqual(frame[0].close, 103)


if __name__ == "__main__":
    unittest.main()
