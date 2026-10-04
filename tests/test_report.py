import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from tstocknews.line import prepare, send
from tstocknews.report import render


def candidate(**updates):
    return {"symbol": "0001", "market": "twse", "name": "示例電子",
            "close": 125.5, "price_change": 2.5, "price_change_pct": 2.5 / 123,
            "volume_shares": 8_250_000, "institutional_buy_volume_ratio": .028,
            "revenue_yoy_3m": .186, "revenue_end_month": "2026-08",
            "fiscal_period": "2026-Q2", "eps": 3.25, "operating_margin": .124,
            "kd_k": 35.2, "kd_d": 30.1, "macd_dif": 1.235, "macd_dea": 1.08,
            **updates}


def report(rows, performance=(), include_performance=True):
    result = {"eligible_count": len(rows), "candidates": rows,
              "diagnostics": {"exclusion_counts": {"EPS_NOT_POSITIVE": 10}}}
    return render("2026-10-02", result, performance, "internal-version", include_performance)


class MobileReportTests(unittest.TestCase):
    def test_stock_information_contains_only_rank_and_name(self):
        text = report([candidate()])
        self.assertEqual(text.split("━━━━━━━━━━━━", 1)[0].splitlines(),
                         ["📊 台股每日篩選｜2026-10-02", "今日符合 1 檔，列出 1 檔", "", "① 示例電子", ""])
        for field in ("0001", "上市", "收盤價", "當日漲跌", "成交量", "法人", "營收", "EPS", "營業利益率", "DIF", "DEA"):
            self.assertNotIn(field, text)
        for internal in ("EPS_NOT_POSITIVE", "internal-version", "資料品質", "為什麼入選", "twse"):
            self.assertNotIn(internal, text)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 38)

    def test_name_only_rows_do_not_require_market_or_indicator_fields(self):
        text = report([{"name": "第一名"}, {"name": "第二名"}], include_performance=False)
        self.assertIn("① 第一名\n② 第二名\n", text)
        self.assertNotIn("歷次入選表現", text)

    def test_render_preserves_full_candidate_records_and_ranking(self):
        rows = [candidate(name="較高排名", symbol="9999"), candidate(name="較低排名", symbol="0001")]
        original = deepcopy(rows)
        text = report(rows)
        self.assertIn("① 較高排名\n② 較低排名\n", text)
        self.assertEqual(rows, original)

    def test_performance_excludes_pending_and_missing_outcomes(self):
        rows = [{"horizon": 10, "status": "MATURE", "price_return": .1},
                {"horizon": 10, "status": "MATURE", "price_return": -.02},
                {"horizon": 10, "status": "NOT_MATURE", "price_return": None},
                {"horizon": 10, "status": "MISSING_PRICE", "price_return": None},
                {"horizon": 10, "status": "MATURE", "price_return": None}]
        text = report([], rows)
        for expected in ("平均：+4.00%", "中位數：+4.00%", "上漲比例：50.0%",
                         "有效紀錄：2／5 筆", "尚待追蹤：1 筆", "價格資料不足：2 筆",
                         "漲幅達 5%：1／2 筆"):
            self.assertIn(expected, text)
        self.assertNotIn("NOT_MATURE", text)
        self.assertIn("漲幅達 5%：尚無結果", report([]))

    def test_no_candidates_and_isolated_test_report(self):
        text = report([], include_performance=False)
        self.assertIn("今天沒有符合條件的股票", text)
        self.assertIn("不計算任何測試績效", text)
        self.assertNotIn("歷次入選表現", text)

    @patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": "test", "LINE_GROUP_ID": "testgroup"})
    def test_twenty_stock_report_and_all_horizons_fit_one_line_request(self):
        rows = [candidate(symbol=f"{i:04d}", name="示例科技") for i in range(20)]
        history = [{"horizon": h, "status": "MATURE", "price_return": .02} for h in (1, 3, 5, 10, 20)]
        text = report(rows, history)
        calls = []
        def request(path, token, payload=None, retry_key=None):
            if path == "message/quota":
                return {"type": "limited", "value": 200}
            if path == "message/quota/consumption":
                return {"totalUsage": 0}
            if path.endswith("members/count"):
                return {"count": 2}
            calls.append(payload)
            return {}
        self.assertEqual(send(text, prepare("hash"), request)["status"], "SENT")
        self.assertEqual(len(calls), 1)
        messages = calls[0]["messages"]
        self.assertLessEqual(len(messages), 5)
        self.assertTrue(all(m["type"] == "text" and len(m["text"]) <= 4500 for m in messages))
        self.assertEqual("".join(m["text"] for m in messages), text)
        self.assertEqual(len(messages), 1)
        self.assertIn("今日符合 20 檔，列出 20 檔", text)
        self.assertIn("⑩ 示例科技", text)
        self.assertIn("⑪ 示例科技\n", text)
        self.assertIn("⑳ 示例科技\n", text)
