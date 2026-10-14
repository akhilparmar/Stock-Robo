from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from index_tester.indicators import cpr
from index_tester.market import prepare
from index_tester.strategy import (
    BarView,
    atm_strike,
    breakeven_trigger_hit,
    build_trade_plan,
    debit_allowed,
    decide_entry,
    in_entry_window,
    index_stop,
    index_stop_hit,
    index_target_hit,
    next_premium_trail,
    premium_stop_hit,
    premium_take_hit,
    premium_trail_hit,
    profit_r,
    supertrend_exit_reason,
    trailing_stop_for_stage,
)

IST = ZoneInfo("Asia/Kolkata")


def view(**overrides) -> BarView:
    base = dict(
        ready=True,
        close=110,
        vwap=100,
        ema9=108,
        ema21=104,
        ema9_15=107,
        ema21_15=103,
        adx15=28,
        atr5=12,
        supertrend_dir=1,
        prev_supertrend_dir=1,
        orb_high=105,
        orb_low=95,
        cpr_top=102,
        cpr_bottom=98,
        bar_ts="2026-10-08T10:00",
    )
    base.update(overrides)
    return BarView(**base)


class RuleTests(unittest.TestCase):
    def test_atm_and_budget(self) -> None:
        self.assertEqual(atm_strike(24432), 24450)
        self.assertEqual(atm_strike(24424), 24400)
        self.assertTrue(debit_allowed(150, 75, 20000))
        self.assertFalse(debit_allowed(300, 75, 20000))

    def test_entry_window(self) -> None:
        self.assertFalse(in_entry_window(9 * 60 + 29))
        self.assertTrue(in_entry_window(9 * 60 + 30))
        self.assertTrue(in_entry_window(11 * 60 + 30))
        self.assertFalse(in_entry_window(12 * 60))
        self.assertTrue(in_entry_window(13 * 60 + 15))
        self.assertTrue(in_entry_window(14 * 60 + 45))
        self.assertFalse(in_entry_window(14 * 60 + 46))

    def test_call_on_opening_range_break(self) -> None:
        decision = decide_entry(view(), 10 * 60, position_open=False)
        self.assertEqual(decision.action, "buy_ce")
        self.assertEqual(decision.reason, "call_above_vwap_ema_orb")

    def test_put_is_the_mirror(self) -> None:
        decision = decide_entry(
            view(
                close=90,
                vwap=100,
                ema9=92,
                ema21=96,
                ema9_15=93,
                ema21_15=97,
                orb_high=105,
                orb_low=95,
                cpr_top=102,
                cpr_bottom=98,
            ),
            10 * 60,
            position_open=False,
        )
        self.assertEqual(decision.action, "buy_pe")
        self.assertEqual(decision.reason, "put_below_vwap_ema_orb")

    def test_low_adx_and_window_block_entries(self) -> None:
        quiet = decide_entry(view(adx15=18), 10 * 60, position_open=False)
        self.assertEqual(quiet.reason, "adx_too_low")
        again = decide_entry(view(), 10 * 60, position_open=False)
        self.assertEqual(again.action, "buy_ce")
        lunch = decide_entry(view(), 12 * 60, position_open=False)
        self.assertEqual(lunch.reason, "outside_entry_window")

    def test_exits(self) -> None:
        self.assertEqual(supertrend_exit_reason("CE", 1, -1), "supertrend_flip")
        self.assertIsNone(supertrend_exit_reason("CE", -1, -1))
        self.assertEqual(supertrend_exit_reason("PE", -1, 1), "supertrend_flip")
        stop = index_stop("CE", 24000, 20)
        self.assertAlmostEqual(stop, 24000 - 25)
        self.assertTrue(index_stop_hit("CE", stop, stop))
        self.assertFalse(index_stop_hit("PE", stop, stop + 1))
        self.assertTrue(premium_stop_hit(100, 65))
        self.assertFalse(premium_stop_hit(100, 70))

    def test_adaptive_trade_plans(self) -> None:
        developing = build_trade_plan("CE", 24000, 20, 25)
        self.assertEqual(developing.regime, "developing")
        self.assertEqual(developing.reward_ratio, 0.5)
        self.assertAlmostEqual(developing.stop, 23982)
        self.assertAlmostEqual(developing.target, 24009)

        trending = build_trade_plan("PE", 24000, 20, 32)
        self.assertEqual(trending.regime, "trending")
        self.assertEqual(trending.reward_ratio, 0.75)
        self.assertAlmostEqual(trending.stop, 24022)
        self.assertAlmostEqual(trending.target, 23983.5)

        strong = build_trade_plan("CE", 24000, 20, 45)
        self.assertEqual(strong.regime, "strong_trend")
        self.assertEqual(strong.reward_ratio, 1.0)
        self.assertAlmostEqual(strong.stop, 23975)
        self.assertAlmostEqual(strong.target, 24025)
        self.assertTrue(index_target_hit("CE", 24025, strong.target))
        self.assertTrue(breakeven_trigger_hit("CE", 24025, 24000, 25))
        self.assertFalse(breakeven_trigger_hit("CE", 24024, 24000, 25))

    def test_two_stage_trailing_stop(self) -> None:
        self.assertAlmostEqual(profit_r("CE", 24025, 24000, 25), 1.0)
        self.assertAlmostEqual(profit_r("PE", 23962.5, 24000, 25), 1.5)
        self.assertAlmostEqual(trailing_stop_for_stage("CE", 24000, 25, 1), 24000)
        self.assertAlmostEqual(trailing_stop_for_stage("CE", 24000, 25, 2), 24012.5)
        self.assertAlmostEqual(trailing_stop_for_stage("PE", 24000, 25, 1), 24000)
        self.assertAlmostEqual(trailing_stop_for_stage("PE", 24000, 25, 2), 23987.5)

    def test_premium_trail_locks_then_trails(self) -> None:
        buy = 100.0
        first = next_premium_trail(
            buy, 100.5, peak_premium=buy, premium_stop=None, trail_stage=0
        )
        self.assertEqual(first.trail_stage, 1)
        self.assertAlmostEqual(first.premium_stop, 100.0)
        self.assertFalse(premium_trail_hit(100.5, first.premium_stop))

        locked = next_premium_trail(
            buy,
            101.2,
            peak_premium=first.peak_premium,
            premium_stop=first.premium_stop,
            trail_stage=first.trail_stage,
        )
        self.assertGreaterEqual(locked.trail_stage, 2)
        self.assertGreaterEqual(locked.premium_stop, 101.0)

        peaked = next_premium_trail(
            buy,
            110.0,
            peak_premium=locked.peak_premium,
            premium_stop=locked.premium_stop,
            trail_stage=locked.trail_stage,
        )
        self.assertAlmostEqual(peaked.peak_premium, 110.0)
        self.assertAlmostEqual(peaked.premium_stop, 108.9)
        self.assertTrue(premium_trail_hit(108.9, peaked.premium_stop))
        self.assertFalse(premium_trail_hit(109.0, peaked.premium_stop))
        self.assertTrue(premium_take_hit(100.0, 108.0))
        self.assertFalse(premium_take_hit(100.0, 107.0))

    def test_cpr_and_opening_range(self) -> None:
        top, bottom, _pivot = cpr(120, 80, 110)
        self.assertAlmostEqual(top, 106.666666, places=2)
        self.assertAlmostEqual(bottom, 100)

        frame = _sessions()
        now = datetime(2026, 10, 8, 10, 3, tzinfo=IST)
        built = prepare(frame, now, instrument="NIFTY")
        self.assertEqual(built.session_date, "2026-10-08")
        self.assertEqual(built.orb_high, 110)
        self.assertEqual(built.orb_low, 110)
        self.assertEqual(built.cpr_top, 100)
        self.assertEqual(built.cpr_bottom, 100)
        self.assertIn("09:55", built.bar_ts)


def _sessions() -> pd.DataFrame:
    rows = []
    rows.extend(_day(datetime(2026, 10, 7, 9, 15, tzinfo=IST), 375, 100))
    rows.extend(_day(datetime(2026, 10, 8, 9, 15, tzinfo=IST), 50, 110))
    return pd.DataFrame(rows)


def _day(start: datetime, count: int, price: float) -> list[dict]:
    rows = []
    for i in range(count):
        ts = start + timedelta(minutes=i)
        rows.append({"ts": ts, "open": price, "high": price, "low": price, "close": price, "volume": 1})
    return rows


if __name__ == "__main__":
    unittest.main()
