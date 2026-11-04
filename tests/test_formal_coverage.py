from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.eps_fixture import ready
from tests.test_engine import bars, weekdays
from tstocknews.analysis import require_complete_financials
from tstocknews.cli import daily, load_history
from tstocknews.official import SourceError
from tstocknews.storage import read, write


class FormalCoverageTests(unittest.TestCase):
    def test_rollover_uses_prior_eps_and_preserves_today_raw_snapshot(self):
        sessions = weekdays(120)
        prior_day, day = sessions[-2:]
        company = {"market": "twse", "symbol": "2330", "name": "Test"}
        prior = {**company, "fiscal_period": "2025-Q1", "eps": 2, "available_date": prior_day}
        current = {**company, "fiscal_period": "", "eps": None, "available_date": day,
                   "revenue_yoy_3m": .2, "revenue_yoy_prev_3m": .1}
        financial = {"observed_date": day, "universe": [company], "fundamentals": [current]}

        with tempfile.TemporaryDirectory() as tmp:
            root, reports = Path(tmp) / "data", Path(tmp) / "reports"
            for price in bars(sessions=sessions):
                ds = price["date"]
                write(root / "days" / f"{ds}.json.gz", {"date": ds, "prices": [price],
                      "institutional": [{**company, "date": ds, "net_buy_shares": 10000}]})
            path = root / "financials" / f"{day}.json.gz"
            write(path, financial)
            write(root / "financials" / f"{prior_day}.json.gz",
                  {"observed_date": prior_day, "fundamentals": [prior]})

            def prepare_comparisons(root, target, sessions, prices, institutions, restored):
                row = restored["fundamentals"][0]
                self.assertEqual((row["eps"], row["fiscal_period"], row["eps_observed_date"]),
                                 (2, "2025-Q1", prior_day))
                self.assertEqual(row["revenue_yoy_3m"], .2)
                return ready(restored, target=target)

            with patch("tstocknews.cli.holiday_days", return_value=set()), patch("tstocknews.cli.sync"), \
                 patch("tstocknews.cli.prepare_eps", side_effect=prepare_comparisons):
                self.assertEqual(daily(root, reports, day)["selected"], 1)
            self.assertEqual(read(path), financial)
            self.assertIn("EPS 沿用已封存財報：1 檔", (reports / f"{day}.md").read_text(encoding="utf-8"))

    def test_only_decision_relevant_unknowns_block_publication(self):
        row = {"reasons": ["EPS_UNKNOWN", "MACD_GATE_FAILED"]}
        result = {"diagnostics": {"symbols": {"twse:2330": row}}}
        require_complete_financials(result)
        for reason in ("EPS_UNKNOWN", "REVENUE_ACCELERATION_UNKNOWN", "EPS_COMPARISON_UNKNOWN",
                       "MONTHLY_REVENUE_COMPARISON_UNKNOWN"):
            row["reasons"] = [reason]
            with self.subTest(reason=reason), self.assertRaisesRegex(SourceError, reason):
                require_complete_financials(result)

    def test_formal_history_rejects_partial_market_snapshot(self):
        day = "2026-11-04"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = {"market": "twse", "symbol": "2330", "date": day}
            write(root / "days" / f"{day}.json.gz", {"date": day, "prices": [row],
                  "institutional": [row], "source_audit": [{"url": "https://example.invalid"}]})
            with self.assertRaisesRegex(SourceError, "Incomplete market coverage"):
                load_history(root)
