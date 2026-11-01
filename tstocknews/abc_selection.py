"""Pure, forward-snapshot A/B/C comparison using the formal v8 financial filters.

A is the unmodified production builder. B and C share one complete eligible pool;
they differ only in ranking. This module performs no downloads or filesystem writes.
"""
from copy import deepcopy
from datetime import date
import math

from . import engine
from .analysis import recommendations
from .eps_improvement import apply as apply_eps
from .monthly_revenue import apply as apply_monthly
from .official import SourceError

TECHNICAL_REASONS = frozenset(('KD_OUTSIDE_CROSS_WINDOW', 'MACD_GATE_FAILED'))
RANK_FIELDS = ('institutional_net_buy_shares_3d_sum', 'revenue_yoy_3m',
               'macd_dif_slope_3_sessions')


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _identity(row):
    return str(row['market']), str(row['symbol'])


def _rank(row):
    return (-row['institutional_net_buy_shares_3d_sum'], -row['revenue_yoy_3m'],
            -row['macd_dif_slope_3_sessions'], row['symbol'], row['market'])


def _cutoff(day, financial):
    observed = financial.get('observed_date')
    try:
        date.fromisoformat(day)
        date.fromisoformat(observed)
    except (TypeError, ValueError) as error:
        raise SourceError('ABC forward comparison requires an explicit valid observation date') from error
    if observed > day:
        raise SourceError('ABC forward comparison cannot use later-observed financials')
    return day


def _history(day, sessions, prices):
    used = sorted({d for d in sessions if d <= day})[-engine.MIN_HISTORY_SESSIONS:]
    maps = {}
    for row in prices:
        if row['date'] not in used:
            continue
        key = _identity(row)
        history = maps.setdefault(key, {})
        previous = history.get(row['date'])
        if previous is not None and previous != row:
            raise ValueError('Conflicting duplicate ABC price bar')
        history[row['date']] = row
    return used, maps


def broad_c5(day, sessions, prices, institutions, financial):
    """Full C5/quality pool before KD/MACD and before EPS filing enrichment.

    The returned engine-compatible result may be passed to the shared EPS/monthly
    filters. Its candidate list is never truncated. Unknown technical inputs are
    excluded from both B and C rather than assigned a zero technical score.
    """
    cutoff = _cutoff(day, financial)
    used, price_maps = _history(day, sessions, prices)
    filtered_prices = [r for rows in price_maps.values() for r in rows.values()]
    filtered_institutions = [r for r in institutions if r['date'] in used]
    result = engine.screen(day, used, financial['universe'], filtered_prices,
                           financial['fundamentals'], filtered_institutions,
                           fundamental_as_of=cutoff, candidate_limit=None)
    names = {_identity(r): r.get('name', '') for r in financial['universe']}
    if len(names) != len(financial['universe']):
        raise ValueError('Duplicate issuer in ABC universe')
    funds = {}
    for fund in financial['fundamentals']:
        if fund.get('available_date') and fund['available_date'] <= cutoff:
            funds.setdefault(_identity(fund), []).append(fund)
    for rows in funds.values():
        rows.sort(key=lambda r: (r.get('available_date', ''), r.get('fiscal_period', '')))
    original = {_identity(r): r for r in result['candidates']}
    selected = []
    counts = {}
    for diag in result['diagnostics']['symbols'].values():
        key = _identity(diag)
        original_reasons = list(diag['reasons'])
        reasons = [r for r in original_reasons if r not in TECHNICAL_REASONS]
        diag['formal_v8_technical_reasons'] = [r for r in original_reasons if r in TECHNICAL_REASONS]
        if not reasons:
            technical_values = ('kd_k', 'kd_d', 'macd_dif', 'macd_dea')
            if (not all(_finite(diag.get(k)) for k in technical_values)
                    or type(diag.get('kd_window_pass')) is not bool
                    or type(diag.get('macd_dif_rising_3_sessions')) is not bool):
                reasons.append('ABC_TECHNICAL_INPUT_UNKNOWN')
        if reasons:
            diag['reasons'] = reasons
            for reason in set(reasons):
                counts[reason] = counts.get(reason, 0) + 1
            continue
        bars = [price_maps[key].get(d) for d in used]
        if len(bars) != engine.MIN_HISTORY_SESSIONS or not all(engine._valid_price(r) for r in bars):
            raise ValueError('ABC broad candidate lacks the canonical 120-bar window')
        f = funds[key][-1]
        last = bars[-1]
        if key in original:
            candidate = deepcopy(original[key])
        else:
            ind = engine._indicators(bars)
            candidate = {
                'symbol': key[1], 'market': key[0], 'name': names[key],
                'as_of': day, 'close': float(last['close']),
                'price_change': engine._float(last.get('price_change')),
                'price_change_pct': engine._float(last.get('price_change_pct')),
                'volume_shares': diag['volume_shares'], 'eps': diag['eps'],
                'revenue_yoy_3m': diag['revenue_yoy_3m'],
                'revenue_yoy_prev_3m': diag['revenue_yoy_prev_3m'],
                'revenue_growth_acceleration': diag['revenue_growth_acceleration'],
                'revenue_months': deepcopy(f.get('revenue_months', [])),
                'revenue_data_status': f.get('revenue_data_status'),
                'fundamental_available_date': f.get('available_date'),
                'fiscal_period': f.get('fiscal_period'), 'revenue_end_month': f.get('revenue_end_month'),
                'operating_margin': f.get('operating_margin'), 'operating_cash_flow': f.get('operating_cash_flow'),
                'macd_dif_slope_3_sessions': (ind['dif'][-1] - ind['dif'][-3]) / float(last['close']),
                'reasons': [],
            }
            candidate.update({k: deepcopy(v) for k, v in diag.items()
                if k.startswith('kd_') or k.startswith('macd_') or k.startswith('institutional_')
                or k == 'volume_shares_3d_sum'})
        if not all(_finite(candidate.get(k)) for k in RANK_FIELDS):
            diag['reasons'] = ['ABC_RANK_INPUT_UNKNOWN']
            counts['ABC_RANK_INPUT_UNKNOWN'] = counts.get('ABC_RANK_INPUT_UNKNOWN', 0) + 1
            continue
        diag['reasons'] = []
        candidate['technical_score'] = int(diag['kd_window_pass']) + int(
            diag['macd_dif'] > diag['macd_dea'] and diag['macd_dif_rising_3_sessions'])
        selected.append(candidate)
    selected.sort(key=_rank)
    result['candidates'] = selected
    result['eligible_count'] = result['diagnostics']['eligible_count'] = len(selected)
    result['diagnostics']['exclusion_counts'] = counts
    result['diagnostics']['abc_pool_scope'] = 'C5_WITHOUT_KD_MACD_GATES_BEFORE_EPS'
    return result


def build(day, sessions, prices, institutions, financial):
    """Return strategies A/B/C, the full shared B/C pool, and parity diagnostics."""
    cutoff = _cutoff(day, financial)
    # This call is the production implementation itself, with its strict cutoff.
    reference = recommendations(day, sessions, prices, institutions, financial)
    common = broad_c5(day, sessions, prices, institutions, financial)
    common = apply_eps(common, financial, cutoff, candidate_limit=None)
    common = apply_monthly(common, cutoff, candidate_limit=None)
    pool = common['candidates']
    expected_a = [r for r in pool if r['technical_score'] == 2]
    if [_identity(r) for r in expected_a[:engine.MAX_CANDIDATES]] != [
            _identity(r) for r in reference['candidates']]:
        raise ValueError('ABC shared-pool reconstruction does not reproduce formal v8')
    a = deepcopy(reference)
    strategies = {'A': a}
    for name, ordered in [('B', sorted(pool, key=_rank)),
                          ('C', sorted(pool, key=lambda r: (-r['technical_score'],) + _rank(r)))]:
        result = deepcopy(common)
        result['candidates'] = deepcopy(ordered[:engine.MAX_CANDIDATES])
        result['strategy_id'] = name
        result.update(test_only=True, excluded_from_performance=True)
        strategies[name] = result
    for name, result in strategies.items():
        result.update(strategy_id=name, test_only=True, excluded_from_performance=True)
        # Shadow families never retain signal IDs that can be mistaken for
        # formal recommendations, including the exact production A selection.
        result['signals'] = [dict(row, signal_id=f"test:abc:{name}:{day}:{row['market']}:{row['symbol']}",
            date=day, rank=rank, strategy_id=name, test_only=True, excluded_from_performance=True)
            for rank, row in enumerate(result['candidates'], 1)]
    return {'strategies': strategies, 'eligible_pool': deepcopy(pool),
            'diagnostics': {'v8_parity': True, 'b_c_same_eligible_pool': True,
                'eligible_pool_count': len(pool), 'financial_observed_date': financial['observed_date'],
                'b_c_same_selected_count': len(strategies['B']['candidates']) == len(strategies['C']['candidates'])}}
