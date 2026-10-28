"""One recommendation builder for production and isolated tests."""
from . import STRATEGY_VERSION
from .catchup import validate as validate_catchup
from .engine import MIN_HISTORY_SESSIONS, _float, screen
from .official import SourceError
from .storage import digest


def supports_revenue_acceleration(financial):
    rows = financial.get("fundamentals", [])
    return bool(rows) and all("revenue_yoy_prev_3m" in row for row in rows)


def recommendations(day, sessions, prices, institutions, financial, *, test_run_id=None,
                    late_financial_date=None):
    used_sessions = sorted({s for s in sessions if s <= day})[-MIN_HISTORY_SESSIONS:]
    used_prices = [r for r in prices if r["date"] in used_sessions]
    used_institutions = [r for r in institutions if r["date"] in used_sessions]
    observed = financial.get("observed_date", day)
    if late_financial_date:
        if test_run_id:
            raise SourceError("Formal catch-up cannot be combined with a test run")
        validate_catchup(day, late_financial_date)
        if observed != late_financial_date:
            raise SourceError("Catch-up snapshot must retain its actual approved observation date")
    if not test_run_id and not late_financial_date and observed > day:
        raise SourceError("Production cannot use a financial snapshot observed after the target date")
    for market in {r["market"] for r in financial["universe"]}:
        rows = [r for r in financial["fundamentals"] if r["market"] == market]
        if not any(r.get("eps") is not None for r in rows) or not any(r.get("revenue_yoy_3m") is not None for r in rows):
            raise SourceError(f"{market} financial source has no usable EPS or revenue observations")
        if not any(_float(r.get("revenue_yoy_3m")) is not None
                   and _float(r.get("revenue_yoy_prev_3m")) is not None for r in rows):
            raise SourceError(f"{market} financial source has no usable revenue acceleration observations")
    # Only tests or the expiring approved catch-up can use a later observation cutoff.
    financial_cutoff = max(day, observed) if test_run_id or late_financial_date else day
    result = screen(day, used_sessions, financial["universe"], used_prices,
                    financial["fundamentals"], used_institutions,
                    fundamental_as_of=financial_cutoff)
    result["strategy_version"] = STRATEGY_VERSION
    result["data_hash"] = digest({"prices": used_prices, "institutions": used_institutions,
                                  "financial": financial})
    catchup = validate_catchup(day, late_financial_date) if late_financial_date else {}
    result.update(catchup)
    prefix = f"test:{test_run_id}:" if test_run_id else ""
    result["signals"] = [{"signal_id": f"{prefix}{STRATEGY_VERSION}:{day}:{r['market']}:{r['symbol']}",
                          "date": day, "rank": i, "strategy_version": STRATEGY_VERSION,
                          **r, **catchup, **({"test_only": True, "excluded_from_performance": True} if test_run_id else {})}
                         for i, r in enumerate(result["candidates"], 1)]
    result["financial_observed_date"] = observed
    if test_run_id:
        result.update(test_only=True, excluded_from_performance=True, test_run_id=test_run_id,
                      late_financial_snapshot=observed > day)
    return result
