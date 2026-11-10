"""Isolated daily quality/momentum comparison and explicit historical resend."""
import argparse
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha256
from pathlib import Path
import re
from statistics import median
from tempfile import TemporaryFile
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile

from . import STRATEGY_VERSION
from . import quality_factors as factors
from . import quality_performance
from .cli import push
from .engine import _valid_price
from .line import prepare
from .list_shadow import identity, index, official_reference
from .network import tls_context
from .official import today
from .storage import digest, read, write

VERSION = 'test-quality-momentum-top20-v1'
TEST_ID = 'quality-momentum-v1'
CACHE_SCHEMA = 'quality-accounting-facts-v1'
MAX_ARCHIVE_BYTES = 256*1024*1024
MEMBER = re.compile(r'-(cr|ir)-(\d{4})-(\d{4}Q[1-4])\.html$', re.I)


def market_features(snapshots, universe):
    if len(snapshots) < 148:
        raise ValueError('Quality momentum needs at least148 market sessions')
    snapshots = snapshots[-274:]
    dates = [r['date'] for r in snapshots]
    if dates != sorted(set(dates)):
        raise ValueError('Market dates must be unique and ordered')
    prices = defaultdict(dict)
    for snapshot in snapshots:
        for key, row in index(snapshot['prices'], identity).items():
            if row.get('date') != snapshot['date']:
                raise ValueError('Price observation date mismatch')
            if _valid_price(row):
                prices[key][snapshot['date']] = row
    result = []
    for key, company in index(universe, identity).items():
        bars = [prices[key].get(d) for d in dates]
        if any(p is None for p in bars[-20:]):
            continue
        liquidity = median(p['close']*p['volume_shares'] for p in bars[-20:])
        if liquidity < 30_000_000:
            continue
        values = factors.momentum(bars)
        if values is None:
            continue
        result.append(dict(company, **values, liquidity=liquidity, close=bars[-1]['close'],
                           price_change_pct=bars[-1].get('price_change_pct')))
    return result


def required_periods(day):
    year = int(day[:4])
    periods = [f'{y}Q{q}' for y in (year-1,year) for q in range(1,5)
               if factors.release_date(f'{y}Q{q}') <= day]
    current = max(periods)
    return (current,) if current.endswith('Q4') else (f'{int(current[:4])-1}Q4', current)


def fetch_period(period, day):
    year = int(period[:4])
    url = 'https://mopsov.twse.com.tw/server-java/FileDownLoad?'+urlencode({
        'step':9,'functionName':'show_file2','fileName':f'tifrs-{period}.zip','filePath':f'/ifrs/{year}/'})
    checksum = sha256()
    with TemporaryFile() as local:
        size = 0
        with urlopen(Request(url, headers={'User-Agent':'TStockNews/0.1'}), timeout=45, context=tls_context()) as response:
            while block := response.read(1024*1024):
                size += len(block)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError('Quality filing archive exceeds size bound')
                checksum.update(block)
                local.write(block)
        local.seek(0)
        with ZipFile(local) as archive:
            grouped = defaultdict(list)
            for member in archive.infolist():
                match = MEMBER.search(member.filename)
                if match and match[3].upper() == period:
                    grouped[match[2]].append((match[1].lower(),member))
            rows = []
            for symbol, members in sorted(grouped.items()):
                scope = 'cr' if any(s == 'cr' for s,_ in members) else 'ir'
                parsed = []
                for kind, member in members:
                    if kind != scope:
                        continue
                    if member.file_size > 16*1024*1024:
                        raise ValueError('Quality filing member exceeds size bound')
                    parsed.append(factors.extract_filing(archive.read(member),symbol,period,scope))
                if parsed:
                    row = dict(parsed[0])
                    for field in ('eps','eps_prior','profit','profit_prior','equity'):
                        values = {r[field] for r in parsed}
                        row[field] = next(iter(values)) if len(values) == 1 else None
                    rows.append(row)
    if not rows or not any(factors.finite(r['profit']) and factors.finite(r['equity']) for r in rows):
        raise ValueError('No usable quality accounting facts in official archive')
    return dict(schema=CACHE_SCHEMA,period=period,observed_date=day,rows=rows,
                source=dict(url=url,sha256=checksum.hexdigest(),size=size))


def load_accounting(period, root, day):
    path = root/'accounting'/f'{period}.json.gz'
    cache = read(path)
    if cache is None:
        cache = fetch_period(period,day)
        write(path,cache)
    if cache.get('schema') != CACHE_SCHEMA or cache.get('period') != period:
        raise ValueError('Quality accounting cache identity mismatch')
    if not cache.get('observed_date') or cache['observed_date'] > day:
        raise ValueError('Cannot use later-observed accounting facts for live history')
    if any(r.get('period') != period for r in cache['rows']) or len({r['symbol'] for r in cache['rows']}) != len(cache['rows']):
        raise ValueError('Duplicate or mismatched accounting cache facts')
    return cache


def rank_members(rows, financial, day, facts):
    revenue = {}
    for fund in financial['fundamentals']:
        for record in fund.get('revenue_months',[]):
            key = (fund['symbol'],record['month'])
            pair = (record.get('current'),record.get('prior_year'))
            if key in revenue and revenue[key] != pair:
                raise ValueError('Conflicting monthly comparison in financial snapshot')
            revenue[key] = pair
    pool, coverage = [], Counter(price_feature_pool=len(rows))
    for row in rows:
        symbol = row['symbol']
        accounting = factors.accounting(symbol,day,facts)
        surprise = factors.revenue_surprise(symbol,day,revenue)
        if not accounting or not all(factors.finite(accounting.get(k)) for k in ('eps_ttm','roe_ttm')):
            coverage['unknown_accounting'] += 1
            continue
        if surprise is None:
            coverage['unknown_monthly_model'] += 1
            continue
        pool.append(dict(row,roe=accounting['roe_ttm'],ep=accounting['eps_ttm']/row['close'],
                         surprise=surprise['surprise'],revenue_month=surprise['month'],fiscal_period=accounting['period']))
    if not pool:
        raise ValueError('No feature-complete quality momentum pool')
    picks = factors.choose(pool)['quality_momentum']
    selected = {r['symbol']:r for r in pool}
    members = [dict(selected[r['symbol']],score=r['score'],weight=r['weight']) for r in picks]
    coverage['pool_size'] = len(pool)
    coverage['uses_12m_count'] = sum(r['uses_12m'] for r in pool)
    return members,dict(coverage)


def next_close_rises(rows, quotes):
    up = known = 0
    for row in rows:
        quote = quotes.get(identity(row))
        if not factors.finite(row.get('close')) or row['close'] <= 0 or not quote or not _valid_price(quote):
            continue
        known += 1
        up += quote['close'] > row['close']
    return dict(up=up,known=known,total=len(rows))


def render(snapshot, previous, performance=None):
    retrospective = snapshot.get('retrospective',False)
    continuous = (snapshot.get('continuity') == 'CONTINUOUS'
                  and previous.get('version') == VERSION
                  and previous.get('date', '') < snapshot['date']
                  and not previous.get('retrospective') and not retrospective)
    prior = {identity(r):r for r in previous.get('members',[])} if continuous else {}
    prior_ranks = {key:rank for rank,key in enumerate(prior,1)}
    members = snapshot['members']
    name = lambda r:f"{r.get('name','')}（{r['symbol']}）"
    title = '🧪 測試策略更正版' if retrospective else '🧪 測試中策略'
    lines = [f"{title}｜{snapshot['date']}",'品質＋動能｜每日排名前20檔','']
    if retrospective:
        lines += ['本次為事後補發，使用11/9收盤行情與已保存研究規則。','不納入當日前瞻觀察紀錄。','']
    lines.append('排名｜股票｜較上次排名變動')
    for rank,row in enumerate(members,1):
        previous_rank = prior_ranks.get(identity(row))
        if not continuous:
            change = '—'
        elif previous_rank is None:
            change = '新增'
        else:
            delta = previous_rank-rank
            change = f"↑ {delta}名" if delta > 0 else f"↓ {-delta}名" if delta < 0 else '持平'
        lines.append(f"{rank}｜{name(row)}｜{change}")
    if not members:
        lines.append('空榜')
    current = {identity(r) for r in members}
    dropped = '、'.join(name(r) for k,r in prior.items() if k not in current) or '0檔'
    lines += ['', '落榜 '+(dropped if continuous else '—')]
    if continuous:
        lines += ['', '前一交易日榜單｜今日收盤較前日收盤']
        for label,value in snapshot.get('outcomes',{}).items():
            if label == '品質＋動能':
                lines.append(f"{label}：{value['up']}／{value['known']}檔上漲，價格完整{value['known']}／{value['total']}檔")
    if performance is not None:
        if retrospective or performance.get('date') != snapshot['date'] or performance.get('version') != VERSION:
            raise ValueError('Quality performance/report identity mismatch')
        lines += quality_performance.render(performance)
    return '\n'.join(lines)+'\n'


def save_report(root, day, snapshot, previous, performance=None):
    report = root/'reports'/f'{day}.md'
    content = render(snapshot,previous,performance)
    report.parent.mkdir(parents=True,exist_ok=True)
    if report.exists() and report.read_text(encoding='utf-8') != content:
        raise ValueError('Saved quality report changed')
    if performance is not None:
        path = root/'performance'/f'{day}.json'
        saved = read(path)
        if saved is not None and saved != performance:
            raise ValueError('Saved quality performance changed')
        if saved is None:
            write(path,performance)
    if not report.exists():
        report.write_text(content,encoding='utf-8')
    delivery = root/'data/delivery'/f'{day}.json'
    receipt = read(delivery)
    if receipt is None:
        write(delivery,prepare(digest(content)))
    elif receipt['payload_hash'] != digest(content):
        raise ValueError('Saved quality payload changed')


def capture(source, root, day, load_period=None):
    if date.fromisoformat(day) != today():
        raise ValueError('Live quality capture requires current Taipei date')
    formal = official_reference(source,day)
    if formal.get('strategy_version') != STRATEGY_VERSION or formal.get('as_of',formal.get('date')) != day or formal.get('test_only'):
        raise ValueError('Same-day formal v8 recommendation required')
    dest = root/'snapshots'/f'{day}.json'
    previous_paths = sorted(p for p in (root/'snapshots').glob('*.json') if p.stem < day)
    previous = read(previous_paths[-1],{}) if previous_paths else {}
    if previous.get('version') != VERSION or previous.get('retrospective'):
        previous = {}
    snapshot = read(dest)
    if snapshot is None:
        if any(p.stem > day for p in (root/'snapshots').glob('*.json')):
            raise ValueError('Cannot insert live observations into existing later history')
        financial = read(source/'financials'/f'{day}.json.gz')
        if not financial or financial.get('observed_date') != day:
            raise ValueError('Same-day financial snapshot required')
        used = sorted(p for p in (source/'days').glob('*.json.gz') if p.name[:10] <= day)[-274:]
        if len(used) < 148 or used[-1].name[:10] != day:
            raise ValueError('At least148 market snapshots ending on target day required')
        snapshots = [read(p) for p in used]
        if [s['date'] for s in snapshots] != [p.name[:10] for p in used]:
            raise ValueError('Market filename/date mismatch')
        for s in snapshots:
            if {r['market'] for r in s['prices']} != {'twse','tpex'}:
                raise ValueError('Both official markets required')
        loader = load_period or load_accounting
        caches = [loader(period,root,day) for period in required_periods(day)]
        facts = {(r['symbol'],r['period']):r for cache in caches for r in cache['rows']}
        members,coverage = rank_members(market_features(snapshots,financial['universe']),financial,day,facts)
        continuity = 'INITIAL' if not previous else 'CONTINUOUS' if previous['date'] == snapshots[-2]['date'] else 'RESET_AFTER_GAP'
        quotes = index(snapshots[-1]['prices'],identity)
        formal_members = [dict(r,close=quotes.get(identity(r),{}).get('close')) for r in formal['candidates']]
        outcomes = {label:next_close_rises(previous[key],quotes) for label,key in (('正式v8','formal'),('品質＋動能','members'))} if continuity == 'CONTINUOUS' else {}
        if date.fromisoformat(day) != today():
            raise ValueError('Capture crossed the intended Taipei session date')
        snapshot = dict(date=day,version=VERSION,test_only=True,excluded_from_performance=True,
                        retrospective=False,members=members,formal=formal_members,formal_hash=digest(formal),
                        previous_hash=digest(previous),continuity=continuity,outcomes=outcomes,
                        fiscal_periods=sorted({r['fiscal_period'] for r in members}),
                        revenue_months=sorted({r['revenue_month'] for r in members}),**coverage,
                        market_sha256={p.name:sha256(p.read_bytes()).hexdigest() for p in used},
                        financial_hash=digest(financial),accounting_sources=[dict(period=c['period'],observed_date=c['observed_date'],source=c['source']) for c in caches])
        write(dest,snapshot)
    if snapshot.get('version') != VERSION or snapshot.get('retrospective') or snapshot['formal_hash'] != digest(formal) or snapshot['previous_hash'] != digest(previous):
        raise ValueError('Saved live snapshot identity changed')
    performance = quality_performance.build(source,root,day,VERSION)
    save_report(root,day,snapshot,previous,performance)
    return snapshot


def prepare_resend(root, day):
    if day != '2026-11-09':
        raise ValueError('Only explicitly approved Nov9 correction is enabled')
    base = root/'resends'/day
    snapshot = read(base/'snapshot.json')
    if not snapshot or snapshot.get('date') != day or snapshot.get('version') != VERSION:
        raise ValueError('Approved quality correction snapshot required')
    if not all(snapshot.get(k) is True for k in ('retrospective','test_only','excluded_from_performance')):
        raise ValueError('Correction must remain isolated retrospective evidence')
    if len(snapshot.get('members',[])) != 20 or len({identity(r) for r in snapshot['members']}) != 20:
        raise ValueError('Approved correction requires20 unique members')
    save_report(base,day,snapshot,{})
    return snapshot


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=('capture','prepare-resend','send'))
    p.add_argument('--date',required=True)
    p.add_argument('--source',type=Path,default=Path('preview-source/data'))
    p.add_argument('--root',type=Path,default=Path('test-runs')/TEST_ID)
    p.add_argument('--resend',action='store_true')
    args = p.parse_args()
    if args.mode == 'capture':
        if args.resend:
            raise ValueError('Historical resend is not live capture')
        capture(args.source,args.root,args.date)
        result = dict(status='QUALITY_REPORT_READY',date=args.date)
    elif args.mode == 'prepare-resend':
        prepare_resend(args.root,args.date)
        result = dict(status='QUALITY_CORRECTION_READY',date=args.date)
    else:
        base = args.root/'resends'/args.date if args.resend else args.root
        result = push(base/'data',base/'reports',args.date)
    print(__import__('json').dumps(result))
    if args.mode == 'send' and result['status'] != 'SENT':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
