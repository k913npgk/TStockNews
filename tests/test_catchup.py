from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_engine import bars
from tstocknews.analysis import recommendations
from tstocknews.catchup import validate
from tstocknews.cli import daily
from tstocknews.official import KNOWN_EXTRA_CLOSURES, SourceError, sync
from tstocknews.report import render
from tstocknews.storage import read, write


DAY, OBSERVED = "2026-10-28", "2026-10-29"
BEFORE_OPEN = datetime.fromisoformat("2026-10-29T00:30:00+08:00")
AT_OPEN = datetime.fromisoformat("2026-10-29T09:00:00+08:00")


class CatchupTests(unittest.TestCase):
    def fixture(self):
        sessions, current = [], date.fromisoformat(DAY)
        while len(sessions) < 125:
            if current.weekday() < 5 and current.isoformat() not in KNOWN_EXTRA_CLOSURES:
                sessions.append(current.isoformat())
            current -= timedelta(days=1)
        sessions.reverse()
        company = {"market": "twse", "symbol": "2330", "name": "Test"}
        financial = {"observed_date": OBSERVED, "universe": [company], "fundamentals": [
            {**company, "eps": 1, "revenue_yoy_3m": .2, "revenue_yoy_prev_3m": .1,
             "available_date": OBSERVED, "fiscal_period": "2026-Q2"}]}
        prices = bars(sessions=sessions)
        inst = [{**company, "date": ds, "net_buy_shares": 1000} for ds in sessions[-3:]]
        return sessions, financial, prices, inst

    def seed(self, root):
        sessions, financial, prices, inst = self.fixture()
        for px in prices:
            write(root / "days" / (px["date"] + ".json.gz"), {"date": px["date"], "prices": [px],
                  "institutional": [r for r in inst if r["date"] == px["date"]]})
        write(root / "financials" / (OBSERVED + ".json.gz"), financial)

    def test_only_explicit_pair_before_open_is_authorized(self):
        with patch("tstocknews.catchup.now", return_value=BEFORE_OPEN):
            self.assertTrue(validate(DAY, OBSERVED)["approved_formal_catchup"])
            for day, observed in (("2026-10-27", OBSERVED), (DAY, "2026-10-30")):
                with self.assertRaisesRegex(SourceError, "No approved"):
                    validate(day, observed)
        for stamp in (AT_OPEN, AT_OPEN + timedelta(seconds=1), BEFORE_OPEN + timedelta(days=1),
                      BEFORE_OPEN - timedelta(days=1)):
            with patch("tstocknews.catchup.now", return_value=stamp), self.assertRaisesRegex(SourceError, "before"):
                validate(DAY, OBSERVED)

    def test_formal_catchup_preserves_real_dates_and_ignores_future_market_data(self):
        sessions, financial, prices, inst = self.fixture()
        with self.assertRaisesRegex(SourceError, "Production cannot use"):
            recommendations(DAY, sessions, prices, inst, financial)
        future = {**prices[-1], "date": OBSERVED, "close": 9999, "high": 9999}
        with patch("tstocknews.catchup.now", return_value=BEFORE_OPEN):
            result = recommendations(DAY, sessions + [OBSERVED], prices + [future], inst, financial,
                                     late_financial_date=OBSERVED)
        signal = result["signals"][0]
        self.assertEqual(signal["date"], DAY)
        self.assertEqual(signal["fundamental_available_date"], OBSERVED)
        self.assertEqual(signal["generated_at"], BEFORE_OPEN.isoformat())
        self.assertEqual(signal["close"], prices[-1]["close"])
        self.assertFalse(signal.get("test_only", False))
        self.assertEqual(result["diagnostics"]["fundamental_as_of"], OBSERVED)
        text = render(DAY, result, [], result["strategy_version"])
        self.assertIn("實際補跑時間：2026-10-29", text)
        self.assertIn("財報／營收取得日：2026-10-29", text)

    def test_exception_does_not_allow_other_observation_dates_or_test_mode(self):
        sessions, financial, prices, inst = self.fixture()
        with patch("tstocknews.catchup.now", return_value=BEFORE_OPEN):
            with self.assertRaisesRegex(SourceError, "actual approved observation"):
                recommendations(DAY, sessions, prices, inst, {**financial, "observed_date": "2026-10-30"},
                                late_financial_date=OBSERVED)
            with self.assertRaisesRegex(SourceError, "combined with a test"):
                recommendations(DAY, sessions, prices, inst, financial, test_run_id="bad", late_financial_date=OBSERVED)

    def test_pipeline_keeps_late_snapshot_separate_and_resume_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, reports = Path(tmp) / "data", Path(tmp) / "reports"
            self.seed(root)
            with patch("tstocknews.cli.today", return_value=date.fromisoformat(OBSERVED)), \
                 patch("tstocknews.cli.holiday_days", return_value=set()), patch("tstocknews.cli.sync"), \
                 patch("tstocknews.cli.universe_and_financials") as fetch, \
                 patch("tstocknews.catchup.now", return_value=BEFORE_OPEN):
                status = daily(root, reports, DAY, late_financial_date=OBSERVED)
                self.assertEqual(status["selected"], 1)
                self.assertTrue(status["approved_formal_catchup"])
                self.assertFalse((root / "financials" / (DAY + ".json.gz")).exists())
                rec = root / "recommendations" / (DAY + ".json")
                before = rec.read_bytes()
                report_before = (reports / (DAY + ".md")).read_bytes()
                with patch("tstocknews.catchup.now", return_value=AT_OPEN):
                    self.assertEqual(daily(root, reports, DAY)["selected"], 1)
                self.assertEqual(rec.read_bytes(), before)
                self.assertEqual((reports / (DAY + ".md")).read_bytes(), report_before)
                fetch.assert_not_called()
                self.assertTrue(all(r["status"] == "PENDING_ENTRY" for r in read(root / "performance" / (DAY + ".json"))))

    def test_crossing_open_during_calculation_creates_no_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, reports = Path(tmp) / "data", Path(tmp) / "reports"
            self.seed(root)
            with patch("tstocknews.cli.today", return_value=date.fromisoformat(OBSERVED)), \
                 patch("tstocknews.cli.holiday_days", return_value=set()), patch("tstocknews.cli.sync"), \
                 patch("tstocknews.catchup.now", side_effect=[BEFORE_OPEN, BEFORE_OPEN, AT_OPEN]), \
                 self.assertRaisesRegex(SourceError, "before"):
                daily(root, reports, DAY, late_financial_date=OBSERVED)
            self.assertFalse((root / "recommendations" / (DAY + ".json")).exists())
            self.assertFalse((reports / (DAY + ".md")).exists())

    def test_next_session_holiday_cannot_be_treated_as_an_entry_day(self):
        with tempfile.TemporaryDirectory() as tmp, patch("tstocknews.catchup.now", return_value=BEFORE_OPEN), \
             patch("tstocknews.cli.holiday_days", return_value={OBSERVED}), patch("tstocknews.cli.sync") as collect:
            with self.assertRaisesRegex(SourceError, "not an exchange session"):
                daily(Path(tmp), Path(tmp) / "reports", DAY, late_financial_date=OBSERVED)
            collect.assert_not_called()

    def test_confirmed_oct23_closure_is_skipped_without_any_quote_request(self):
        with tempfile.TemporaryDirectory() as tmp, patch("tstocknews.official.holiday_days", return_value={"2026-10-26"}), \
             patch("tstocknews.official.ordinary_universe", return_value=[]), \
             patch("tstocknews.official.collect_day", side_effect=lambda c, day, allowed: {"date": day}) as collect:
            sync(Path(tmp), "2026-10-23", DAY)
            self.assertEqual([c.args[1] for c in collect.call_args_list], ["2026-10-27", DAY])


if __name__ == "__main__":
    unittest.main()
