"""Four-indicator production screener: KD, MACD, Institutional Flow, and Volume (Scheme B, Top 10)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
from statistics import median
from typing import Any

from .engine import (
    KD_PRE_CROSS_MAX_GAP,
    MIN_HISTORY_SESSIONS,
    MIN_VOLUME_SHARES,
    _float,
    _indicators,
    _kd_window,
    _row_key,
    _valid_price,
)

MAX_CANDIDATES = 10
INST_WINDOW_SESSIONS = 20
TURNOVER_WINDOW_SESSIONS = 20
MIN_MEDIAN_TURNOVER_20D = 30_000_000.0
SCORE_WEIGHTS = {
    "inst_ratio_20d": 0.40,
    "kd_k": 0.20,
    "macd_dif_pct": 0.20,
    "vol_turnover": 0.20,
}


def _percentile_ranks(values: list[float]) -> list[float]:
    """Cross-sectional percentile ranks in [0.0, 1.0] with average rank for ties."""
    m = len(values)
    if m == 0:
        return []
    if m == 1:
        return [0.5]
    indexed = sorted(range(m), key=lambda i: values[i])
    ranks = [0.0] * m
    i = 0
    while i < m:
        j = i + 1
        while j < m and values[indexed[j]] == values[indexed[i]]:
            j += 1
        avg_rank = 0.5 * (i + j - 1) / (m - 1)
        for k in range(i, j):
            ranks[indexed[k]] = avg_rank
        i = j
    return ranks


def screen_four_indicators(
    as_of: str,
    sessions: list[str],
    universe: list[dict[str, Any]],
    prices: list[dict[str, Any]],
    institutional: list[dict[str, Any]],
    *,
    candidate_limit: int | None = MAX_CANDIDATES,
) -> dict[str, Any]:
    """Apply G5 wide bullish 4-indicator gates and Scheme B composite ranking (Top 10)."""
    date.fromisoformat(as_of)
    ordered_sessions = sorted({s for s in sessions if s <= as_of})
    session_set = set(ordered_sessions)
    diagnostics: dict[str, Any] = {
        "as_of": as_of,
        "calendar_sessions_available": len(ordered_sessions),
        "minimum_history_sessions": MIN_HISTORY_SESSIONS,
        "gates": {
            "kd": f"K > D or PRE_CROSS (0 <= D - K <= {KD_PRE_CROSS_MAX_GAP} and narrowing)",
            "macd": "DIF > DEA",
            "institutional": "today net_buy_shares > 0 and complete 20-session institutional data",
            "volume": f"volume_shares >= {MIN_VOLUME_SHARES} and 20-session median turnover >= {int(MIN_MEDIAN_TURNOVER_20D)}",
        },
        "ranking": {
            "method": "cross_sectional_percentile_composite",
            "weights": dict(SCORE_WEIGHTS),
            "candidate_limit": candidate_limit,
        },
        "universe_count": len(universe),
        "eligible_count": 0,
        "exclusion_counts": defaultdict(int),
        "symbols": {},
        "data_warnings": [],
    }
    if len(ordered_sessions) < MIN_HISTORY_SESSIONS:
        diagnostics["data_warnings"].append("calendar has fewer than 120 sessions through as_of")

    price_map: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in prices:
        ds = row.get("date")
        if ds in session_set and ds not in price_map[_row_key(row)]:
            price_map[_row_key(row)][ds] = row

    inst_map: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in institutional:
        ds = row.get("date")
        if ds in session_set:
            inst_map[_row_key(row)][ds] += int(row.get("net_buy_shares") or 0)

    recent_20 = ordered_sessions[-INST_WINDOW_SESSIONS:]
    recent_3 = ordered_sessions[-3:]
    candidates: list[dict[str, Any]] = []

    for company in universe:
        key = _row_key(company)
        symbol, market = key
        label = f"{market}:{symbol}"
        reasons: list[str] = []
        diag: dict[str, Any] = {"symbol": symbol, "market": market, "reasons": reasons}
        diagnostics["symbols"][label] = diag

        hist = price_map.get(key, {})
        streak: list[dict[str, Any]] = []
        for ds in reversed(ordered_sessions):
            row = hist.get(ds)
            if not _valid_price(row):
                break
            streak.append(row)
        streak.reverse()
        diag["valid_consecutive_sessions"] = len(streak)
        if len(streak) < MIN_HISTORY_SESSIONS:
            reasons.append("INSUFFICIENT_OR_GAPPED_PRICE_HISTORY")
            diagnostics["exclusion_counts"]["INSUFFICIENT_OR_GAPPED_PRICE_HISTORY"] += 1
            continue
        if any(row.get("corporate_action_unresolved") for row in streak[-MIN_HISTORY_SESSIONS:]):
            reasons.append("CORPORATE_ACTION_UNRESOLVED")
            diagnostics["exclusion_counts"]["CORPORATE_ACTION_UNRESOLVED"] += 1
            continue

        streak = streak[-MIN_HISTORY_SESSIONS:]
        latest_row = streak[-1]
        latest_date = latest_row["date"]
        diag["price_date"] = latest_date
        if latest_date != as_of:
            reasons.append("MISSING_AS_OF_PRICE")

        ind = _indicators(streak)
        last_i = len(streak) - 1
        latest_close = float(latest_row["close"])
        volume = int(latest_row.get("volume_shares") or 0)
        vol_turnover = latest_close * volume

        # 20-session median turnover proxy
        recent_20_rows = streak[-TURNOVER_WINDOW_SESSIONS:]
        turnovers_20 = [float(r["close"]) * int(r.get("volume_shares") or 0) for r in recent_20_rows]
        med_turnover_20d = float(median(turnovers_20)) if len(turnovers_20) == TURNOVER_WINDOW_SESSIONS else None

        # KD(9,3,3) G5 gate: K > D or PRE_CROSS
        kd = _kd_window(ind["k"], ind["d"])
        latest_k = ind["k"][last_i]
        latest_d = ind["d"][last_i]
        kd_bullish = (latest_k > latest_d) or (kd["kd_stage"] == "PRE_CROSS")

        # MACD(12,26,9) G5 gate: DIF > DEA
        latest_dif = ind["dif"][last_i]
        latest_dea = ind["dea"][last_i]
        macd_dif_pct = _float(latest_dif / latest_close) if latest_close > 0 else None
        macd_bullish = latest_dif > latest_dea

        # Institutional 20-session flow & today net buy > 0
        if len(recent_20) < INST_WINDOW_SESSIONS:
            reasons.append("INSUFFICIENT_INSTITUTIONAL_SESSIONS")
        if any(ds not in inst_map[key] for ds in recent_20):
            reasons.append("MISSING_INSTITUTIONAL_DATA")
        inst_today = inst_map[key].get(as_of) if as_of in inst_map[key] else None
        sum_net_20d = sum(inst_map[key].get(ds, 0) for ds in recent_20) if len(recent_20) == INST_WINDOW_SESSIONS else None
        sum_vol_20d = sum(int(r.get("volume_shares") or 0) for r in recent_20_rows)
        inst_ratio_20d = (_float(sum_net_20d / sum_vol_20d)
                          if sum_net_20d is not None and sum_vol_20d > 0 else None)

        net3 = [inst_map[key].get(ds, 0) for ds in recent_3] if len(recent_3) == 3 else []
        sum_net3 = sum(net3) if len(net3) == 3 else None

        if not kd_bullish:
            reasons.append("KD_NOT_BULLISH")
        if not macd_bullish or macd_dif_pct is None:
            reasons.append("MACD_NOT_BULLISH")
        if inst_today is None or inst_today <= 0:
            if "MISSING_INSTITUTIONAL_DATA" not in reasons:
                reasons.append("INSTITUTIONAL_NOT_BUYING_TODAY")
        if inst_ratio_20d is None and "MISSING_INSTITUTIONAL_DATA" not in reasons:
            reasons.append("INSTITUTIONAL_RATIO_UNKNOWN")
        if volume < MIN_VOLUME_SHARES:
            reasons.append("VOLUME_BELOW_2000_LOTS")
        if med_turnover_20d is None or med_turnover_20d < MIN_MEDIAN_TURNOVER_20D:
            reasons.append("TURNOVER_20D_BELOW_30M")

        diag.update({
            "kd_k": latest_k,
            "kd_d": latest_d,
            **kd,
            "kd_bullish_gate": kd_bullish,
            "macd_dif": latest_dif,
            "macd_dea": latest_dea,
            "macd_dif_pct": macd_dif_pct,
            "institutional_net_buy_today": inst_today,
            "institutional_net_buy_shares_3d_sum": sum_net3,
            "institutional_net_buy_shares_20d_sum": sum_net_20d,
            "volume_shares": volume,
            "volume_shares_20d_sum": sum_vol_20d,
            "vol_turnover": vol_turnover,
            "median_turnover_20d": med_turnover_20d,
            "inst_ratio_20d": inst_ratio_20d,
        })
        if reasons:
            for reason in set(reasons):
                diagnostics["exclusion_counts"][reason] += 1
            continue

        candidates.append({
            "symbol": symbol,
            "market": market,
            "name": company.get("name", ""),
            "as_of": as_of,
            "close": latest_close,
            "price_change": _float(latest_row.get("price_change")),
            "price_change_pct": _float(latest_row.get("price_change_pct")),
            "volume_shares": volume,
            "vol_turnover": vol_turnover,
            "median_turnover_20d": med_turnover_20d,
            "kd_k": latest_k,
            "kd_d": latest_d,
            **kd,
            "macd_dif": latest_dif,
            "macd_dea": latest_dea,
            "macd_dif_pct": macd_dif_pct,
            "institutional_net_buy_today": inst_today,
            "institutional_net_buy_shares_3d_sum": sum_net3,
            "institutional_net_buy_shares_20d_sum": sum_net_20d,
            "volume_shares_20d_sum": sum_vol_20d,
            "inst_ratio_20d": inst_ratio_20d,
            "reasons": [],
        })

    if candidates:
        p_inst = _percentile_ranks([c["inst_ratio_20d"] for c in candidates])
        p_kd = _percentile_ranks([c["kd_k"] for c in candidates])
        p_macd = _percentile_ranks([c["macd_dif_pct"] for c in candidates])
        p_vol = _percentile_ranks([c["vol_turnover"] for c in candidates])
        for i, c in enumerate(candidates):
            score = (
                SCORE_WEIGHTS["inst_ratio_20d"] * p_inst[i]
                + SCORE_WEIGHTS["kd_k"] * p_kd[i]
                + SCORE_WEIGHTS["macd_dif_pct"] * p_macd[i]
                + SCORE_WEIGHTS["vol_turnover"] * p_vol[i]
            )
            c["pct_inst_ratio_20d"] = p_inst[i]
            c["pct_kd_k"] = p_kd[i]
            c["pct_macd_dif_pct"] = p_macd[i]
            c["pct_vol_turnover"] = p_vol[i]
            c["composite_score"] = score
            if not math.isfinite(score):
                raise ValueError(f"Non-finite composite score for {c['market']}:{c['symbol']}")

    candidates.sort(
        key=lambda c: (
            -c["composite_score"],
            -c["inst_ratio_20d"],
            -c["vol_turnover"],
            c["symbol"],
            c["market"],
        )
    )
    diagnostics["eligible_count"] = len(candidates)
    diagnostics["exclusion_counts"] = dict(sorted(diagnostics["exclusion_counts"].items()))
    selected = candidates if candidate_limit is None else candidates[:candidate_limit]
    return {
        "as_of": as_of,
        "eligible_count": len(candidates),
        "candidates": selected,
        "diagnostics": diagnostics,
    }
