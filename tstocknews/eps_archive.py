"""Restore omitted EPS from dated official snapshots."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import re

from .engine import _float
from .official import SourceError
from .storage import digest, read


def restore_archived_eps(financial, source, target):
    """Fill omitted issuers from dated official archives; never replace a reported value/period."""
    date.fromisoformat(target)
    missing = {(row['market'], row['symbol']) for row in financial['fundamentals']
               if row.get('eps') is None and not row.get('fiscal_period')}
    if not missing:
        return financial
    result = deepcopy(financial)
    known = {}
    for path in sorted((Path(source) / 'financials').glob('*.json.gz')):
        if (not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:-c5)?\.json\.gz', path.name)
                or path.name[:10] > target):
            continue
        saved = read(path)
        observed = saved.get('observed_date')
        if observed != path.name[:10]:
            raise SourceError('Archived EPS snapshot filename/observation date mismatch')
        audit = None
        for row in saved.get('fundamentals', []):
            key = (row['market'], row['symbol'])
            if key not in missing:
                continue
            period = row.get('fiscal_period', '')
            if (_float(row.get('eps')) is None or not re.fullmatch(r'\d{4}-Q[1-4]', period)
                    or not row.get('available_date') or row['available_date'] > observed):
                continue
            rank = (period, observed)
            previous = known.get(key)
            if previous and previous[0] == rank and previous[1]['eps'] != row['eps']:
                raise SourceError('Conflicting archived EPS for the same observation date/period')
            if previous is None or rank > previous[0]:
                if audit is None:
                    audit = {'path': path.name, 'observed_date': observed, 'hash': digest(saved)}
                known[key] = (rank, row, audit)
    restored = []
    for row in result['fundamentals']:
        if row.get('eps') is not None or row.get('fiscal_period'):
            continue
        old = known.get((row['market'], row['symbol']))
        if old is None:
            continue
        _, prior, audit = old
        row.update(eps=prior['eps'], fiscal_period=prior['fiscal_period'],
                   operating_margin=prior.get('operating_margin'),
                   eps_observed_date=audit['observed_date'], eps_source_snapshot=audit)
        restored.append({'market': row['market'], 'symbol': row['symbol'], **audit})
    result['archived_eps_restored'] = restored
    return result
