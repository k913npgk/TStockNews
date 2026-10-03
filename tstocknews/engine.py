"""Deterministic, dependency-free stock screening and outcome tracking."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
from typing import Any


HORIZONS = (1, 3, 5, 10, 20)
MIN_HISTORY_SESSIONS = 120
MIN_VOLUME_SHARES = 2_000_000


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ema(values: list[float], period: int) -> list[float]:
    """EMA seeded with the first observation, as required by the v1 contract."""
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _indicators(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(row["close"]) for row in rows]
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]

    rsv: list[float] = []
    k_values: list[float] = []
    d_values: list[float] = []
    previous_rsv = 50.0
    k = d = 50.0
    for i in range(len(rows)):
        if i >= 8:
            hi = max(highs[i - 8 : i + 1])
            lo = min(lows[i - 8 : i + 1])
            if hi != lo:
                previous_rsv = (closes[i] - lo) / (hi - lo) * 100.0
        rsv.append(previous_rsv)
        k = (2.0 * k + previous_rsv) / 3.0
        d = (2.0 * d + k) / 3.0
        k_values.append(k)
        d_values.append(d)

    dif = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    dea = _ema(dif, 9)
    return {"rsv": rsv, "k": k_values, "d": d_values, "dif": dif, "dea": dea}


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("symbol", "")), str(row.get("market", ""))


def _valid_price(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    vals = [_float(row.get(key)) for key in ("open", "high", "low", "close")]
    return (all(v is not None and v > 0 for v in vals)
            and vals[1] >= max(vals[0], vals[3]) and vals[2] <= min(vals[0], vals[3])
            and int(row.get("volume_shares") or 0) >= 0)


def screen(
    as_of: str,
    sessions: list[str],
    universe: list[dict[str, Any]],
    prices: list[dict[str, Any]],
    fundamentals: list[dict[str, Any]],
    institutional: list[dict[str, Any]],
    *, fundamental_as_of: str | None = None,
) -> dict[str, Any]:
    """Apply all v1 gates and return at most ten ranked candidates.

    `sessions` is the authoritative ordered exchange-session calendar. Price gaps
    are never forward-filled. Fundamental records are point-in-time filtered by
    `available_date`.
    """
    _date(as_of)
    financial_cutoff = fundamental_as_of or as_of
    _date(financial_cutoff)
    ordered_sessions = sorted({s for s in sessions if s <= as_of})
    session_set = set(ordered_sessions)
    diagnostics: dict[str, Any] = {
        "as_of": as_of,
        "fundamental_as_of": financial_cutoff,
        "calendar_sessions_available": len(ordered_sessions),
        "minimum_history_sessions": MIN_HISTORY_SESSIONS,
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

    fundamentals_map: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in fundamentals:
        available = row.get("available_date")
        if available and available <= financial_cutoff:
            fundamentals_map[_row_key(row)].append(row)
    for rows in fundamentals_map.values():
        rows.sort(key=lambda r: (r.get("available_date", ""), r.get("fiscal_period", "")))

    inst_map: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in institutional:
        ds = row.get("date")
        if ds in session_set:
            inst_map[_row_key(row)][ds] += int(row.get("net_buy_shares") or 0)

    recent_sessions = ordered_sessions[-3:]
    candidates: list[dict[str, Any]] = []
    for company in universe:
        key = _row_key(company)
        symbol = key[0]
        market = key[1]
        label = f"{market}:{symbol}"
        reasons: list[str] = []
        diag: dict[str, Any] = {"symbol": symbol, "market": market, "reasons": reasons}
        diagnostics["symbols"][label] = diag

        hist = price_map.get(key, {})
        streak: list[dict[str, Any]] = []
        # Keep only the contiguous, fully valid price streak ending on as_of.
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
        # Indicators use the available contiguous streak, never fabricated bars.
        if any(row.get("corporate_action_unresolved") for row in streak[-MIN_HISTORY_SESSIONS:]):
            reasons.append("CORPORATE_ACTION_UNRESOLVED")
            diagnostics["exclusion_counts"]["CORPORATE_ACTION_UNRESOLVED"] += 1
            continue

        # Fixed rolling 120-session initialization makes the warm-up contract reproducible.
        streak = streak[-MIN_HISTORY_SESSIONS:]
        ind = _indicators(streak)
        last_i = len(streak) - 1
        last_rows = streak[-3:]
        latest_date = streak[-1]["date"]
        diag["price_date"] = latest_date
        if latest_date != as_of:
            reasons.append("MISSING_AS_OF_PRICE")
        if len(recent_sessions) < 3:
            reasons.append("INSUFFICIENT_INSTITUTIONAL_SESSIONS")
        volume = int(streak[-1].get("volume_shares") or 0)
        net3 = [inst_map[key].get(ds, 0) for ds in recent_sessions]
        if any(ds not in inst_map[key] for ds in recent_sessions):
            reasons.append("MISSING_INSTITUTIONAL_DATA")
        volume3_rows = [hist.get(ds) for ds in recent_sessions]
        sum_volume3 = sum(int(r.get("volume_shares") or 0) for r in volume3_rows if r)
        sum_net3 = sum(net3)
        ratio = sum_net3 / sum_volume3 if sum_volume3 > 0 else None
        cross = ind["k"][last_i - 1] <= ind["d"][last_i - 1] and ind["k"][last_i] > ind["d"][last_i]
        dif_rising = ind["dif"][last_i - 2] < ind["dif"][last_i - 1] < ind["dif"][last_i]
        latest_dif, latest_dea = ind["dif"][last_i], ind["dea"][last_i]

        available_fundamentals = fundamentals_map.get(key, [])
        fund = available_fundamentals[-1] if available_fundamentals else None
        eps = _float(fund.get("eps")) if fund else None
        revenue = _float(fund.get("revenue_yoy_3m")) if fund else None
        if fund is None:
            reasons.append("NO_AVAILABLE_FUNDAMENTAL")
        elif eps is None or eps <= 0:
            reasons.append("EPS_NOT_POSITIVE")
        if fund is not None and (revenue is None or revenue <= 0):
            reasons.append("REVENUE_GROWTH_NOT_POSITIVE")
        if not cross:
            reasons.append("KD_NO_GOLDEN_CROSS")
        if not (latest_dif > latest_dea and dif_rising):
            reasons.append("MACD_GATE_FAILED")
        if len(net3) != 3 or not all(value > 0 for value in net3):
            reasons.append("INSTITUTIONAL_NOT_BUYING_3_SESSIONS")
        if volume < MIN_VOLUME_SHARES:
            reasons.append("VOLUME_BELOW_2000_LOTS")
        if latest_date != as_of:
            # The earlier missing-as-of reason remains decisive even if stale gates pass.
            pass

        diag.update({
            "kd_k": ind["k"][last_i], "kd_d": ind["d"][last_i], "kd_golden_cross": cross,
            "macd_dif": latest_dif, "macd_dea": latest_dea, "macd_dif_rising_3_sessions": dif_rising,
            "institutional_net_buy_shares_3d": net3, "institutional_net_buy_shares_3d_sum": sum_net3,
            "volume_shares": volume, "volume_shares_3d_sum": sum_volume3,
            "institutional_buy_volume_ratio": ratio,
            "fundamental_available_date": fund.get("available_date") if fund else None,
            "fiscal_period": fund.get("fiscal_period") if fund else None,
            "eps": eps, "revenue_yoy_3m": revenue,
        })
        if reasons:
            for reason in set(reasons):
                diagnostics["exclusion_counts"][reason] += 1
            continue

        dif_slope = (ind["dif"][last_i] - ind["dif"][last_i - 2]) / float(streak[-1]["close"])
        candidates.append({
            "symbol": symbol, "market": market, "name": company.get("name", ""),
            "as_of": as_of, "close": float(streak[-1]["close"]),
            "volume_shares": volume, "eps": eps, "revenue_yoy_3m": revenue,
            "fundamental_available_date": fund.get("available_date") if fund else None,
            "fiscal_period": fund.get("fiscal_period") if fund else None,
            "kd_k": ind["k"][last_i], "kd_d": ind["d"][last_i],
            "revenue_end_month": fund.get("revenue_end_month"),
            "operating_margin": fund.get("operating_margin"),
            "operating_cash_flow": fund.get("operating_cash_flow"),
            "macd_dif": latest_dif, "macd_dea": latest_dea,
            "institutional_net_buy_shares_3d": net3,
            "institutional_net_buy_shares_3d_sum": sum_net3,
            "volume_shares_3d_sum": sum_volume3,
            "institutional_buy_volume_ratio": ratio,
            "macd_dif_slope_3_sessions": dif_slope,
            "reasons": [],
        })

    candidates.sort(key=lambda c: (
        -(c["institutional_buy_volume_ratio"] if c["institutional_buy_volume_ratio"] is not None else float("-inf")),
        -(c["revenue_yoy_3m"] if c["revenue_yoy_3m"] is not None else float("-inf")),
        -c["macd_dif_slope_3_sessions"], c["symbol"], c["market"],
    ))
    diagnostics["eligible_count"] = len(candidates)
    diagnostics["exclusion_counts"] = dict(sorted(diagnostics["exclusion_counts"].items()))
    return {"as_of": as_of, "eligible_count": len(candidates), "candidates": candidates[:10], "diagnostics": diagnostics}


def track(
    signals: list[dict[str, Any]],
    sessions: list[str],
    prices: list[dict[str, Any]],
    as_of: str,
) -> list[dict[str, Any]]:
    """Track open-to-close returns at fixed session horizons, preserving unknowns."""
    _date(as_of)
    ordered_sessions = sorted(set(sessions))
    index = {ds: i for i, ds in enumerate(ordered_sessions)}
    price_map: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in prices:
        price_map[_row_key(row)][row.get("date", "")] = row

    result: list[dict[str, Any]] = []
    for signal in sorted(signals, key=lambda s: (s.get("date", ""), s.get("market", ""), s.get("symbol", ""), str(s.get("signal_id", "")))):
        if signal.get("test_only") or signal.get("excluded_from_performance"):
            continue
        sig_date = signal["date"]
        base = {"signal_id": signal.get("signal_id"), "date": sig_date, "symbol": signal["symbol"], "market": signal["market"]}
        entry = {"date": None, "open": None}
        signal_idx = index.get(sig_date)
        if signal_idx is None:
            # A signal date outside the calendar cannot be safely mapped to a session.
            for horizon in HORIZONS:
                result.append({**base, "horizon": horizon, "status": "MISSING_PRICE", "entry_date": None, "exit_date": None, "entry_open": None, "exit_close": None, "price_return": None, "research_event": None})
            continue
        key = _row_key(signal)
        entry_idx = signal_idx + 1
        if entry_idx < len(ordered_sessions):
            entry["date"] = ordered_sessions[entry_idx]
            entry_row = price_map.get(key, {}).get(entry["date"])
            if entry_row and entry_row.get("suspended"):
                entry["status"] = "SUSPENDED"
            elif entry_row and entry_row.get("corporate_action_unresolved"):
                entry["status"] = "CORPORATE_ACTION_UNRESOLVED"
            elif entry_row and _float(entry_row.get("open")) is not None and float(entry_row["open"]) > 0:
                entry["open"] = float(entry_row["open"])
        else:
            entry["status"] = "PENDING_ENTRY"

        for horizon in HORIZONS:
            exit_idx = signal_idx + horizon
            exit_date = ordered_sessions[exit_idx] if exit_idx < len(ordered_sessions) else None
            entry_date = entry["date"]
            status: str
            exit_close: float | None = None
            ret: float | None = None
            event: bool | None = None
            if entry_idx >= len(ordered_sessions) or (entry_date and entry_date > as_of):
                status = "PENDING_ENTRY"
            elif entry.get("status"):
                status = entry["status"]
            elif entry.get("open") is None:
                status = "MISSING_PRICE"
            elif exit_date is None or exit_date > as_of:
                status = "NOT_MATURE"
            else:
                exit_row = price_map.get(key, {}).get(exit_date)
                interval = ordered_sessions[entry_idx : exit_idx + 1]
                interval_rows = [price_map.get(key, {}).get(ds) for ds in interval]
                if any(row and row.get("corporate_action_unresolved") for row in interval_rows):
                    status = "CORPORATE_ACTION_UNRESOLVED"
                elif exit_row and exit_row.get("suspended"):
                    status = "SUSPENDED"
                elif exit_row and exit_row.get("corporate_action_unresolved"):
                    status = "CORPORATE_ACTION_UNRESOLVED"
                elif not exit_row or _float(exit_row.get("close")) is None or float(exit_row["close"]) <= 0:
                    status = "MISSING_PRICE"
                else:
                    exit_close = float(exit_row["close"])
                    ret = exit_close / float(entry["open"]) - 1.0 if entry["open"] else None
                    status = "MATURE" if ret is not None else "MISSING_PRICE"
                    event = (ret >= 0.05) if horizon == 10 and ret is not None else None
            result.append({
                **base, "horizon": horizon, "status": status, "entry_date": entry_date,
                "exit_date": exit_date, "entry_open": entry["open"], "exit_close": exit_close,
                "price_return": ret, "research_event": event,
            })
    return result
