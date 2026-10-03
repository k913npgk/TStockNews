import unittest
from datetime import date, timedelta

from tstocknews.engine import screen
from tstocknews.official import quote_rows


DAY = "2026-10-02"


def quote(market, fields, values):
    return {"date": DAY.replace("-", ""), "fields": fields, "data": [values]}


class OfficialPriceChangeTests(unittest.TestCase):
    def test_twse_positive_negative_and_flat_use_official_sign_marker(self):
        fields = ["證券代號", "證券名稱", "成交股數", "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差"]
        cases = [
            ("<p style= color:red>+</p>", "2.00", 102.0, 2.0),
            ("<p style= color:green>-</p>", "1.25", 98.75, -1.25),
            ("<p> </p>", "0.00", 100.0, 0.0),
        ]
        for sign, amount, close, expected_change in cases:
            with self.subTest(sign=sign, amount=amount):
                row = quote_rows(quote("twse", fields, ["2330", "台積電", "1,000", "99", "103", "98", str(close), sign, amount]), "twse", DAY)[0]
                self.assertEqual(row["price_change"], expected_change)
                self.assertAlmostEqual(row["price_change_pct"], expected_change / (close - expected_change))

    def test_tpex_signed_change_positive_negative_and_flat(self):
        fields = ["代號", "名稱", "成交股數", "開盤", "最高", "最低", "收盤", "漲跌"]
        for change, expected in [("+1.27", 1.27), ("-0.04", -0.04), ("0.00", 0.0), ("＋1.27", 1.27), ("－0.04", -0.04)]:
            with self.subTest(change=change):
                close = 101.27 if expected > 0 else 99.96 if expected < 0 else 100.0
                row = quote_rows(quote("tpex", fields, ["6488", "環球晶", "2,000", "100", str(close), "99", str(close), change]), "tpex", DAY)[0]
                self.assertEqual(row["price_change"], expected)
                self.assertAlmostEqual(row["price_change_pct"], expected / (close - expected))

    def test_no_comparison_and_missing_or_unsigned_nonzero_change_stay_unknown(self):
        twse_fields = ["證券代號", "證券名稱", "成交股數", "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差"]
        special = quote_rows(quote("twse", twse_fields, ["2330", "台積電", "1,000", "99", "101", "98", "100", "X", "0.00"]), "twse", DAY)[0]
        self.assertIsNone(special["price_change"])
        self.assertIsNone(special["price_change_pct"])

        tpex_fields = ["代號", "名稱", "成交股數", "開盤", "最高", "最低", "收盤", "漲跌"]
        unsigned = quote_rows(quote("tpex", tpex_fields, ["6488", "環球晶", "2,000", "99", "101", "98", "100", "1.25"]), "tpex", DAY)[0]
        self.assertIsNone(unsigned["price_change"])
        self.assertIsNone(unsigned["price_change_pct"])

        absent = quote_rows(quote("tpex", tpex_fields[:-1], ["6488", "環球晶", "2,000", "99", "101", "98", "100"]), "tpex", DAY)[0]
        self.assertIsNone(absent["price_change"])
        self.assertIsNone(absent["price_change_pct"])

    def test_screen_passes_optional_change_fields_through_without_affecting_eligibility(self):
        sessions = []
        current = date(2026, 4, 1)
        while len(sessions) < 125:
            if current.weekday() < 5:
                sessions.append(current.isoformat())
            current += timedelta(days=1)
        prices = []
        closes = [100.0] * len(sessions)
        closes[-4:] = [50.0, 50.0, 80.0, 130.0]
        for index, (day, close) in enumerate(zip(sessions, closes)):
            row = {"date": day, "symbol": "2330", "market": "twse", "name": "Test",
                   "open": close - .5, "high": max(close, 100) + 1, "low": min(close, 100) - 1,
                   "close": close, "volume_shares": 2_500_000}
            if index == len(sessions) - 1:
                row.update({"price_change": 2.0, "price_change_pct": 2 / 128})
            prices.append(row)
        fundamentals = [{"symbol": "2330", "market": "twse", "eps": 1,
                         "revenue_yoy_3m": .2, "available_date": sessions[-1], "fiscal_period": "2025Q1"}]
        institutional = [{"date": day, "symbol": "2330", "market": "twse", "net_buy_shares": 25_000}
                         for day in sessions[-3:]]
        result = screen(sessions[-1], sessions, [{"symbol": "2330", "market": "twse", "name": "Test"}],
                        prices, fundamentals, institutional)
        self.assertEqual(result["eligible_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["price_change"], 2.0)
        self.assertAlmostEqual(candidate["price_change_pct"], 2 / 128)

        prices[-1].pop("price_change")
        prices[-1].pop("price_change_pct")
        unknown = screen(sessions[-1], sessions, [{"symbol": "2330", "market": "twse", "name": "Test"}],
                         prices, fundamentals, institutional)["candidates"][0]
        self.assertIsNone(unknown["price_change"])
        self.assertIsNone(unknown["price_change_pct"])


if __name__ == "__main__":
    unittest.main()
