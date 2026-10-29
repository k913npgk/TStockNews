"""Synthetic, offline ready EPS comparison snapshots for positive flow fixtures."""

from calendar import monthrange
from copy import deepcopy

from tstocknews.eps_improvement import SCHEMA


def ready(financial, target=None):
    """Return a deep-copied valid same-period EPS snapshot for every fund row."""
    result = deepcopy(financial)
    observed = result.get("observed_date") or target
    if observed is None:
        observed = next((row.get("available_date") for row in result.get("fundamentals", [])
                         if row.get("available_date")), None)
    if observed is None:
        raise ValueError("Synthetic EPS fixture needs an observation date")
    target = target or observed
    result["observed_date"] = observed
    result["eps_comparison_schema_version"] = SCHEMA
    result["eps_target_date"] = target
    for index, row in enumerate(result.get("fundamentals", [])):
        eps = float(row["eps"])
        fiscal_period = row["fiscal_period"]
        year_text, quarter_text = fiscal_period.split("-Q")
        year, quarter = int(year_text), int(quarter_text)
        month = quarter * 3
        prior_month = month
        current_end = f"{year}-{month:02d}-{monthrange(year, month)[1]:02d}"
        prior_end = f"{year - 1}-{prior_month:02d}-{monthrange(year - 1, prior_month)[1]:02d}"
        prior_eps = eps - 0.1
        comparison = {
            "eps_ytd": eps,
            "eps_prior_ytd": prior_eps,
            "statement_scope": "consolidated",
            "period_start": f"{year}-01-01",
            "period_end": current_end,
            "prior_period_start": f"{year - 1}-01-01",
            "prior_period_end": prior_end,
            "status": "KNOWN",
            "facts": {
                "eps_ytd": {"contextRef": f"synthetic-current-{index}", "unitRef": "TWD_PER_SHARE",
                            "source_member_sha256": "0" * 64},
                "eps_prior_ytd": {"contextRef": f"synthetic-prior-{index}", "unitRef": "TWD_PER_SHARE",
                                  "source_member_sha256": "1" * 64},
            },
        }
        row.update(eps_comparison_status="KNOWN", eps_prior_ytd=prior_eps,
                   eps_comparison=comparison, eps_comparison_observed_date=observed)
    return result
