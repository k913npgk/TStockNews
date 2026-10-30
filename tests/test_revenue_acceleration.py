import copy
from datetime import date
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from tests.test_engine import bars, weekdays
from tests.eps_fixture import ready
from tstocknews import STRATEGY_VERSION
from tstocknews.analysis import recommendations
from tstocknews.cli import daily
from tstocknews.engine import screen
from tstocknews.official import SourceError, month_offset, revenue_windows, universe_and_financials
from tstocknews.storage import read, write


DAY = date(2026, 1, 15)
COMPANIES = [{"market": m, "symbol": s} for m, s in (("twse", "2330"), ("tpex", "6488"))]


class FinancialClient:
    """Official-shaped responses, including a year crossing and both market tables."""
    def __init__(self):
        self.audit, self.urls = [], []
        self.values = {(c["market"], *month_offset(DAY, -d)): (130 if d <= 3 else 110, 100)
                       for c in COMPANIES for d in range(1, 8)}
        self.conflict = False

    def get(self, url, html=False):
        self.urls.append(url)
        market = "twse" if "/sii/" in url or "openapi.twse" in url else "tpex"
        company = next(c for c in COMPANIES if c["market"] == market)
        if not html:
            return [{"公司代號": company["symbol"], "年度": "114", "季別": "3",
                     "基本每股盈餘（元）": "1"}] if url.endswith("_ci") else []
        year, month, category = map(int, re.search(r"t21sc03_(\d+)_(\d+)_(\d+)\.html", url).groups())
        value = self.values.get((market, year + 1911, month))
        if value is None or (category == 1 and not self.conflict):
            return "<table></table>"
        current, prior = value
        if category == 1:
            current += 1
        return (f"<table><tr><td>{company['symbol']}</td><td>Test</td>"
                f"<td>{current}</td><td>0</td><td>{prior}</td></tr></table>")


class RevenueWindowTests(unittest.TestCase):
    def test_sums_not_average_of_monthly_rates_and_crosses_year(self):
        key = ("twse", "2330")
        pairs = [(150, 100), (100, 200), (120, 100), (110, 100), (110, 100), (110, 100)]
        values = {(*key, *month_offset(DAY, -i)): p for i, p in enumerate(pairs, 1)}
        result = revenue_windows(values, key, DAY)
        self.assertEqual(result["revenue_end_month"], "2025-12")
        self.assertEqual([r["month"] for r in result["revenue_months"]],
                         ["2025-12", "2025-11", "2025-10", "2025-09", "2025-08", "2025-07"])
        self.assertAlmostEqual(result["revenue_yoy_3m"], 370 / 400 - 1)
        self.assertAlmostEqual(result["revenue_yoy_prev_3m"], .1)
        self.assertAlmostEqual(result["revenue_growth_acceleration"], 370 / 400 - 1.1)

    def test_missing_prior_window_never_selects_an_older_end_month(self):
        client = FinancialClient()
        key = ("twse", "2330")
        values = {(*key, y, m): v for (market, y, m), v in client.values.items() if market == "twse"}
        del values[(*key, 2025, 7)]
        result = revenue_windows(values, key, DAY)
        self.assertEqual(result["revenue_end_month"], "2025-12")
        self.assertAlmostEqual(result["revenue_yoy_3m"], .3)
        self.assertIsNone(result["revenue_growth_acceleration"])
        self.assertIsNone(result["revenue_months"][-1]["current"])

    def test_invalid_or_nonpositive_prior_base_is_unknown(self):
        key = ("twse", "2330")
        for value in ((110, 0), (110, -100), (float("nan"), 100), (110, float("inf")), None):
            values = {(*key, *month_offset(DAY, -i)): (130, 100) for i in range(1, 7)}
            for i in (4, 5, 6):
                values[(*key, *month_offset(DAY, -i))] = value
            with self.subTest(value=value):
                self.assertIsNone(revenue_windows(values, key, DAY)["revenue_growth_acceleration"])

    def test_official_adapter_reads_seven_months_and_retains_observation_date(self):
        client = FinancialClient()
        with patch("tstocknews.official.today", return_value=DAY), \
             patch("tstocknews.official.ordinary_universe", return_value=COMPANIES):
            result = universe_and_financials(client, DAY.isoformat())
        self.assertEqual(len([u for u in client.urls if "t21sc03_" in u]), 28)
        self.assertEqual(result["observed_date"], DAY.isoformat())
        for row in result["fundamentals"]:
            self.assertAlmostEqual(row["revenue_growth_acceleration"], .2)
            self.assertEqual(row["available_date"], DAY.isoformat())
            self.assertEqual(row["revenue_data_status"], "COMPLETE")
            self.assertEqual(len(row["revenue_months"]), 6)

    def test_not_yet_reported_last_month_falls_back_exactly_one_month(self):
        client = FinancialClient()
        for market in ("twse", "tpex"):
            del client.values[(market, 2025, 12)]
        with patch("tstocknews.official.today", return_value=DAY), \
             patch("tstocknews.official.ordinary_universe", return_value=COMPANIES):
            result = universe_and_financials(client, DAY.isoformat())
        self.assertTrue(all(r["revenue_end_month"] == "2025-11" for r in result["fundamentals"]))
        self.assertTrue(all(r["revenue_growth_acceleration"] is not None for r in result["fundamentals"]))

    def test_conflicting_category_duplicates_remain_unknown(self):
        client = FinancialClient()
        client.conflict = True
        with patch("tstocknews.official.today", return_value=DAY), \
             patch("tstocknews.official.ordinary_universe", return_value=COMPANIES):
            result = universe_and_financials(client, DAY.isoformat())
        self.assertTrue(all(r["revenue_growth_acceleration"] is None for r in result["fundamentals"]))


class ProductionC5Tests(unittest.TestCase):
    def setUp(self):
        self.sessions = weekdays(125)
        self.day = self.sessions[-1]
        self.company = {"market": "twse", "symbol": "2330"}
        self.prices = bars(sessions=self.sessions)
        self.inst = [{**self.company, "date": ds, "net_buy_shares": 1000} for ds in self.sessions[-3:]]
        self.fund = {**self.company, "eps": 1, "revenue_yoy_3m": .2, "revenue_yoy_prev_3m": .1,
                     "available_date": self.day, "fiscal_period": "2025-Q1"}

    def screen(self, row):
        return screen(self.day, self.sessions, [self.company], self.prices, [row], self.inst)

    def test_strict_acceleration_boundary_and_missing_nonfinite_inputs(self):
        for prior, expected_reason in ((.1, None), (.2, "REVENUE_NOT_ACCELERATING"),
                                       (.3, "REVENUE_NOT_ACCELERATING"),
                                       (None, "REVENUE_ACCELERATION_UNKNOWN"),
                                       (float("nan"), "REVENUE_ACCELERATION_UNKNOWN"),
                                       (float("inf"), "REVENUE_ACCELERATION_UNKNOWN")):
            with self.subTest(prior=prior):
                result = self.screen({**self.fund, "revenue_yoy_prev_3m": prior,
                                      "revenue_growth_acceleration": 999})
                self.assertEqual(result["eligible_count"], int(expected_reason is None))
                reasons = result["diagnostics"]["symbols"]["twse:2330"]["reasons"]
                if expected_reason:
                    self.assertIn(expected_reason, reasons)
                else:
                    self.assertAlmostEqual(result["candidates"][0]["revenue_growth_acceleration"], .1)

    def test_acceleration_cannot_override_existing_gates_or_future_snapshots(self):
        result = self.screen({**self.fund, "eps": -1})
        self.assertIn("EPS_NOT_POSITIVE", result["diagnostics"]["symbols"]["twse:2330"]["reasons"])
        result = self.screen({**self.fund, "revenue_yoy_3m": -.1, "revenue_yoy_prev_3m": -.2})
        self.assertIn("REVENUE_GROWTH_NOT_POSITIVE", result["diagnostics"]["symbols"]["twse:2330"]["reasons"])
        result = self.screen({**self.fund, "available_date": "2099-01-01"})
        self.assertEqual(result["eligible_count"], 0)

    def test_source_loss_fails_and_real_zero_acceleration_is_valid_zero_candidates(self):
        financial = ready({"observed_date": self.day, "universe": [self.company], "fundamentals": [self.fund]},
                          target=self.day)
        broken = copy.deepcopy(financial)
        broken["fundamentals"][0].pop("revenue_yoy_prev_3m")
        with self.assertRaisesRegex(SourceError, "no usable revenue acceleration"):
            recommendations(self.day, self.sessions, self.prices, self.inst, broken)
        financial["fundamentals"][0] = {**self.fund, "revenue_yoy_prev_3m": .2}
        self.assertEqual(recommendations(self.day, self.sessions, self.prices, self.inst, financial)["eligible_count"], 0)

    def test_versioned_signals_keep_evidence_and_isolated_tests_match_production(self):
        financial = ready({"observed_date": self.day, "universe": [self.company], "fundamentals": [self.fund]},
                          target=self.day)
        normal = recommendations(self.day, self.sessions, self.prices, self.inst, financial)
        test = recommendations(self.day, self.sessions, self.prices, self.inst, financial, test_run_id="c5")
        self.assertEqual(normal["candidates"], test["candidates"])
        self.assertEqual(normal["strategy_version"], STRATEGY_VERSION)
        self.assertTrue(normal["signals"][0]["signal_id"].startswith(STRATEGY_VERSION + ":"))
        self.assertAlmostEqual(normal["signals"][0]["revenue_growth_acceleration"], .1)
        self.assertTrue(test["signals"][0]["test_only"])

    def test_v8_eps_and_monthly_filters_both_precede_top20(self):
        companies = [{"market": "twse", "symbol": str(1000+i)} for i in range(32)]
        funds = [{**self.fund, **c} for c in companies]
        financial = ready({"observed_date": self.day, "universe": companies, "fundamentals": funds},
                          target=self.day)
        for f in financial['fundamentals'][:12]:
            f['revenue_months'][0]['current'] = 99
            f['revenue_yoy_3m'] = 339 / 300 - 1
        prices = [{**p, **c} for c in companies for p in self.prices]
        inst = [{**p, **c, 'net_buy_shares': 3000 if i < 12 else 1000}
                for i,c in enumerate(companies) for p in self.inst]
        got = recommendations(self.day,self.sessions,prices,inst,financial)
        isolated = recommendations(self.day,self.sessions,prices,inst,financial,test_run_id='v8-top20')
        self.assertEqual(got['eligible_count'],20)
        self.assertEqual([r['symbol'] for r in got['candidates']], [str(1012+i) for i in range(20)])
        self.assertEqual(got['candidates'],isolated['candidates'])
        self.assertEqual(got['diagnostics']['exclusion_counts']['MONTHLY_REVENUE_NOT_ALL_GROWING'],12)

    def test_today_old_snapshot_upgrades_separately_and_old_recommendation_remains_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, reports = Path(tmp) / "data", Path(tmp) / "reports"
            for px in self.prices:
                write(root / "days" / (px["date"] + ".json.gz"),
                      {"date": px["date"], "prices": [px], "institutional": [
                          {**self.company, "date": px["date"], "net_buy_shares": 1000}]})
            old = {"observed_date": self.day, "universe": [self.company],
                   "fundamentals": [{k: v for k, v in self.fund.items() if k != "revenue_yoy_prev_3m"}]}
            path = root / "financials" / (self.day + ".json.gz")
            write(path, old)
            before = path.read_bytes()
            upgraded = {**old, "fundamentals": [self.fund]}
            with patch("tstocknews.cli.today", return_value=date.fromisoformat(self.day)), \
                 patch("tstocknews.cli.holiday_days", return_value=set()), patch("tstocknews.cli.sync"), \
                 patch("tstocknews.cli.universe_and_financials", return_value=upgraded) as fetch, \
                 patch("tstocknews.cli.prepare_eps", side_effect=lambda *args: ready(args[-1], target=args[1])):
                self.assertEqual(daily(root, reports, self.day)["selected"], 1)
                fetch.assert_called_once()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(read(root / "financials" / (self.day + "-c5.json.gz")), upgraded)
                # A crash after saving today's upgraded inputs can resume on a later day.
                result_path = root / "recommendations" / (self.day + ".json")
                expected = read(result_path)
                result_path.unlink()
                fetch.reset_mock()
                with patch("tstocknews.cli.today", return_value=date(2026, 10, 29)):
                    self.assertEqual(daily(root, reports, self.day)["selected"], 1)
                self.assertEqual(read(result_path), expected)
                fetch.assert_not_called()
                result_path = root / "recommendations" / (self.day + ".json")
                archived = read(result_path)
                archived["strategy_version"] = "rules-v5-institutional-net-buy-top20"
                write(result_path, archived)
                fetch.reset_mock()
                self.assertEqual(daily(root, reports, self.day)["strategy_version"], archived["strategy_version"])
                self.assertEqual(read(result_path), archived)
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
