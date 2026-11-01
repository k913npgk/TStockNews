"""Require each of the latest three legal revenue months to grow year over year."""
import re

from .engine import MAX_CANDIDATES, _float
from .official import SourceError


def months_ending(end):
    if not isinstance(end, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', end):
        return None
    year, month = map(int, end.split('-'))
    if year < 2:
        return None
    total = year * 12 + month - 1
    return [f'{(total-i)//12:04d}-{(total-i)%12+1:02d}' for i in range(3)]


def comparison(row, cutoff):
    months = months_ending(row.get('revenue_end_month'))
    if months is None or months[0] >= cutoff[:7]:
        return None
    records = row.get('revenue_months')
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        return None
    result = []
    for month in months:
        found = [r for r in records if r.get('month') == month]
        if len(found) != 1:
            return None
        current, prior = _float(found[0].get('current')), _float(found[0].get('prior_year'))
        if current is None or prior is None or current < 0 or prior <= 0:
            return None
        growth = _float(current / prior - 1)
        if growth is None:
            return None
        result.append({'month': month, 'current': current, 'prior_year': prior, 'yoy': growth})
    return result


def apply(result, cutoff, *, candidate_limit=MAX_CANDIDATES):
    selected = []
    known = 0
    counts = result['diagnostics']['exclusion_counts']
    for row in result['candidates']:
        evidence = comparison(row, cutoff)
        if evidence is None:
            reason = 'MONTHLY_REVENUE_COMPARISON_UNKNOWN'
        else:
            known += 1
            reason = None if all(r['yoy'] > 0 for r in evidence) else 'MONTHLY_REVENUE_NOT_ALL_GROWING'
        diag = result['diagnostics']['symbols'][row['market'] + ':' + row['symbol']]
        diag['monthly_revenue_comparison'] = evidence
        if reason:
            diag['reasons'].append(reason)
            counts[reason] = counts.get(reason, 0) + 1
        else:
            selected.append({**row, 'monthly_revenue_comparison': evidence,
                             'minimum_monthly_revenue_yoy': min(r['yoy'] for r in evidence)})
    if result['candidates'] and not known:
        raise SourceError('No EPS-eligible company has a verified three-month revenue comparison')
    result['eligible_count'] = result['diagnostics']['eligible_count'] = len(selected)
    result['candidates'] = selected if candidate_limit is None else selected[:candidate_limit]
    result['diagnostics']['monthly_revenue_rule'] = 'each latest three legal monthly revenue YoY > 0'
    return result
