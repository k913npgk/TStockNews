"""Unit and integration tests for the v9 four-indicator Scheme B (Top 10) strategy."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tstocknews import STRATEGY_VERSION
from tstocknews.analysis import recommendations
from tstocknews.cli import daily
from tstocknews.four_indicator import (
    MAX_CANDIDATES,
    MIN_MEDIAN_TURNOVER_20D,
    SCORE_WEIGHTS,
    _percentile_ranks,
    screen_four_indicators,
)
from tstocknews.preview import preview
from tstocknews.storage import read, write


def make_sessions(n: int = 125, start: str = "2026-01-02") -> list[str]:
    out = []
    cur = date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def make_uptrend_bars(
    sessions: list[str],
    symbol: str,
    market: str = "twse",
    *,
    base_close: float = 50.0,
    step: float = 0.25,
    accel: float = 0.004,
    volume: int = 3_000_000,
    last_volume: int | None = None,
) -> list[dict]:
    rows = []
    prev_close = base_close
    for i, ds in enumerate(sessions):
        close = round(base_close + i * step + accel * i * i, 2)
        vol = last_volume if (last_volume is not None and i == len(sessions) - 1) else volume
        chg = round(close - prev_close, 2) if i > 0 else 0.0
        rows.append({
            "date": ds,
            "market": market,
            "symbol": symbol,
            "open": round(close - 0.2, 2),
            "high": round(close + 0.5, 2),
            "low": round(close - 0.5, 2),
            "close": close,
            "price_change": chg,
            "price_change_pct": (chg / prev_close) if i > 0 and prev_close > 0 else 0.0,
            "volume_shares": vol,
        })
        prev_close = close
    return rows


def make_inst(
    sessions: list[str],
    symbol: str,
    market: str = "twse",
    *,
    daily_net: int = 100_000,
    last_net: int | None = None,
    skip_dates: set[str] | None = None,
) -> list[dict]:
    skip = skip_dates or set()
    rows = []
    for i, ds in enumerate(sessions):
        if ds in skip:
            continue
        net = last_net if (last_net is not None and i == len(sessions) - 1) else daily_net
        rows.append({"date": ds, "market": market, "symbol": symbol, "net_buy_shares": net})
    return rows


class FourIndicatorTests(unittest.TestCase):
    def test_percentile_ranks_handles_single_and_ties(self):
        self.assertEqual(_percentile_ranks([]), [])
        self.assertEqual(_percentile_ranks([42.0]), [0.5])
        self.assertEqual(_percentile_ranks([10.0, 20.0, 20.0, 30.0]), [0.0, 0.5, 0.5, 1.0])

    def test_g5_gates_no_g0_3day_inst_or_fundamental_required(self):
        sessions = make_sessions(125)
        day = sessions[-1]
        universe = [
            {"symbol": "2330", "market": "twse", "name": "台積電"},
            {"symbol": "2317", "market": "twse", "name": "鴻海"},
            {"symbol": "2454", "market": "twse", "name": "聯發科"},
            {"symbol": "3008", "market": "twse", "name": "大立光"},
            {"symbol": "8069", "market": "tpex", "name": "元太"},
        ]
        prices = (
            # 2330: passes all G5 gates even with negative prior 2 days institutional flow (today > 0)
            make_uptrend_bars(sessions, "2330", base_close=100.0, step=0.4, volume=3_000_000)
            # 2317: fails volume < 2000 lots on target day
            + make_uptrend_bars(sessions, "2317", base_close=100.0, step=0.4, volume=3_000_000, last_volume=1_500_000)
            # 2454: fails 20d median turnover < 30M (price ~5 * 2M = 10M)
            + make_uptrend_bars(sessions, "2454", base_close=5.0, step=0.02, accel=0.0002, volume=2_000_000)
            # 3008: fails institutional today <= 0
            + make_uptrend_bars(sessions, "3008", base_close=100.0, step=0.4, volume=3_000_000)
            # 8069: fails missing institutional day in last 20 sessions
            + make_uptrend_bars(sessions, "8069", market="tpex", base_close=100.0, step=0.4, volume=3_000_000)
        )
        # 2330 has negative flow on t-2 and t-1, positive today -> G5 passes (no G0 3-day streak gate)
        inst_2330 = make_inst(sessions, "2330", daily_net=200_000)
        inst_2330[-2]["net_buy_shares"] = -50_000
        inst_2330[-3]["net_buy_shares"] = -50_000
        inst = (
            inst_2330
            + make_inst(sessions, "2317", daily_net=200_000)
            + make_inst(sessions, "2454", daily_net=200_000)
            + make_inst(sessions, "3008", daily_net=200_000, last_net=0)
            + make_inst(sessions, "8069", market="tpex", daily_net=200_000, skip_dates={sessions[-5]})
        )

        res = screen_four_indicators(day, sessions, universe, prices, inst)
        self.assertEqual([c["symbol"] for c in res["candidates"]], ["2330"])
        self.assertIn("VOLUME_BELOW_2000_LOTS", res["diagnostics"]["symbols"]["twse:2317"]["reasons"])
        self.assertIn("TURNOVER_20D_BELOW_30M", res["diagnostics"]["symbols"]["twse:2454"]["reasons"])
        self.assertIn("INSTITUTIONAL_NOT_BUYING_TODAY", res["diagnostics"]["symbols"]["twse:3008"]["reasons"])
        self.assertIn("MISSING_INSTITUTIONAL_DATA", res["diagnostics"]["symbols"]["tpex:8069"]["reasons"])
        self.assertGreaterEqual(res["candidates"][0]["median_turnover_20d"], MIN_MEDIAN_TURNOVER_20D)

    def test_scheme_b_composite_ranking_and_top10_cap(self):
        sessions = make_sessions(125)
        day = sessions[-1]
        universe = [{"symbol": f"{2000 + i}", "market": "twse", "name": f"股{i}"} for i in range(15)]
        prices, inst = [], []
        for i, co in enumerate(universe):
            # Vary inst_ratio_20d (40% weight) strongly so higher index ranks higher
            prices.extend(make_uptrend_bars(sessions, co["symbol"], base_close=50.0 + i * 2.0, step=0.2 + i * 0.02, volume=2_500_000 + i * 100_000))
            inst.extend(make_inst(sessions, co["symbol"], daily_net=50_000 + i * 50_000))

        res = screen_four_indicators(day, sessions, universe, prices, inst)
        self.assertEqual(res["eligible_count"], 15)
        self.assertEqual(len(res["candidates"]), MAX_CANDIDATES)
        self.assertEqual(MAX_CANDIDATES, 10)
        # Top candidate should be 2014 (highest inst_ratio_20d, kd_k, vol_turnover)
        top = res["candidates"][0]
        self.assertEqual(top["symbol"], "2014")
        expected_score = (
            0.40 * top["pct_inst_ratio_20d"]
            + 0.20 * top["pct_kd_k"]
            + 0.20 * top["pct_macd_dif_pct"]
            + 0.20 * top["pct_vol_turnover"]
        )
        self.assertAlmostEqual(top["composite_score"], expected_score, places=9)
        self.assertEqual(SCORE_WEIGHTS["inst_ratio_20d"], 0.40)
        # Verify strictly non-increasing composite scores
        scores = [c["composite_score"] for c in res["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_daily_and_preview_use_v9_without_financials_and_keep_history_immutable(self):
        sessions = make_sessions(125)
        day = sessions[-1]
        universe = [{"symbol": "2330", "market": "twse", "name": "台積電"},
                    {"symbol": "8069", "market": "tpex", "name": "元太"}]
        prices = (make_uptrend_bars(sessions, "2330", base_close=100.0, step=0.4, volume=3_000_000)
                  + make_uptrend_bars(sessions, "8069", market="tpex", base_close=100.0, step=0.3, volume=3_000_000))
        inst = (make_inst(sessions, "2330", daily_net=300_000)
                + make_inst(sessions, "8069", market="tpex", daily_net=150_000))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            reports = Path(tmp) / "reports"
            for ds in sessions:
                write(root / "days" / (ds + ".json.gz"), {
                    "date": ds,
                    "prices": [r for r in prices if r["date"] == ds],
                    "institutional": [r for r in inst if r["date"] == ds],
                })
            with (
                patch("tstocknews.cli.today", return_value=date.fromisoformat(day)),
                patch("tstocknews.cli.holiday_days", return_value=set()),
                patch("tstocknews.cli.sync", return_value=[]),
                patch("tstocknews.cli.universe_snapshot", return_value={"observed_date": day, "universe": universe}),
            ):
                status = daily(root, reports, day)

            self.assertEqual(status["status"], "REPORT_READY")
            self.assertEqual(status["strategy_version"], STRATEGY_VERSION)
            self.assertEqual(status["selected"], 2)
            saved = read(root / "recommendations" / (day + ".json"))
            self.assertEqual(saved["strategy_version"], STRATEGY_VERSION)
            self.assertEqual([c["symbol"] for c in saved["candidates"]], ["2330", "8069"])
            self.assertTrue((root / "universe" / (day + ".json.gz")).exists())

            # Preview parity on the exact same inputs
            runs = Path(tmp) / "test-runs"
            with (
                patch("tstocknews.preview.today", return_value=date.fromisoformat(day)),
                patch("tstocknews.preview.holiday_days", return_value=set()),
                patch("tstocknews.preview.sync", return_value=[]),
            ):
                manifest = preview(root, runs, "v9-parity", day)
            self.assertEqual(manifest["analysis_status"], "COMPLETE")
            self.assertEqual(manifest["selected"], 2)
            test_rec = read(runs / "v9-parity" / "data" / "recommendations" / (day + ".json"))
            self.assertEqual(
                [c["symbol"] for c in test_rec["candidates"]],
                [c["symbol"] for c in saved["candidates"]],
            )
            self.assertTrue(test_rec["test_only"])


if __name__ == "__main__":
    unittest.main()
