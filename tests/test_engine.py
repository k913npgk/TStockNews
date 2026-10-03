import unittest
from datetime import date, timedelta

from tstocknews.engine import _ema, _indicators, screen, track


def weekdays(count):
    days = []
    day = date(2025, 1, 1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def bars(symbol="2330", market="twse", sessions=None, close_values=None, volume=2_500_000):
    sessions = sessions or weekdays(125)
    if close_values is None:
        close_values = [100.0] * len(sessions)
        # A deep pullback followed by a sharp rebound yields a real last-session
        # K/D cross while DIF rises on its final three observations.
        close_values[-4:] = [50.0, 50.0, 80.0, 130.0]
    out = []
    for ds, close in zip(sessions, close_values):
        out.append({
            "date": ds, "symbol": symbol, "market": market, "name": "Test",
            "open": close - 0.5, "high": max(close, 100.0) + 1.0,
            "low": min(close, 100.0) - 1.0, "close": close,
            "volume_shares": volume,
        })
    return out


class IndicatorTests(unittest.TestCase):
    def test_ema_is_seeded_with_first_close(self):
        self.assertEqual(_ema([10.0, 13.0], 3), [10.0, 11.5])

    def test_kd_initialization_and_macd_values_are_finite(self):
        rows = bars()[:10]
        values = _indicators(rows)
        self.assertEqual(values["rsv"][0], 50.0)
        self.assertEqual(values["k"][0], 50.0)
        self.assertEqual(values["d"][0], 50.0)
        self.assertTrue(all(value == value for seq in values.values() for value in seq))


class ScreenTests(unittest.TestCase):
    def test_point_in_time_fundamentals_and_all_gates(self):
        sessions = weekdays(125)
        px = bars(sessions=sessions)
        inst = [
            {"date": ds, "symbol": "2330", "market": "twse", "net_buy_shares": 25_000}
            for ds in sessions[-3:]
        ]
        fundamentals = [
            {"symbol": "2330", "market": "twse", "eps": 1.0, "revenue_yoy_3m": 0.2,
             "available_date": sessions[-1], "fiscal_period": "2025Q1"},
            # Newer row published after as_of must never replace the available snapshot.
            {"symbol": "2330", "market": "twse", "eps": -9.0, "revenue_yoy_3m": -0.5,
             "available_date": "2099-01-01", "fiscal_period": "2098Q4"},
        ]
        result = screen(sessions[-1], sessions, [{"symbol": "2330", "market": "twse", "name": "Test"}], px, fundamentals, inst)
        self.assertEqual(result["eligible_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["fundamental_available_date"], sessions[-1])
        self.assertEqual(candidate["fiscal_period"], "2025Q1")
        self.assertGreater(candidate["institutional_buy_volume_ratio"], 0)
        self.assertEqual(candidate["reasons"], [])

    def test_gap_in_last_120_sessions_excludes_without_filling(self):
        sessions = weekdays(125)
        px = bars(sessions=sessions)
        px = [row for row in px if row["date"] != sessions[-5]]
        result = screen(sessions[-1], sessions, [{"symbol": "2330", "market": "twse", "name": "Test"}], px, [], [])
        self.assertEqual(result["candidates"], [])
        self.assertIn("INSUFFICIENT_OR_GAPPED_PRICE_HISTORY", result["diagnostics"]["symbols"]["twse:2330"]["reasons"])

    def test_unresolved_corporate_action_blocks_candidate(self):
        sessions = weekdays(125)
        px = bars(sessions=sessions)
        px[-1]["corporate_action_unresolved"] = True
        result = screen(sessions[-1], sessions, [{"symbol": "2330", "market": "twse", "name": "Test"}], px, [], [])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["diagnostics"]["exclusion_counts"]["CORPORATE_ACTION_UNRESOLVED"], 1)

    def test_ranking_is_deterministic_and_caps_at_ten(self):
        sessions = weekdays(125)
        universe = []
        px, fundamentals, inst = [], [], []
        for i in range(12):
            symbol = f"{1000 + i:04d}"
            universe.append({"symbol": symbol, "market": "twse", "name": symbol})
            px.extend(bars(symbol=symbol, sessions=sessions, volume=2_500_000))
            fundamentals.append({"symbol": symbol, "market": "twse", "eps": 1, "revenue_yoy_3m": 0.2, "available_date": sessions[-1], "fiscal_period": "2025Q1"})
            inst.extend({"date": ds, "symbol": symbol, "market": "twse", "net_buy_shares": (i + 1) * 1000} for ds in sessions[-3:])
        first = screen(sessions[-1], sessions, universe, px, fundamentals, inst)
        second = screen(sessions[-1], sessions, universe, px, fundamentals, inst)
        self.assertEqual(len(first["candidates"]), 10)
        self.assertEqual(first, second)
        self.assertEqual(first["candidates"][0]["symbol"], "1011")


class TrackingTests(unittest.TestCase):
    def test_horizons_use_market_sessions_and_open_to_close_prices(self):
        sessions = ["2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10", "2025-01-13", "2025-01-14", "2025-01-15", "2025-01-16", "2025-01-17"]
        px = [{"date": ds, "symbol": "2330", "market": "twse", "open": 100.0, "close": 106.0 if i == 10 else 103.0} for i, ds in enumerate(sessions)]
        output = track([{"signal_id": "a", "date": sessions[0], "symbol": "2330", "market": "twse"}], sessions, px, sessions[-1])
        by_horizon = {item["horizon"]: item for item in output}
        self.assertEqual(by_horizon[1]["entry_date"], "2025-01-06")
        self.assertEqual(by_horizon[5]["exit_date"], "2025-01-10")
        self.assertAlmostEqual(by_horizon[10]["price_return"], 0.06)
        self.assertIs(by_horizon[10]["research_event"], True)

    def test_missing_entry_price_and_pending_horizon_preserve_nulls(self):
        sessions = weekdays(6)
        px = [{"date": ds, "symbol": "2330", "market": "twse", "open": 100, "close": 101} for ds in sessions if ds != sessions[1]]
        output = track([{"signal_id": "a", "date": sessions[0], "symbol": "2330", "market": "twse"}], sessions, px, sessions[2])
        by_horizon = {item["horizon"]: item for item in output}
        self.assertEqual(by_horizon[1]["status"], "MISSING_PRICE")
        self.assertIsNone(by_horizon[1]["price_return"])
        self.assertIsNone(by_horizon[1]["research_event"])

    def test_suspension_and_corporate_action_are_explicit(self):
        sessions = weekdays(4)
        px = [
            {"date": sessions[1], "symbol": "2330", "market": "twse", "open": 100, "close": 101, "suspended": True},
        ]
        output = track([{"signal_id": "a", "date": sessions[0], "symbol": "2330", "market": "twse"}], sessions, px, sessions[-1])
        self.assertEqual(output[0]["status"], "SUSPENDED")
        self.assertIsNone(output[0]["price_return"])

        px = [
            {"date": sessions[1], "symbol": "2330", "market": "twse", "open": 100, "close": 101},
            {"date": sessions[2], "symbol": "2330", "market": "twse", "close": 102, "corporate_action_unresolved": True},
            {"date": sessions[3], "symbol": "2330", "market": "twse", "close": 105},
        ]
        output = track([{"signal_id": "a", "date": sessions[0], "symbol": "2330", "market": "twse"}], sessions, px, sessions[-1])
        self.assertEqual(output[1]["status"], "CORPORATE_ACTION_UNRESOLVED")
        self.assertIsNone(output[1]["price_return"])


if __name__ == "__main__":
    unittest.main()
