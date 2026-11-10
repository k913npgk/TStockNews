"""Forward-only quality-strategy statistics, isolated from formal performance."""
from collections import Counter
from statistics import mean, median

from .engine import HORIZONS, track
from .storage import digest, read


def calculate(history, sessions, prices, day, version):
    observations = []
    signals = []
    seen = set()
    for snapshot in sorted(history, key=lambda s: s['date']):
        if (snapshot['date'] > day or snapshot.get('version') != version
                or snapshot.get('retrospective') is not False):
            continue
        if not snapshot.get('test_only') or not snapshot.get('excluded_from_performance'):
            raise ValueError('Quality performance requires isolated test snapshots')
        if snapshot['date'] in seen:
            raise ValueError('Duplicate quality observation date')
        seen.add(snapshot['date'])
        observations.append(snapshot)
        identities = set()
        for member in snapshot['members']:
            key = (member['market'], member['symbol'])
            if key in identities:
                raise ValueError('Duplicate quality member')
            identities.add(key)
            # Project only calculation inputs into the shared numerical tracker.
            # Persisted snapshots retain their exclusion flags; every output row
            # is tagged again before leaving this isolated calculation adapter.
            signals.append(dict(date=snapshot['date'], market=key[0], symbol=key[1],
                                signal_id=f"test:quality-performance:{version}:{snapshot['date']}:{key[0]}:{key[1]}"))
    rows = track(signals, [s for s in sessions if s <= day],
                 [p for p in prices if p['date'] <= day], day)
    rows = [dict(r, test_only=True, excluded_from_performance=True,
                 strategy_version=version) for r in rows]
    return dict(schema='quality-forward-performance-v1', date=day, version=version,
                test_only=True, excluded_from_performance=True,
                start_date=observations[0]['date'] if observations else None,
                observation_days=len(observations), signal_count=len(signals), rows=rows,
                observation_hashes={s['date']: digest(s) for s in observations})


def build(source, root, day, version):
    history = []
    for path in sorted((root/'snapshots').glob('*.json')):
        if path.stem > day:
            continue
        snapshot = read(path)
        if snapshot.get('date') != path.stem:
            raise ValueError('Quality observation filename/date mismatch')
        history.append(snapshot)
    sessions, prices, hashes = [], [], {}
    for path in sorted((source/'days').glob('*.json.gz')):
        expected = path.name.removesuffix('.json.gz')
        if expected > day:
            continue
        market = read(path)
        if market.get('date') != expected or any(p.get('date') != expected for p in market['prices']):
            raise ValueError('Quality performance market filename/date mismatch')
        sessions.append(expected)
        prices.extend(market['prices'])
        hashes[expected] = digest(market)
    if day not in sessions:
        raise ValueError('Quality performance requires target market session')
    result = calculate(history, sessions, prices, day, version)
    result['market_hashes'] = hashes
    return result


def render(performance):
    lines = ['', '📈 測試策略績效統計',
             f"追蹤起日：{performance['start_date'] or '—'}｜入選紀錄：{performance['signal_count']}筆", '']
    for horizon in HORIZONS:
        rows = [r for r in performance['rows'] if r['horizon'] == horizon]
        returns = [r['price_return'] for r in rows if r['status'] == 'MATURE'
                   and r.get('price_return') is not None]
        statuses = Counter(r['status'] for r in rows)
        pending = statuses['PENDING_ENTRY'] + statuses['NOT_MATURE']
        unknown = len(rows)-len(returns)-pending
        lines.append(f'持有{horizon}個交易日')
        if returns:
            lines.append(f"平均：{mean(returns):+.2%}｜中位數：{median(returns):+.2%}｜上漲比例：{sum(r > 0 for r in returns)/len(returns):.1%}")
        else:
            lines.append('平均：—｜中位數：—｜上漲比例：—')
        lines += [f'有效：{len(returns)}／{len(rows)}筆｜待追蹤：{pending}筆｜資料待確認：{unknown}筆', '']
    lines += ['入選後下一交易日開盤起算，未扣費用、未計股息。',
              '同股重複入選逐筆統計，非投資組合報酬。']
    return lines
