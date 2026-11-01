"""Pure forward A/B/C accounting. Snapshot authenticity is checked by the caller.

Raw prices are never silently filled. An explicitly suspended stock can retain
its last observed valuation; that is not a tradable price. Entry/exit deferrals
and corporate-action cash flows are included in the actual capital ledger.
"""
from __future__ import annotations

from datetime import date
import math
import random
import statistics as st

VERSIONS = ('A', 'B', 'C')


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def valid(row):
    return bool(row and not row.get('suspended') and
                all(finite(row.get(k)) and row[k] > 0 for k in ('open', 'high', 'low', 'close')) and
                row['high'] >= max(row['open'], row['close']) and
                row['low'] <= min(row['open'], row['close']))


def _identity(row):
    return str(row['market']), str(row['symbol'])


def _prepare(snapshots):
    ordered = sorted(snapshots, key=lambda x: x['date'])
    dates = [x['date'] for x in ordered]
    if len(dates) != len(set(dates)):
        raise ValueError('Duplicate snapshot date')
    expected = set()
    for snap in ordered:
        days = snap.get('sessions')
        if not isinstance(days, list) or days != sorted(set(days)) or snap['date'] not in days:
            raise ValueError('An ordered authoritative calendar through the snapshot date is required')
        if any(d > snap['date'] for d in days):
            raise ValueError('Snapshot contains a future calendar observation')
        expected.update(d for d in days if dates[0] <= d <= dates[-1])
        for version in VERSIONS:
            rows = snap.get('selections', {}).get(version)
            if not isinstance(rows, list) or len(rows) != len({_identity(r) for r in rows}):
                raise ValueError('Missing selection list or duplicate issuer')
    missing = sorted(expected-set(dates))
    prices, actions = {}, {}
    for snap in ordered:
        day = snap['date']
        px = {}
        for row in snap.get('prices', []):
            if row.get('date') != day or _identity(row) in px:
                raise ValueError('Duplicate issuer or price date mismatch')
            px[_identity(row)] = row
        prices[day] = px
        event_map = {}
        for event in snap.get('corporate_actions', []):
            key = _identity(event)
            if event.get('date') != day or key in event_map:
                raise ValueError('Duplicate corporate action or date mismatch')
            event_map[key] = event
        actions[day] = event_map
    return ordered, dates, prices, actions, missing


def _action_known(event):
    known = (event.get('verified') is True and bool(event.get('source_url')) and
            finite(event.get('share_multiplier')) and event['share_multiplier'] > 0 and
            finite(event.get('cash_per_old_share')) and event['cash_per_old_share'] >= 0)
    if not known: return False
    if event['cash_per_old_share']:
        try:
            return date.fromisoformat(event['cash_payment_date']) >= date.fromisoformat(event['date'])
        except (KeyError, TypeError, ValueError):
            return False
    return True


def simulate(snapshots, version, bps, protocol, decision_dates=None, end_date=None, *, _prepared=None):
    """Funded h-sleeve ledger, with deferred orders and no capital reuse.

    A missing/zero quote without an explicit suspension blocks the path. Known
    suspended entries may wait up to max_entry_wait_sessions, then remain cash.
    A suspended exit waits for a valid quote. A busy sleeve skips a new signal.
    Corporate actions take effect before the opening auction, on old holdings.
    """
    h = int(protocol.get('horizon', 60))
    wait = int(protocol.get('max_entry_wait_sessions', 5))
    if h < 1 or wait < 0 or not finite(bps) or bps < 0 or bps >= 20000:
        raise ValueError('Invalid accounting parameters')
    if version not in VERSIONS:
        raise ValueError(version)
    prepared = _prepared or _prepare(snapshots)
    ordered, dates, prices, events, missing_sessions = prepared
    if missing_sessions:
        return dict(status='BLOCKED_MISSING_SESSION', missing=missing_sessions, curve=[],
                    total_return=None, max_drawdown=None, completed=False)
    if not dates:
        return dict(status='NO_SNAPSHOTS', curve=[], total_return=None, max_drawdown=None, completed=False)
    index = {d:i for i,d in enumerate(dates)}
    decisions = dates if decision_dates is None else list(decision_dates)
    if any(d not in index for d in decisions) or decisions != sorted(set(decisions)):
        raise ValueError('Decisions must be unique ordered observed dates')
    stop = index[end_date] if end_date is not None else len(dates)-1
    decisions = [d for d in decisions if index[d] <= stop]
    if not decisions:
        return dict(status='NO_DECISIONS', curve=[], total_return=None, max_drawdown=None, completed=False)
    first = index[decisions[0]]
    selected = {s['date']:s['selections'][version] for s in ordered}
    entries = {index[d]+1:d for d in decisions}
    sleeves = [dict(cash=1/h, pending=[], positions=[], receivables=[]) for _ in range(h)]
    curve, errors, fills, nofills, deferred = [], [], [], [], []
    skipped = []; traded = 0.; peak = 1.; max_dd = 0.
    last_liquidation = None; last_settlement = None
    nominal_end = max(index[d]+h for d in decisions)
    for i in range(first, stop+1):
        day = dates[i]; day_prices = prices[day]
        for sleeve in sleeves:
            unpaid=[]
            for payment in sleeve['receivables']:
                if payment['date'] <= day:
                    sleeve['cash'] += payment['amount']; last_settlement=day
                else: unpaid.append(payment)
            sleeve['receivables']=unpaid
        # Shares and cash are adjusted only from verified events on old holdings.
        for sleeve in sleeves:
            for pos in sleeve['positions']:
                event = events[day].get(pos['key'])
                if event:
                    if not _action_known(event):
                        errors.append(dict(date=day, key=pos['key'], stage='CORPORATE_ACTION_UNKNOWN')); break
                    old = pos['shares']; mult = event['share_multiplier']; cash = event['cash_per_old_share']
                    if cash:
                        if event['cash_payment_date'] <= day:
                            sleeve['cash'] += old*cash; last_settlement=day
                        else:
                            sleeve['receivables'].append(dict(date=event['cash_payment_date'],amount=old*cash))
                    pos['shares'] = old*mult
                    pos['last_mark'] = (pos['last_mark']-cash)/mult
                    if not finite(pos['last_mark']) or pos['last_mark'] <= 0:
                        errors.append(dict(date=day,key=pos['key'],stage='INVALID_ACTION_REFERENCE')); break
            if errors: break
        if errors: break
        if i in entries:
            decision = entries[i]
            sleeve = sleeves[(i-first-1) % h]
            if sleeve['positions'] or sleeve['pending']:
                skipped.append(dict(date=day, decision=decision, reason='NO_AVAILABLE_SLEEVE'))
            else:
                rows = selected[decision]
                capital = sleeve['cash']
                sleeve['pending'] = [dict(key=_identity(row), budget=capital/len(rows),
                    intended=i, decision=decision) for row in rows] if rows else []
        # Pending budgets stay in sleeve cash; they are never offered to a second order.
        for sleeve in sleeves:
            pending = []
            for order in sleeve['pending']:
                row = day_prices.get(order['key'])
                if row and row.get('corporate_action_unresolved'):
                    errors.append(dict(date=day,key=order['key'],stage='ENTRY_ACTION_UNKNOWN')); break
                if valid(row):
                    shares = order['budget']/(row['open']*(1+bps/20000))
                    sleeve['cash'] -= order['budget']
                    sleeve['positions'].append(dict(key=order['key'],shares=shares,
                        exit=i+h-1,last_mark=row['open']))
                    traded += shares*row['open']
                    fills.append(dict(date=day,key=order['key'],decision=order['decision'],shares=shares))
                elif row and row.get('suspended') is True:
                    if i-order['intended'] >= wait:
                        nofills.append(dict(date=day,key=order['key'],reason='SUSPENDED_ENTRY_EXPIRED'))
                    else:
                        pending.append(order)
                        deferred.append(dict(date=day,key=order['key'],stage='ENTRY'))
                else:
                    errors.append(dict(date=day,key=order['key'],stage='ENTRY_PRICE_UNKNOWN')); break
            sleeve['pending'] = pending
            if errors: break
        if errors: break
        invested = 0.
        for sleeve in sleeves:
            remaining = []
            for pos in sleeve['positions']:
                row = day_prices.get(pos['key'])
                if row and row.get('corporate_action_unresolved'):
                    errors.append(dict(date=day,key=pos['key'],stage='HOLDING_ACTION_UNKNOWN')); break
                if valid(row):
                    pos['last_mark'] = row['close']
                    value = pos['shares']*row['close']
                    if i >= pos['exit']:
                        sleeve['cash'] += value*(1-bps/20000)
                        traded += value; last_liquidation = day
                    else:
                        invested += value; remaining.append(pos)
                elif row and row.get('suspended') is True:
                    invested += pos['shares']*pos['last_mark']; remaining.append(pos)
                    deferred.append(dict(date=day,key=pos['key'],stage='EXIT' if i>=pos['exit'] else 'VALUATION'))
                else:
                    errors.append(dict(date=day,key=pos['key'],stage='MARK_OR_EXIT_PRICE_UNKNOWN')); break
            sleeve['positions'] = remaining
            if errors: break
        if errors: break
        cash = sum(s['cash'] for s in sleeves)
        if cash < -1e-10:
            raise ValueError('Accounting produced negative cash')
        receivable=sum(p['amount'] for s in sleeves for p in s['receivables'])
        nav = cash+invested+receivable; peak=max(peak,nav); max_dd=min(max_dd,nav/peak-1)
        curve.append(dict(date=day,nav=nav,cash=cash,receivables=receivable,
            invested_fraction=invested/nav if nav else 0))
        if end_date is None and i>=nominal_end and not any(s['positions'] or s['pending'] or s['receivables'] for s in sleeves):
            stop=i; break
    pending = sum(len(s['pending']) for s in sleeves)
    held = sum(len(s['positions']) for s in sleeves)
    unpaid=sum(len(s['receivables']) for s in sleeves)
    completed = not errors and not held and not pending and not unpaid and stop >= nominal_end
    status = 'BLOCKED_ACCOUNTING_INPUT' if errors else 'COMPLETE_CAPITAL_SIMULATION' if completed else 'PARTIAL_CAPITAL_SIMULATION'
    return dict(status=status,completed=completed,curve=curve,missing=errors,
        total_return=(curve[-1]['nav']-1 if completed else None),
        partial_mark_return=(curve[-1]['nav']-1 if curve else None),
        max_drawdown=(max_dd if completed else None),partial_max_drawdown=max_dd,
        mean_invested_fraction=st.mean(x['invested_fraction'] for x in curve) if curve else None,
        fills=fills,no_fills=nofills,deferred=deferred,skipped_signals=skipped,
        traded_notional_over_initial_nav=traded,last_liquidation_date=last_liquidation,
        last_settlement_date=last_settlement,pending_receivables=unpaid,
        pending_orders=pending,open_positions=held)


def _bootstrap_lower(values, h, reps, seed, alpha):
    rng=random.Random(seed); n=len(values); doubled=values+values
    prefix=[0.]
    for x in doubled: prefix.append(prefix[-1]+x)
    means=[]
    for _ in range(reps):
        count=0; total=0.
        while count<n:
            start=rng.randrange(n); width=min(h,n-count)
            total+=prefix[start+width]-prefix[start]; count+=width
        means.append(total/n)
    means.sort()
    return means[max(0,math.ceil(alpha*reps)-1)]


def evaluate(snapshots, protocol):
    """Evaluate mature cohorts and funded NAV without asserting independent OOS.

    ``minimum_decision_dates`` is an eligibility floor, not a power guarantee.
    The caller must verify observation cutoffs, action coverage and sealed inputs.
    """
    prepared=_prepare(snapshots); ordered,dates,_,_,missing=prepared
    h=int(protocol.get('horizon',60)); costs=protocol.get('costs_bps',[60,100])
    minimum=int(protocol.get('minimum_decision_dates',360))
    if h<1 or minimum<1 or not costs: raise ValueError('Invalid protocol')
    primary_cost=protocol.get('primary_cost_bps',costs[0])
    if primary_cost not in costs: raise ValueError('Primary cost must be included in cost scenarios')
    if missing:
        return dict(status='BLOCKED_MISSING_SESSION',missing_sessions=missing,formal_holdout_pass=False)
    if not dates:
        return dict(status='INCONCLUSIVE',reason='NO_SNAPSHOTS',formal_holdout_pass=False)
    # A single funded sleeve would otherwise invest 1/h. Scale its final capital
    # increment back to the whole decision-day cohort, including no-fill cash.
    cohorts={v:{} for v in VERSIONS}; completion={}; eligible=dates[:-h] if len(dates)>h else []
    for d in eligible:
        per={}
        for v in VERSIONS:
            per[v]={}
            for bps in costs:
                sim=simulate(snapshots,v,bps,protocol,[d],_prepared=prepared)
                per[v][str(bps)]=dict(status=sim['status'],completed=sim['completed'],
                    net_return=sim['total_return']*h if sim['completed'] else None,
                    last_liquidation_date=sim['last_liquidation_date'],last_settlement_date=sim['last_settlement_date'],missing=sim['missing'],
                    no_fills=len(sim['no_fills']),deferred=len(sim['deferred']))
            cohorts[v][d]=per[v]
        completion[d]=all(per[v][str(bps)]['completed'] for v in VERSIONS for bps in costs)
    common=[d for d in eligible if completion[d]]
    # A completed capital comparison uses a contiguous mature decision prefix.
    prefix=[]
    for d in eligible:
        if not completion[d]: break
        prefix.append(d)
    completed_end=None
    if prefix:
        exit_dates=[cohorts[v][d][str(bps)][field] for d in prefix for v in VERSIONS for bps in costs
                    for field in ('last_liquidation_date','last_settlement_date')]
        completed_end=max([dates[dates.index(prefix[-1])+h]]+[d for d in exit_dates if d])
    portfolios={v:{str(bps):dict(current=simulate(snapshots,v,bps,protocol,_prepared=prepared),
        completed_window=simulate(snapshots,v,bps,protocol,prefix,completed_end,_prepared=prepared)
        if prefix else dict(status='NO_COMPLETE_COMMON_WINDOW',completed=False,total_return=None,max_drawdown=None))
        for bps in costs} for v in VERSIONS}
    # The first N registered decision dates are a fixed primary checkpoint.
    # Later captures can extend descriptive diagnostics, never its test window.
    fixed_primary_dates=dates[:minimum]
    primary_ready=(len(fixed_primary_dates)==minimum and
                   all(completion.get(d,False) for d in fixed_primary_dates))
    primary_maturity_end=None
    if primary_ready:
        primary_endings=[cohorts[v][d][str(bps)][field] for d in fixed_primary_dates
                         for v in VERSIONS for bps in costs
                         for field in ('last_liquidation_date','last_settlement_date')]
        primary_maturity_end=max([dates[dates.index(fixed_primary_dates[-1])+h]]+
                                 [d for d in primary_endings if d])
    for v in VERSIONS:
        for bps in costs:
            portfolios[v][str(bps)]['primary_window']=(
                simulate(snapshots,v,bps,protocol,fixed_primary_dates,primary_maturity_end,_prepared=prepared)
                if primary_ready else dict(status='WAITING_FIXED_PRIMARY_MATURITY',completed=False,
                    total_return=None,max_drawdown=None))
    comparisons={}; primary_statistics={}
    for newer,older in [('B','A'),('C','A'),('C','B')]:
        name=f'{newer}_minus_{older}'; comparisons[name]={}; primary_statistics[name]={}
        for bps in costs:
            vals=[cohorts[newer][d][str(bps)]['net_return']-cohorts[older][d][str(bps)]['net_return'] for d in common]
            different=sum({_identity(r) for r in ordered[dates.index(d)]['selections'][newer]} !=
                          {_identity(r) for r in ordered[dates.index(d)]['selections'][older]} for d in common)
            comparisons[name][str(bps)]=dict(n=len(common),increment=st.mean(vals) if vals else None,
                differing_selection_dates=different,lower_bound=None,
                status='NO_INCREMENTAL_EXPOSURE' if vals and different==0 else 'DESCRIPTIVE_ONLY')
            primary=dict(target_n=minimum,n=sum(completion.get(d,False) for d in fixed_primary_dates),
                increment=None,lower_bound=None,differing_selection_dates=None,
                window_start=fixed_primary_dates[0] if fixed_primary_dates else None,
                window_end=fixed_primary_dates[-1] if len(fixed_primary_dates)==minimum else None,
                maturity_end_date=primary_maturity_end,status='WAITING_FIXED_PRIMARY_MATURITY')
            if primary_ready:
                primary_vals=[cohorts[newer][d][str(bps)]['net_return']-cohorts[older][d][str(bps)]['net_return']
                              for d in fixed_primary_dates]
                primary_different=sum({_identity(r) for r in ordered[dates.index(d)]['selections'][newer]} !=
                    {_identity(r) for r in ordered[dates.index(d)]['selections'][older]} for d in fixed_primary_dates)
                primary.update(increment=st.mean(primary_vals),differing_selection_dates=primary_different)
                if newer!='C':
                    primary['status']='DIAGNOSTIC_ONLY'
                elif bps!=primary_cost:
                    primary['status']='STRESS_DIAGNOSTIC_ONLY'
                elif primary_different==0:
                    primary['status']='NO_INCREMENTAL_EXPOSURE'
                else:
                    # The fixed family contains only C-A and C-B. Re-evaluation
                    # uses exactly the same first-N values, not an expanding test.
                    primary.update(lower_bound=_bootstrap_lower(primary_vals,h,
                        int(protocol.get('bootstrap_reps',10000)),int(protocol.get('seed',1729)),.05/2),
                        status='FIXED_PRIMARY_INTERVAL_COMPUTED')
            primary_statistics[name][str(bps)]=primary
    # Deliberately never upgrades the caller's evidence to independent/OOS.
    return dict(status='FORWARD_EVALUATED',formal_holdout_pass=False,
        evidence_status='AUTHENTICITY_AND_ACTION_COVERAGE_REQUIRE_CALLER_VALIDATION',
        snapshot_dates=len(dates),nominal_mature_decision_dates=len(eligible),
        mature_common_dates=common,mature_coverage=len(common)/len(eligible) if eligible else 0,
        complete_common_prefix_dates=prefix,completed_comparison_end=completed_end,
        cohorts=cohorts,portfolios=portfolios,comparisons=comparisons,
        fixed_primary_dates=fixed_primary_dates,primary_statistics=primary_statistics,
        limitations=['Fractional shares and sufficient liquidity assumed; no borrowing.',
            'Explicit suspension valuation is an observed-price carry, never an execution price.',
            'Only supplied verified corporate actions are applied; completeness is not inferred.',
            '360 dates or a bootstrap interval alone does not establish power or independent efficacy.'])
