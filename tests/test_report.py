import os
import unittest
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
    def test_financial_and_technical_values_are_preserved_in_short_lines(self):
        text = report([candidate()])
        for value in ("示例電子（0001）｜上市", "125.50 元", "▲ +2.50 元（+2.03%）",
                      "8,250.0 張", "+2.80%", "+18.60%", "2026/08", "2026 年上半年",
                      "EPS：3.25 元", "+12.40%", "K 35.20／D 30.10", "DIF 1.235／DEA 1.080"):
            self.assertIn(value, text)
        for internal in ("EPS_NOT_POSITIVE", "internal-version", "資料品質", "為什麼入選", "twse"):
            self.assertNotIn(internal, text)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 38)

    def test_falling_flat_and_missing_changes_are_distinct(self):
        for amount, rate, expected in ((-1.2, -.0173, "▼ -1.20 元（-1.73%）"),
                                      (0, 0, "0.00 元（0.00%）"),
                                      (None, None, "資料未提供"),
                                      (1.2, None, "▲ +1.20 元（幅度未提供）")):
            with self.subTest(amount=amount):
                text = report([candidate(price_change=amount, price_change_pct=rate)])
                self.assertIn("當日漲跌：" + expected, text)
        old = candidate()
        old.pop("price_change")
        old.pop("price_change_pct")
        self.assertIn("當日漲跌：資料未提供", report([old]))

    def test_unknown_financial_observations_are_not_shown_as_zero(self):
        text = report([candidate(operating_margin=None, revenue_end_month=None, fiscal_period="")])
        for field in ("營業利益率", "營收資料截至", "財報期間"):
            self.assertIn(field + "：資料未提供", text)

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
        self.assertIn("今日符合 20 檔，列出 20 檔", text)
        self.assertIn("⑩ 示例科技", text)
        self.assertIn("⑪ 示例科技（0010）", text)
        self.assertIn("⑳ 示例科技（0019）", text)
