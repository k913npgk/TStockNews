"""Current observed MOPS filings for the C5-eligible pool, before Top20."""
from copy import deepcopy
from datetime import datetime
from calendar import monthrange
import hashlib
import base64
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .engine import screen, MIN_HISTORY_SESSIONS, MAX_CANDIDATES, _float
from .official import SourceError, today, TAIPEI
from .network import tls_context
from .storage import read,write,digest

SCHEMA='same-filing-ytd-eps-v1'
MAX_FILING_BYTES=16*1024*1024


def download_filing(url):
    """One ordinary official document request; sequential, no retry or alternate URL."""
    try:
        started=time.monotonic();blocks=[];size=0
        with urlopen(Request(url,headers={'User-Agent':'TStockNews/0.1'}),
                     timeout=45,context=tls_context()) as response:
            while True:
                if time.monotonic()-started>60:raise SourceError('EPS document exceeded wall-clock limit')
                block=response.read(64*1024)
                if not block:break
                size+=len(block)
                if size>MAX_FILING_BYTES:raise SourceError('EPS document exceeded size limit')
                blocks.append(block)
            raw=b''.join(blocks)
    except SourceError:
        raise
    except Exception as exc:
        code=getattr(exc,'code',None)
        detail=f'HTTP {code}' if code is not None else type(exc).__name__
        raise SourceError(f'MOPS EPS document request failed ({detail})') from exc
    if not raw or len(raw)>MAX_FILING_BYTES:raise SourceError('Empty or oversized EPS document')
    time.sleep(.3)
    return raw


def full_c5(day,sessions,prices,institutions,financial):
    used=sorted({d for d in sessions if d<=day})[-MIN_HISTORY_SESSIONS:]
    return screen(day,used,financial['universe'],[r for r in prices if r['date'] in used],
                  financial['fundamentals'],[r for r in institutions if r['date'] in used],
                  fundamental_as_of=max(day,financial.get('observed_date',day)),candidate_limit=None)


def prepare(root,day,sessions,prices,institutions,financial):
    """Observe comparisons only for the complete C5 pool; preserve original snapshots."""
    pool=full_c5(day,sessions,prices,institutions,financial)['candidates']
    if financial.get('eps_comparison_schema_version')==SCHEMA and financial.get('eps_target_date')==day:
        return financial
    key=digest({'financial':financial,'pool':[(r['market'],r['symbol'],r['fiscal_period']) for r in pool]})
    observed=financial.get('observed_date',day)
    path=root/'financials'/f'{observed}-{day}-eps.json.gz'
    cached=read(path)
    if cached is not None:
        if cached.get('eps_input_hash')!=key or cached.get('eps_comparison_schema_version')!=SCHEMA:
            raise SourceError('Saved EPS comparison snapshot does not match inputs')
        return cached
    if observed!=today().isoformat():
        raise SourceError('Historical snapshot lacks observed EPS comparisons; cannot backfill them as historical knowledge')
    enriched=deepcopy(financial);funds={(r['market'],r['symbol']):r for r in enriched['fundamentals']}
    groups={}
    for r in pool:
        if not re.fullmatch(r'\d{4}-Q[1-4]',r.get('fiscal_period','')):
            raise SourceError('Invalid EPS fiscal period')
        groups.setdefault(r['fiscal_period'],[]).append(r)
    audit=[]
    from .eps_facts import extract_comparison,identify_filing
    for period,companies in sorted(groups.items()):
        year,quarter=int(period[:4]),int(period[-1])
        for company in sorted(companies,key=lambda r:(r['market'],r['symbol'])):
            symbol=company['symbol']
            url='https://mopsov.twse.com.tw/server-java/t164sb01?'+urlencode({
                'step':1,'CO_ID':symbol,'SYEAR':year,'SSEASON':quarter,'REPORT_ID':'C'})
            try:raw=download_filing(url)
            except SourceError as exc:
                raise SourceError(f'EPS source for {symbol} {period}: {exc}') from exc
            body_hash=hashlib.sha256(raw).hexdigest()
            identity=identify_filing(raw)
            if not identity or (identity['symbol'],identity['year'],identity['quarter'])!=(symbol,year,quarter):
                raise SourceError('Official EPS document identity/period is missing or differs from request')
            scope=identity['statement_scope'];scope_code='cr' if scope=='consolidated' else 'ir'
            name=f'filing-{scope_code}-{symbol}-{year}Q{quarter}.html'
            fact=extract_comparison(raw,symbol=symbol,year=year,quarter=quarter,member_name=name)
            fact.update(source_url=url,source_identity=identity,source_member_sha256=body_hash,
                        observed_at=datetime.now(TAIPEI).isoformat())
            artifact=root/'eps-filings'/period/(f'{scope_code}-{symbol}-'+body_hash+'.json.gz')
            saved=read(artifact)
            if saved is None:
                write(artifact,{'identity':identity,'source_url':url,'sha256':body_hash,
                    'observed_at':fact['observed_at'],'body_base64':base64.b64encode(raw).decode('ascii')})
            elif saved.get('sha256')!=body_hash or hashlib.sha256(base64.b64decode(saved.get('body_base64',''))).hexdigest()!=body_hash:
                raise SourceError('Saved EPS filing content changed')
            target=funds[company['market'],symbol];target['eps_comparison_status']='UNKNOWN'
            if fact['status']=='KNOWN':
                if fact['eps_ytd']==_float(target.get('eps')):
                    target.update(eps_comparison_status='KNOWN',eps_prior_ytd=fact['eps_prior_ytd'],
                                  eps_comparison=fact,eps_comparison_observed_date=observed)
                else:target['eps_comparison_status']='CURRENT_VALUE_MISMATCH'
            audit.append({'symbol':symbol,'market':company['market'],'fiscal_period':period,
                          'url':url,'sha256':body_hash,'bytes':len(raw)})
    if observed!=today().isoformat():
        raise SourceError('EPS observation crossed the financial snapshot date')
    if pool and not any(funds[r['market'],r['symbol']].get('eps_comparison_status')=='KNOWN' for r in pool):
        raise SourceError('No C5-eligible company has a verified EPS comparison')
    enriched.update(eps_comparison_schema_version=SCHEMA,eps_input_hash=key,eps_source_audit=audit,eps_target_date=day)
    write(path,enriched)
    return enriched


def apply(result,financial,cutoff,*,candidate_limit=MAX_CANDIDATES):
    if result['candidates'] and financial.get('eps_comparison_schema_version')!=SCHEMA:
        raise SourceError('EPS comparison snapshot is required for recommendations')
    funds={(r['market'],r['symbol']):r for r in financial['fundamentals']}
    selected=[];counts=result['diagnostics']['exclusion_counts']
    for row in result['candidates']:
        f=funds[row['market'],row['symbol']];prior=_float(f.get('eps_prior_ytd'))
        fact=f.get('eps_comparison',{});observed=f.get('eps_comparison_observed_date','')
        period=row.get('fiscal_period','')
        dates_ok=False
        if re.fullmatch(r'\d{4}-Q[1-4]',period):
            year,month=int(period[:4]),int(period[-1])*3
            dates_ok=(fact.get('period_start')==f'{year}-01-01'
                and fact.get('prior_period_start')==f'{year-1}-01-01'
                and fact.get('period_end')==f'{year}-{month:02d}-{monthrange(year,month)[1]}'
                and fact.get('prior_period_end')==f'{year-1}-{month:02d}-{monthrange(year-1,month)[1]}')
        known=(f.get('eps_comparison_status')=='KNOWN' and prior is not None and observed and observed<=cutoff
               and dates_ok and fact.get('status')=='KNOWN'
               and fact.get('eps_ytd')==row['eps'] and fact.get('eps_prior_ytd')==prior
               and fact.get('statement_scope') in ('consolidated','individual'))
        reason='EPS_COMPARISON_UNKNOWN' if not known else ('EPS_NOT_IMPROVED_YOY' if row['eps']<=prior else None)
        diag=result['diagnostics']['symbols'][row['market']+':'+row['symbol']]
        diag.update(eps_prior_ytd=prior,eps_comparison_status=f.get('eps_comparison_status','UNKNOWN'))
        if reason:
            diag['reasons'].append(reason);counts[reason]=counts.get(reason,0)+1
        else:
            selected.append({**row,'eps_prior_ytd':prior,'eps_ytd_yoy_delta':row['eps']-prior,
                             'eps_comparison':fact,'eps_comparison_observed_date':observed})
    result['eligible_count']=result['diagnostics']['eligible_count']=len(selected)
    result['candidates']=selected if candidate_limit is None else selected[:candidate_limit]
    result['diagnostics']['eps_rule']='same-scope YTD EPS > prior-year same-period YTD EPS'
    return result
