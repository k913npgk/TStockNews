"""Isolation tests for the quality/momentum daily shadow and Nov 9 resend."""
from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tstocknews import quality_factors as factors
from tstocknews import quality_shadow as shadow
from tstocknews.storage import digest, read, write


DAY = "2026-11-10"
RESEND_DAY = "2026-11-09"


def member(symbol, rank=1):
    return dict(symbol=symbol, market="twse", name=f"公司{symbol}", rank=rank,
                close=100.0 + rank, price_change_pct=0.01)


def resend_snapshot():
    return dict(
        date=RESEND_DAY,
        version=shadow.VERSION,
        retrospective=True,
        test_only=True,
        excluded_from_performance=True,
        members=[member(f"{1000 + i}", i + 1) for i in range(20)],
        formal=[member("2330")],
        pool_size=80,
        uses_12m_count=60,
        fiscal_periods=["2026Q2"],
        revenue_months=["2026-09"],
        continuity="INITIAL",
    )


class QualityShadowTests(unittest.TestCase):
    def test_market_features_requires_148_sessions_and_marks_12m_coverage(self):
        start = date(2026, 1, 1)
        company = dict(symbol="2330", market="twse", name="台積電")

        def snapshots(count):
            out = []
            for i in range(count):
                day = (start + timedelta(days=i)).isoformat()
                close = 100.0 + i
                out.append(dict(date=day, prices=[dict(
                    symbol="2330", market="twse", date=day,
                    open=close, high=close + 1, low=close - 1, close=close,
                    volume_shares=1_000_000, price_change_pct=0.01,
                )]))
            return out

        with self.assertRaisesRegex(ValueError, "148"):
            shadow.market_features(snapshots(147), [company])

        six_month = shadow.market_features(snapshots(148), [company])
        self.assertEqual(len(six_month), 1)
        self.assertTrue(factors.finite(six_month[0]["m6"]))
        self.assertFalse(six_month[0]["uses_12m"])

        twelve_month = shadow.market_features(snapshots(274), [company])
        self.assertEqual(len(twelve_month), 1)
        self.assertTrue(factors.finite(twelve_month[0]["m12"]))
        self.assertTrue(twelve_month[0]["uses_12m"])

    def test_rank_members_uses_only_feature_complete_pool_and_quality_momentum_rank(self):
        count = 24
        rows = [dict(symbol=str(1000 + i), market="twse", name=f"公司{i}",
                     m6=float(i), m12=float(count - i), uses_12m=True,
                     close=50.0 + i, price_change_pct=0.01)
                for i in range(count)]
        funds, revenues, facts = [], {}, {}
        months = ("2026-09", "2026-08", "2026-07", "2026-06")
        for i, row in enumerate(rows):
            symbol = row["symbol"]
            revenue_months = []
            for j, month in enumerate(months):
                base = 100.0 + i
                growth = 0.05 + i * 0.01 + j * 0.02
                record = dict(month=month, current=base * (1 + growth), prior_year=base)
                revenue_months.append(record)
                revenues[(symbol, month)] = (record["current"], record["prior_year"])
            funds.append(dict(symbol=symbol, market="twse", revenue_months=revenue_months))
            facts[(symbol, "2026Q2")] = dict(
                symbol=symbol, period="2026Q2", scope="cr", eps=12.0 + i,
                eps_prior=10.0, profit=300_000_000.0 + i * 1_000_000,
                profit_prior=250_000_000.0, equity=1_000_000_000.0 + i * 1_000_000,
            )
            facts[(symbol, "2025Q4")] = dict(
                symbol=symbol, period="2025Q4", scope="cr", eps=30.0 + i,
                eps_prior=27.0, profit=800_000_000.0 + i * 2_000_000,
                profit_prior=700_000_000.0, equity=900_000_000.0 + i * 1_000_000,
            )

        # Missing TTM accounting and a missing seasonal month must both exclude
        # the issuer instead of assigning a zero feature or falling back stale.
        missing_accounting = rows[-1]["symbol"]
        facts.pop((missing_accounting, "2026Q2"))
        missing_revenue = rows[-2]["symbol"]
        funds[-2]["revenue_months"] = [x for x in funds[-2]["revenue_months"]
                                      if x["month"] != "2026-06"]
        revenues.pop((missing_revenue, "2026-06"))

        members, coverage = shadow.rank_members(rows, dict(fundamentals=funds), DAY, facts)

        self.assertEqual(coverage["price_feature_pool"], count)
        self.assertEqual(coverage["pool_size"], count - 2)
        self.assertEqual(coverage["unknown_accounting"], 1)
        self.assertEqual(coverage["unknown_monthly_model"], 1)
        self.assertEqual(len(members), 20)
        self.assertNotIn(missing_accounting, {r["symbol"] for r in members})
        self.assertNotIn(missing_revenue, {r["symbol"] for r in members})

        expected_pool = []
        for row in rows:
            accounting = factors.accounting(row["symbol"], DAY, facts)
            surprise = factors.revenue_surprise(row["symbol"], DAY, revenues)
            if not accounting or surprise is None:
                continue
            expected_pool.append(dict(
                row, roe=accounting["roe_ttm"],
                ep=accounting["eps_ttm"] / row["close"],
                surprise=surprise["surprise"], revenue_month=surprise["month"],
                fiscal_period=accounting["period"],
            ))
        expected = factors.choose(expected_pool)["quality_momentum"]
        self.assertEqual([r["symbol"] for r in members],
                         [r["symbol"] for r in expected])

    def test_capture_resets_roster_when_previous_snapshot_has_another_strategy_version(self):
        target = date.fromisoformat(DAY)
        start = target - timedelta(days=147)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, root = base / "source", base / "quality-momentum-v1"
            old_day = (target - timedelta(days=1)).isoformat()
            old = dict(date=old_day, version="test-growth-flow-old-v1", active=True,
                       members=[member("9999")], formal=[member("2330")])
            write(root / "snapshots" / f"{old_day}.json", old)
            write(source / "recommendations" / f"{DAY}.json",
                  dict(strategy_version=shadow.STRATEGY_VERSION, as_of=DAY,
                       candidates=[member("2330")]))
            write(source / "financials" / f"{DAY}.json.gz",
                  dict(observed_date=DAY, universe=[member("2330")], fundamentals=[]))

            for i in range(148):
                current = start + timedelta(days=i)
                day = current.isoformat()
                prices = [dict(symbol=symbol, market=market, date=day,
                               open=100.0, high=101.0, low=99.0, close=100.0,
                               volume_shares=1_000_000)
                          for symbol, market in (("2330", "twse"), ("6488", "tpex"))]
                write(source / "days" / f"{day}.json.gz",
                      dict(date=day, prices=prices))

            previous_date = (target - timedelta(days=2)).isoformat()
            # Ensure the previous roster exists on the session immediately
            # before the snapshot window's final session, as continuity expects.
            previous = dict(old, date=previous_date)
            write(root / "snapshots" / f"{previous_date}.json", previous)
            eligible = [dict(symbol="2330", market="twse", name="台積電",
                             m6=0.2, m12=0.3, uses_12m=True, close=100.0,
                             price_change_pct=0.01)]
            selected = [dict(eligible[0], roe=0.1, ep=0.05, surprise=0.02,
                             revenue_month="2026-09", fiscal_period="2026Q2",
                             score=0.9, weight=1.0)]
            loader = lambda period, _root, day: dict(
                period=period, observed_date=day, rows=[], source={})

            with patch.object(shadow, "today", return_value=target), \
                 patch.object(shadow, "market_features", return_value=eligible), \
                 patch.object(shadow, "rank_members", return_value=(selected, {"pool_size": 1,
                     "uses_12m_count": 1, "price_feature_pool": 1})):
                snapshot = shadow.capture(source, root, DAY, load_period=loader)

            self.assertEqual(snapshot["continuity"], "INITIAL")
            self.assertEqual(snapshot["outcomes"], {})
            report = (root / "reports" / f"{DAY}.md").read_text(encoding="utf-8")
            self.assertIn("入榜 台積電", report)
            self.assertNotIn("留榜", report)

    def test_resend_is_namespaced_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_text = "原本已送達的測試報告\n"
            original_report = root / "reports" / f"{RESEND_DAY}.md"
            original_report.parent.mkdir(parents=True)
            original_report.write_text(original_text, encoding="utf-8")
            live_snapshot = dict(date=RESEND_DAY, version=shadow.VERSION,
                                 members=[member("2330")])
            live_snapshot_path = root / "snapshots" / f"{RESEND_DAY}.json"
            write(live_snapshot_path, live_snapshot)
            formal_path = root / "recommendations" / f"{RESEND_DAY}.json"
            formal = dict(date=RESEND_DAY, strategy_version=shadow.STRATEGY_VERSION,
                          candidates=[member("2330")])
            write(formal_path, formal)
            live_delivery_path = root / "data" / "delivery" / f"{RESEND_DAY}.json"
            live_delivery = dict(status="SENT", payload_hash=digest(original_text),
                                 retry_key="original-live-key")
            write(live_delivery_path, live_delivery)
            original_bytes = {p: p.read_bytes() for p in
                              (original_report, live_snapshot_path, formal_path, live_delivery_path)}

            resend_root = root / "resends" / RESEND_DAY
            write(resend_root / "snapshot.json", resend_snapshot())
            prepared = shadow.prepare_resend(root, RESEND_DAY)
            report_path = resend_root / "reports" / f"{RESEND_DAY}.md"
            delivery_path = resend_root / "data" / "delivery" / f"{RESEND_DAY}.json"
            content = report_path.read_text(encoding="utf-8")
            delivery = read(delivery_path)
            self.assertTrue(prepared["retrospective"])
            self.assertIn("測試策略更正版", content)
            self.assertIn("事後補發", content)
            self.assertNotIn("+80.40%", content)
            self.assertEqual(delivery["status"], "PENDING")
            self.assertEqual(delivery["payload_hash"], digest(content))
            self.assertTrue(delivery["retry_key"])
            self.assertNotEqual(delivery["retry_key"], live_delivery["retry_key"])

            report_bytes, delivery_bytes = report_path.read_bytes(), delivery_path.read_bytes()
            shadow.prepare_resend(root, RESEND_DAY)
            self.assertEqual(report_path.read_bytes(), report_bytes)
            self.assertEqual(delivery_path.read_bytes(), delivery_bytes)
            for path, before in original_bytes.items():
                self.assertEqual(path.read_bytes(), before)

            delivery["status"] = "SENT"
            write(delivery_path, delivery)
            result = shadow.push(resend_root / "data", resend_root / "reports", RESEND_DAY)
            self.assertEqual(result["status"], "SENT")

            report_path.write_text(content + "變更文字\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Report changed after preparing"):
                shadow.push(resend_root / "data", resend_root / "reports", RESEND_DAY)

    def test_resend_rejects_wrong_strategy_or_nonretrospective_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = resend_snapshot()
            invalid["version"] = "test-other-strategy-v1"
            write(root / "resends" / RESEND_DAY / "snapshot.json", invalid)
            with self.assertRaisesRegex(ValueError, "Approved quality correction"):
                shadow.prepare_resend(root, RESEND_DAY)

            invalid = resend_snapshot()
            invalid["retrospective"] = False
            write(root / "resends" / RESEND_DAY / "snapshot.json", invalid)
            with self.assertRaisesRegex(ValueError, "retrospective evidence"):
                shadow.prepare_resend(root, RESEND_DAY)

    def test_next_close_rises_compares_today_close_to_saved_close(self):
        rows = [member("2330"), member("6488", 2), member("1101", 3)]
        quotes = {
            ("twse", "2330"): dict(open=100, high=106, low=99, close=105,
                                     volume_shares=100),
            ("twse", "6488"): dict(open=100, high=102, low=97, close=98,
                                     volume_shares=100),
            # Invalid OHLC is unknown, not a down observation.
            ("twse", "1101"): dict(open=100, high=100, low=101, close=99,
                                     volume_shares=100),
        }
        self.assertEqual(shadow.next_close_rises(rows, quotes),
                         dict(up=1, known=2, total=3))


if __name__ == "__main__":
    unittest.main()
