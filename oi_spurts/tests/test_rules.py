"""Rules for top-5 overlap, the opening candle, and the 1:1 trail."""
from __future__ import annotations

import unittest
from datetime import date

from oi_spurts.candles import opening_candle, parse_bars, previous_close
from oi_spurts.nse import parse_index_rows, parse_oi_rows, select_spurts
from oi_spurts.strategy import (
    candle_agrees,
    capped_stop,
    gap_agrees,
    match_spurts,
    premium_stop_hit,
    stop_hit,
    top_bands,
    trail_levels,
)


def _stock(symbol: str, pct: float) -> dict:
    return {"symbol": symbol, "pct": pct, "ltp": 100.0}


class TopBandsTests(unittest.TestCase):
    def test_top_five_each_side_ignores_flat_names(self) -> None:
        stocks = [_stock(f"G{i}", float(i)) for i in range(1, 8)]
        stocks += [_stock(f"L{i}", -float(i)) for i in range(1, 8)]
        stocks.append(_stock("FLAT", 0))
        gainers, losers = top_bands(stocks, 5)
        self.assertEqual([row["symbol"] for row in gainers], ["G7", "G6", "G5", "G4", "G3"])
        self.assertEqual([row["symbol"] for row in losers], ["L7", "L6", "L5", "L4", "L3"])

    def test_match_requires_oi_spurts(self) -> None:
        gainers = [_stock("TRENT", 4.0), _stock("INFY", 2.0)]
        losers = [_stock("WIPRO", -3.0)]
        spurts = [
            {"symbol": "TRENT", "oi_pct": 86.0},
            {"symbol": "SBIN", "oi_pct": 12.0},
        ]
        found = match_spurts(gainers, losers, spurts)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["symbol"], "TRENT")
        self.assertEqual(found[0]["option_type"], "CE")
        self.assertEqual(found[0]["oi_pct"], 86.0)


class CandleTests(unittest.TestCase):
    def test_direction(self) -> None:
        self.assertTrue(candle_agrees("gainer", 100, 101))
        self.assertFalse(candle_agrees("gainer", 101, 100))
        self.assertTrue(candle_agrees("loser", 101, 100))
        self.assertFalse(candle_agrees("loser", 100, 101))
        self.assertFalse(candle_agrees("gainer", 100, 100))

    def test_gap_vs_previous_close(self) -> None:
        self.assertTrue(gap_agrees("gainer", 101, 100))
        self.assertFalse(gap_agrees("gainer", 99, 100))
        self.assertFalse(gap_agrees("gainer", 100, 100))
        self.assertTrue(gap_agrees("loser", 99, 100))
        self.assertFalse(gap_agrees("loser", 101, 100))

    def test_previous_close_from_prior_session(self) -> None:
        day = date(2026, 10, 14)
        bars = parse_bars(
            [
                ["2026-10-13T15:29:00+05:30", 200, 201, 199, 200.5],
                ["2026-10-14T09:15:00+05:30", 202, 203, 201, 202.5],
            ]
        )
        self.assertEqual(previous_close(bars, day), 200.5)

    def test_opening_candle_waits_for_last_minute(self) -> None:
        day = date(2026, 10, 13)
        early = parse_bars(
            [
                ["2026-10-13T09:15:00+05:30", 100, 101, 99, 100.5],
                ["2026-10-13T09:16:00+05:30", 100.5, 102, 100, 101],
            ]
        )
        self.assertIsNone(opening_candle(early, day))
        full = parse_bars(
            [
                ["2026-10-13T09:15:00+05:30", 100, 101, 99, 100.5],
                ["2026-10-13T09:18:00+05:30", 101, 103, 100, 102],
                ["2026-10-13T09:19:00+05:30", 102, 104, 101, 103],
                ["2026-10-13T09:20:00+05:30", 110, 111, 109, 110],
            ]
        )
        candle = opening_candle(full, day)
        self.assertIsNotNone(candle)
        assert candle is not None
        self.assertEqual(candle.open, 100)
        self.assertEqual(candle.close, 103)
        self.assertEqual(candle.bars, 3)


class RiskTests(unittest.TestCase):
    def test_capped_stop_tightens_wide_candle(self) -> None:
        # Wide candle open would risk >1.25%; cap pulls stop closer to entry.
        stop = capped_stop("CE", entry=100.0, candle_open=97.0, max_stop_pct=1.25)
        self.assertAlmostEqual(stop, 98.75)
        stop_put = capped_stop("PE", entry=100.0, candle_open=104.0, max_stop_pct=1.25)
        self.assertAlmostEqual(stop_put, 101.25)

    def test_premium_stop(self) -> None:
        self.assertFalse(premium_stop_hit(20.0, 16.0, 25.0))
        self.assertTrue(premium_stop_hit(20.0, 15.0, 25.0))


class TrailTests(unittest.TestCase):
    def test_call_starts_at_candle_open_and_steps_the_target(self) -> None:
        fresh = trail_levels("CE", entry=100, risk=2, initial_stop=98, best_price=100)
        self.assertEqual(fresh.stage, 0)
        self.assertAlmostEqual(fresh.stop, 98)
        self.assertAlmostEqual(fresh.target, 102)

        one_r = trail_levels("CE", entry=100, risk=2, initial_stop=98, best_price=102)
        self.assertEqual(one_r.stage, 1)
        self.assertAlmostEqual(one_r.stop, 100)
        self.assertAlmostEqual(one_r.target, 104)

        two_r = trail_levels("CE", entry=100, risk=2, initial_stop=98, best_price=105)
        self.assertEqual(two_r.stage, 2)
        self.assertAlmostEqual(two_r.stop, 102)
        self.assertAlmostEqual(two_r.target, 106)
        self.assertFalse(stop_hit("CE", 105, two_r.stop))
        self.assertTrue(stop_hit("CE", 102, two_r.stop))

    def test_put_mirrors_the_call(self) -> None:
        fresh = trail_levels("PE", entry=100, risk=3, initial_stop=103, best_price=100)
        self.assertEqual(fresh.stage, 0)
        self.assertAlmostEqual(fresh.stop, 103)
        self.assertAlmostEqual(fresh.target, 97)
        self.assertTrue(stop_hit("PE", 103, fresh.stop))

        locked = trail_levels("PE", entry=100, risk=3, initial_stop=103, best_price=94)
        self.assertEqual(locked.stage, 2)
        self.assertAlmostEqual(locked.stop, 97)
        self.assertAlmostEqual(locked.target, 91)


class NseParseTests(unittest.TestCase):
    def test_index_skips_the_index_row(self) -> None:
        payload = {
            "data": [
                {"symbol": "NIFTY 200", "priority": 1, "pChange": 0.4},
                {"symbol": "TRENT", "pChange": "3.20", "lastPrice": 2900, "previousClose": 2810},
                {"symbol": "INFY", "pChange": -1.5, "lastPrice": "1500", "previousClose": "1520"},
            ]
        }
        rows = parse_index_rows(payload, "NIFTY 200")
        self.assertEqual([row["symbol"] for row in rows], ["TRENT", "INFY"])
        self.assertEqual(rows[0]["pct"], 3.2)
        self.assertEqual(rows[0]["prev_close"], 2810.0)
        self.assertEqual(rows[1]["prev_close"], 1520.0)

    def test_oi_percent_field_names(self) -> None:
        payload = {
            "data": [
                {"symbol": "TRENT", "avgInOI": 86.88},
                {"symbol": "INFY", "pchangeinOpenInterest": 12.5},
                {"symbol": "NIFTY", "avgInOI": 33.0},
                {"underlying": "SBIN", "percentChangeInOI": "4"},
                {"symbol": "WIPRO", "avgInOI": -1.2},
            ]
        }
        rows = {row["symbol"]: row["oi_pct"] for row in parse_oi_rows(payload)}
        self.assertEqual(rows["TRENT"], 86.88)
        self.assertEqual(rows["INFY"], 12.5)
        self.assertEqual(rows["SBIN"], 4.0)
        picked = [row["symbol"] for row in select_spurts(parse_oi_rows(payload), 2)]
        self.assertEqual(picked, ["TRENT", "INFY"])


if __name__ == "__main__":
    unittest.main()
