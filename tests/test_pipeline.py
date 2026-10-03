import io
import os
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tstocknews.cli import daily, main, push
from tstocknews.line import prepare, send
from tstocknews.official import SourceError, RevenueTable, inst_rows, quote_rows, month_offset, sync
from tstocknews.storage import read, write
from tests.test_engine import weekdays, bars


class AdapterTests(unittest.TestCase):
    def test_quote_units_dates_and_missing_prices(self):
        value = {"date": "20261002", "tables": [{"fields": ["代號", "名稱", "開盤", "最高", "最低", "收盤", "成交股數"],
                  "data": [["1234", "甲", "10", "12", "9", "11", "2,000,000"], ["5678", "乙", "--", "--", "--", "--", "0"]]}]}
        rows = quote_rows(value, "tpex", "2026-10-02")
        self.assertEqual(rows[0]["volume_shares"], 2_000_000)
        self.assertIsNone(rows[1]["open"])
        with self.assertRaises(SourceError):
            quote_rows(value, "tpex", "2026-10-01")

    def test_institutional_total_is_not_double_counted(self):
        value = {"title": "115年10月02日 三大法人", "fields": ["證券代號", "自營商買賣超股數", "三大法人買賣超股數"],
                 "data": [["1234", "123", "2,345"]]}
        self.assertEqual(inst_rows(value, "twse", "2026-10-02")[0]["net_buy_shares"], 2345)
        value["title"] = "115年10月01日"
        with self.assertRaises(SourceError):
            inst_rows(value, "twse", "2026-10-02")

    def test_revenue_parser_and_year_boundary(self):
        parser = RevenueTable()
        parser.feed('<table><tr><th>公司代號</th><th>名稱</th></tr><tr><td>1234</td><td>甲</td><td>2,000</td><td>1,000</td><td>1,500</td></tr></table>')
        self.assertEqual(parser.rows[1][2], "2,000")
        from datetime import date
        self.assertEqual(month_offset(date(2026, 1, 3), -1), (2025, 12))


class LineTests(unittest.TestCase):
    def test_webhook_signature_and_group_setup_command(self):
        import base64, hashlib, hmac, json
        from tstocknews.webhook import verified_groups
        body = json.dumps({"events": [{"source": {"type": "group", "groupId": "Ctest"},
                                     "message": {"type": "text", "text": "TStockNews 設定"}}]}).encode()
        signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
        self.assertEqual(verified_groups(body, signature, "secret"), ["Ctest"])
        with self.assertRaises(ValueError):
            verified_groups(body + b" ", signature, "secret")

    def fake(self, calls, quota=200, consumed=0):
        def request(path, token, payload=None, retry_key=None):
            calls.append((path, payload, retry_key))
            if path == "message/quota":
                return {"type": "limited", "value": quota}
            if path == "message/quota/consumption":
                return {"totalUsage": consumed}
            if path.endswith("members/count"):
                return {"count": 2}
            return {}
        return request

    @patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": "test", "LINE_GROUP_ID": "testgroup"})
    def test_quota_blocks_and_paid_plan_refused(self):
        for quota, consumed, status in [(200, 199, "QUOTA_BLOCKED"), (3000, 0, "PLAN_NOT_FREE")]:
            calls = []
            out = send("report", prepare("h"), self.fake(calls, quota, consumed))
            self.assertEqual(out["status"], status)
            self.assertFalse(any(c[0] == "message/push" for c in calls))

    @patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": "test", "LINE_GROUP_ID": "testgroup"})
    def test_single_push_and_idempotent_sent_record(self):
        calls = []
        pending = prepare("h")
        out = send("a" * 6000, pending, self.fake(calls))
        pushes = [c for c in calls if c[0] == "message/push"]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(len(pushes[0][1]["messages"]), 2)
        self.assertEqual(pushes[0][2], pending["retry_key"])
        self.assertEqual(send("report", out, self.fake(calls)), out)
        self.assertEqual(len([c for c in calls if c[0] == "message/push"]), 1)

    def test_expired_retry_is_blocked_before_network(self):
        pending = {**prepare("h"), "prepared_at": "2020-01-01T00:00:00+00:00"}
        self.assertEqual(send("x", pending, lambda *a: self.fail("network call"))["status"], "RETRY_WINDOW_EXPIRED")


class PipelineTests(unittest.TestCase):
    def test_known_typhoon_closure_does_not_fetch_or_publish(self):
        with tempfile.TemporaryDirectory() as tmp, patch("tstocknews.cli.Client") as client:
            root = Path(tmp) / "data"
            status = daily(root, Path(tmp) / "reports", "2026-07-10")
            self.assertEqual(status["status"], "NON_TRADING_DAY")
            client.assert_not_called()
            self.assertFalse((root / "recommendations").exists())

    def test_sync_resumes_cached_day_and_skips_confirmed_closures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = {"date": "2026-07-09", "prices": [], "institutional": []}
            write(root / "days" / "2026-07-09.json.gz", saved)
            write(root / "extra_closures.json", {"2026-07-14": "https://example.org/closure"})
            collected = {"date": "2026-07-13", "prices": [], "institutional": []}
            with patch("tstocknews.official.holiday_days", return_value=set()), \
                 patch("tstocknews.official.ordinary_universe", return_value=[]), \
                 patch("tstocknews.official.collect_day", return_value=collected) as collect:
                sync(root, "2026-07-09", "2026-07-14")
                self.assertEqual([call.args[1] for call in collect.call_args_list], ["2026-07-13"])
            self.assertEqual(read(root / "days" / "2026-07-09.json.gz"), saved)
            self.assertFalse((root / "days" / "2026-07-10.json.gz").exists())
            self.assertFalse((root / "days" / "2026-07-14.json.gz").exists())

    def test_unconfirmed_missing_session_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("tstocknews.official.holiday_days", return_value=set()), \
             patch("tstocknews.official.ordinary_universe", return_value=[]), \
             patch("tstocknews.official.collect_day", side_effect=SourceError("missing quotes")):
            root = Path(tmp)
            with self.assertRaises(SourceError):
                sync(root, "2026-07-13", "2026-07-13")
            self.assertFalse((root / "days" / "2026-07-13.json.gz").exists())

    def test_bootstrap_failure_keeps_checkpoint_and_can_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def partial_sync(root, start, end, progress):
                write(root / "days" / "2026-07-09.json.gz",
                      {"date": "2026-07-09", "prices": [], "institutional": []})
                progress("2026-07-09")
                raise SourceError("missing quotes for 2026-07-13")
            argv = ["--data-dir", str(root), "bootstrap", "--days", "4", "--end", "2026-07-13"]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with patch("tstocknews.cli.sync", side_effect=partial_sync):
                    self.assertEqual(main(argv), 1)
                status = read(root / "status.json")
                self.assertEqual(status["status"], "BOOTSTRAP_FAILED")
                self.assertEqual(status["last_completed"], "2026-07-09")
                self.assertEqual(status["start"], "2026-07-09")
                self.assertEqual(status["end"], "2026-07-13")
                self.assertEqual(status["error"], read(root / "error.json"))
                # Failure before any new progress must retain the earlier checkpoint.
                with patch("tstocknews.cli.sync", side_effect=SourceError("still missing")):
                    self.assertEqual(main(argv), 1)
                self.assertEqual(read(root / "status.json")["last_completed"], "2026-07-09")
                with patch("tstocknews.cli.sync"):
                    self.assertEqual(main(argv), 0)
                self.assertEqual(read(root / "status.json")["status"], "BOOTSTRAP_COMPLETE")
                self.assertFalse((root / "error.json").exists())

    def test_atomic_gzip_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "day.json.gz"
            write(path, {"中文": [1, None]})
            self.assertEqual(read(path), {"中文": [1, None]})
            self.assertFalse(path.with_suffix(".gz.tmp").exists())

    def test_daily_report_survives_rerun_without_replacing_signals(self):
        sessions = weekdays(125)
        day = sessions[-1]
        universe = [{"symbol": "2330", "market": "twse", "name": "Test"}]
        financial = {"universe": universe, "fundamentals": [{**universe[0], "eps": 1,
                       "revenue_yoy_3m": .2, "available_date": day, "fiscal_period": "2025-Q1"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root, reports = Path(tmp) / "data", Path(tmp) / "reports"
            for px in bars(sessions=sessions):
                write(root / "days" / (px["date"] + ".json.gz"), {"date": px["date"], "prices": [px],
                      "institutional": [{"date": px["date"], "symbol": "2330", "market": "twse", "net_buy_shares": 10000}]})
            write(root / "financials" / (day + ".json.gz"), financial)
            with patch("tstocknews.cli.holiday_days", return_value=set()), patch("tstocknews.cli.sync"):
                first = daily(root, reports, day)
                self.assertEqual(first["selected"], 1)
                original = read(root / "recommendations" / (day + ".json"))
                text = (reports / (day + ".md")).read_text(encoding="utf-8")
                financial["fundamentals"][0]["eps"] = -1
                write(root / "financials" / (day + ".json.gz"), financial)
                daily(root, reports, day)
                self.assertEqual(read(root / "recommendations" / (day + ".json")), original)
                self.assertEqual((reports / (day + ".md")).read_text(encoding="utf-8"), text)
                outcomes = read(root / "performance" / (day + ".json"))
                self.assertTrue(all(row["status"] == "PENDING_ENTRY" for row in outcomes))
                self.assertIn("法人買超比例 +0.40%", text)

    def test_weekend_does_not_fetch_or_publish(self):
        with tempfile.TemporaryDirectory() as tmp, patch("tstocknews.cli.sync") as sync:
            status = daily(Path(tmp) / "data", Path(tmp) / "reports", "2026-10-03")
            self.assertEqual(status["status"], "NON_TRADING_DAY")
            sync.assert_not_called()
