"""Offline literature hypotheses; accounting facts and returns remain auditable."""
from __future__ import annotations

import bisect
import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from html import unescape
import math
import re
import statistics as st


VARIANTS = ("v8", "v8_common", "momentum", "quality_momentum",
            "revenue_surprise_proxy", "value_momentum", "common_equal_weight")
NAMES = {"v8": "v8原股票池歷史代理", "v8_common": "v8共同資料池",
         "momentum": "中期價格動能", "quality_momentum": "ROE品質＋動能代理",
         "revenue_surprise_proxy": "營收驚喜模型代理", "value_momentum": "益本比價值＋動能",
         "common_equal_weight": "共同資料池等權對照"}
FACT_NAMES = {"BasicEarningsLossPerShare", "ProfitLossAttributableToOwnersOfParent",
              "EquityAttributableToOwnersOfParent", "ProfitLoss", "Equity"}
ATTR = re.compile(r'([\w:.-]+)\s*=\s*[\"\']([^\"\']*)[\"\']')
CONTEXT = re.compile(r'<xbrli:context\b([^>]*)>(.*?)</xbrli:context\s*>', re.I | re.S)
UNIT = re.compile(r'<xbrli:unit\b([^>]*)>(.*?)</xbrli:unit\s*>', re.I | re.S)
# Target only statement facts, rather than parsing every note in a large filing.
FACT = re.compile(r'<ix:nonfraction\b([^>]*\bname\s*=\s*[\"\'][^\"\']*:'
                  r'(?:BasicEarningsLossPerShare|ProfitLossAttributableToOwnersOfParent|'
                  r'EquityAttributableToOwnersOfParent|ProfitLoss|Equity)[\"\'][^>]*)>'
                  r'(.*?)</ix:nonfraction\s*>', re.I | re.S)


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def attrs(s):
    return {k.lower(): unescape(v) for k, v in ATTR.findall(s)}


def text(s):
    return unescape(re.sub(r'<[^>]*>', '', s)).strip()


def numeric(a, body):
    if a.get('xsi:nil', '').lower() in ('true', '1'):
        return None
    if a.get('format', '').split(':')[-1] not in ('', 'numdotdecimal', 'numcommadot'):
        return None
    value = text(body).replace(',', '').replace('\u00a0', '').strip()
    if not value or a.get('sign', '') not in ('', '+', '-'):
        return None
    paren = value.startswith('(') and value.endswith(')')
    if paren:
        if a.get('sign'):
            return None
        value = '-' + value[1:-1]
    try:
        scale = a.get('scale', '0')
        if not re.fullmatch(r'[+-]?\d{1,2}', scale):
            return None
        val = Decimal(value) * Decimal(10) ** int(scale)
        if a.get('sign') in ('-', '+') and val < 0:
            return None
        if a.get('sign') == '-':
            val = -val
        result = float(val)
        return result if finite(result) else None
    except (ValueError, InvalidOperation, OverflowError):
        return None


def extract_filing(body, symbol, period, scope):
    """Read unique nondimensional TWD facts for exact dates and entity ID.

    Conflicting duplicate contexts, units or facts become unknown. Monetary
    values include iXBRL scale/sign. EPS requires a declared TWD/share unit.
    """
    s = body.decode('utf-8-sig')
    contexts, context_conflicts = {}, set()
    for head, content in CONTEXT.findall(s):
        key = attrs(head).get('id')
        if not key:
            continue
        identifier = re.findall(r'<xbrli:identifier\b[^>]*>(.*?)</xbrli:identifier>', content, re.I | re.S)
        dates = {name: re.findall(rf'<xbrli:{name}\b[^>]*>(.*?)</xbrli:{name}>', content, re.I | re.S)
                 for name in ('instant', 'startdate', 'enddate')}
        item = (tuple(text(x) for x in identifier),
                tuple((name, tuple(text(x) for x in values)) for name, values in dates.items()),
                bool(re.search(r'<(?:xbrldi:)?(?:explicitmember|typedmember)\b', content, re.I)))
        if key in contexts and contexts[key] != item:
            context_conflicts.add(key)
        contexts[key] = item
    units, unit_conflicts = {}, set()
    for head, content in UNIT.findall(s):
        key = attrs(head).get('id')
        measures = tuple(x.strip().lower() for x in re.findall(r'<xbrli:measure\b[^>]*>\s*([^<]+)\s*</xbrli:measure>', content, re.I))
        item = (measures, bool(re.search(r'<xbrli:divide\b', content, re.I)),
                bool(re.search(r'<xbrli:unitnumerator\b[^>]*>\s*<xbrli:measure>\s*iso4217:TWD\s*</xbrli:measure>', content, re.I)),
                bool(re.search(r'<xbrli:unitdenominator\b[^>]*>\s*<xbrli:measure>\s*xbrli:shares\s*</xbrli:measure>', content, re.I)))
        if key in units and units[key] != item:
            unit_conflicts.add(key)
        units[key] = item
    y, q = int(period[:4]), int(period[-1])
    end = date(y, q*3, calendar.monthrange(y, q*3)[1]).isoformat()
    prior_end = date(y-1, q*3, calendar.monthrange(y-1, q*3)[1]).isoformat()
    values = {}
    for head, content in FACT.findall(s):
        a = attrs(head)
        key, ukey = a.get('contextref'), a.get('unitref')
        if key in context_conflicts or ukey in unit_conflicts:
            continue
        ctx, unit = contexts.get(key), units.get(ukey)
        if not ctx or not unit or ctx[0] != (symbol,) or ctx[2]:
            continue
        ds = dict(ctx[1])
        name = a.get('name', '').split(':')[-1]
        if name not in FACT_NAMES:
            continue
        if name == 'BasicEarningsLossPerShare':
            if unit != (('iso4217:twd', 'xbrli:shares'), True, True, True):
                continue
        elif unit[:2] != (('iso4217:twd',), False):
            continue
        if ds['instant'] == (end,) and not ds['startdate'] and not ds['enddate']:
            kind = 'instant'
        elif ds['startdate'] == (f'{y}-01-01',) and ds['enddate'] == (end,) and not ds['instant']:
            kind = 'current'
        elif ds['startdate'] == (f'{y-1}-01-01',) and ds['enddate'] == (prior_end,) and not ds['instant']:
            kind = 'prior'
        else:
            continue
        value = numeric(a, content)
        # A nil/invalid duplicate must not be silently discarded as harmless.
        values.setdefault((name, kind), set()).add(value)

    def unique(name, kind):
        found = values.get((name, kind), set())
        return next(iter(found)) if len(found) == 1 and None not in found else None

    profit = 'ProfitLossAttributableToOwnersOfParent' if scope == 'cr' else 'ProfitLoss'
    equity = 'EquityAttributableToOwnersOfParent' if scope == 'cr' else 'Equity'
    return dict(symbol=symbol, period=period, scope=scope, end=end,
                eps=unique('BasicEarningsLossPerShare', 'current'),
                eps_prior=unique('BasicEarningsLossPerShare', 'prior'),
                profit=unique(profit, 'current'), profit_prior=unique(profit, 'prior'),
                equity=unique(equity, 'instant'))


def release_date(period, extra=0):
    year, quarter = int(period[:4]), int(period[-1])
    end = date(year, quarter*3, calendar.monthrange(year, quarter*3)[1])
    return (end + timedelta(days=(120 if quarter == 4 else 60) + extra)).isoformat()


def month_shift(month, shift):
    n = int(month[:4])*12 + int(month[5:7])-1 + shift
    return f'{n//12:04d}-{n%12+1:02d}'


def revenue_release(month, extra=0):
    return (date.fromisoformat(month_shift(month, 1)+'-15') + timedelta(days=extra)).isoformat()


def accounting(symbol, day, facts, extra=0):
    year = int(day[:4])
    available = [f'{y}Q{q}' for y in (year-1, year) for q in range(1, 5)
                 if release_date(f'{y}Q{q}', extra) <= day]
    if not available:
        return None
    period = max(available)
    current = facts.get((symbol, period))
    if current is None:
        return None
    if period.endswith('Q4'):
        eps, profit = current['eps'], current['profit']
    else:
        annual_period = f'{int(period[:4])-1}Q4'
        annual = facts.get((symbol, annual_period))
        if not annual or annual['scope'] != current['scope'] or release_date(annual_period, extra) > day:
            return None
        eps_values = (current['eps'], annual['eps'], current['eps_prior'])
        profit_values = (current['profit'], annual['profit'], current['profit_prior'])
        eps = eps_values[0]+eps_values[1]-eps_values[2] if all(finite(x) for x in eps_values) else None
        profit = profit_values[0]+profit_values[1]-profit_values[2] if all(finite(x) for x in profit_values) else None
    equity = current['equity']
    roe = profit/equity if finite(profit) and finite(equity) and equity > 0 else None
    return dict(period=period, scope=current['scope'], eps_ttm=eps, roe_ttm=roe)


def revenue_surprise(symbol, day, revenues, extra=0):
    # Select the latest scheduled month, not the latest month that happens to
    # have data: a missing recent release must not fall back to stale good news.
    month = month_shift(day[:7], -1)
    while revenue_release(month, extra) > day:
        month = month_shift(month, -1)
    pairs = [revenues.get((symbol, month_shift(month, -j))) for j in range(4)]
    if any(p is None or not all(finite(x) for x in p) or p[0] < 0 or p[1] <= 0 for p in pairs):
        return None
    growth = [a/b-1 for a, b in pairs]
    forecast_growth = st.mean(growth[1:])
    return dict(month=month, surprise=growth[0]-forecast_growth,
                actual_yoy=growth[0], model_forecast_yoy=forecast_growth,
                assumed_release=revenue_release(month, extra))


def momentum(bars):
    # 21 sessions/month; P(t-21)/P(t-147) and P(t-21)/P(t-273).
    if len(bars) < 148 or any(p is None or p.get('corporate_action_unresolved') for p in bars[-148:]):
        return None
    closes = [p['close'] if p else None for p in bars]
    if any(not finite(x) or x <= 0 for x in closes[-148:]):
        return None
    returns = [b/a-1 for a, b in zip(closes[-127:-1], closes[-126:])]
    vol = st.pstdev(returns)*math.sqrt(252)
    if vol <= 0:
        return None
    m6 = closes[-22]/closes[-148]-1
    m12 = None
    if len(bars) >= 274 and all(p is not None and not p.get('corporate_action_unresolved') for p in bars[-274:]):
        m12 = closes[-22]/closes[-274]-1
    return dict(m6=m6/vol, m12=m12/vol if m12 is not None else None,
                volatility=vol, uses_12m=m12 is not None)


def ranks(rows, key):
    if any(not finite(r.get(key)) for r in rows):
        raise ValueError('Unknown required feature cannot become a score')
    values = sorted(r[key] for r in rows)
    return {r['symbol']: ((bisect.bisect_left(values, r[key])+bisect.bisect_right(values, r[key])-1)/2)/(len(values)-1)
            if len(values) > 1 else .5 for r in rows}


def choose(rows, limit=20):
    """All four candidates use one feature-complete pool, average tied ranks."""
    if len({r['symbol'] for r in rows}) != len(rows):
        raise ValueError('Duplicate symbols')
    m6, roe, ep, surprise = (ranks(rows, key) for key in ('m6', 'roe', 'ep', 'surprise'))
    twelve = [r for r in rows if finite(r.get('m12'))]
    m12 = ranks(twelve, 'm12')
    mom = {r['symbol']: (m6[r['symbol']]+m12[r['symbol']])/2 if r['symbol'] in m12 else m6[r['symbol']] for r in rows}
    scores = {'momentum': mom,
              'quality_momentum': {s: (mom[s]+roe[s])/2 for s in mom},
              'value_momentum': {s: (mom[s]+ep[s])/2 for s in mom},
              'revenue_surprise_proxy': surprise}
    out = {}
    for variant, by_symbol in scores.items():
        picked = sorted(rows, key=lambda r: (-by_symbol[r['symbol']], r['symbol']))[:limit]
        out[variant] = [dict(symbol=r['symbol'], market=r['market'], score=by_symbol[r['symbol']],
                             weight=1/len(picked), revenue_month=r['revenue_month']) for r in picked]
    out['common_equal_weight'] = [dict(symbol=r['symbol'], market=r['market'], weight=1/len(rows)) for r in rows]
    return out


def cohort(sessions, index, picks, prices, horizon, bps):
    if index+horizon >= len(sessions):
        return dict(status='IMMATURE', value=None)
    if not picks:
        return dict(status='CASH', value=0., stock_returns=[])
    values, weights = [], []
    for row in picks:
        bars = [prices.get(row['symbol'], {}).get(d) for d in sessions[index+1:index+horizon+1]]
        if any(p is None for p in bars):
            return dict(status='MISSING_PRICE', value=None, symbol=row['symbol'])
        if any(p.get('corporate_action_unresolved') for p in bars[1:]):
            return dict(status='CORPORATE_ACTION_UNRESOLVED', value=None, symbol=row['symbol'])
        rate = bps/20000
        values.append(bars[-1]['close']*(1-rate)/(bars[0]['open']*(1+rate))-1)
        weights.append(row.get('weight', 1/len(picks)))
    return dict(status='KNOWN', value=sum(v*w for v, w in zip(values, weights)), stock_returns=values)
