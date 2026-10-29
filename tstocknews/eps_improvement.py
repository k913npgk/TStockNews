"""Current observed MOPS filings for the C5-eligible pool, before Top20."""
from copy import deepcopy
from datetime import datetime
from calendar import monthrange
import hashlib
import io
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile, BadZipFile

from .engine import screen, MIN_HISTORY_SESSIONS, MAX_CANDIDATES, _float
from .official import SourceError, today, TAIPEI
from .network import tls_context
from .storage import read,write,digest

SCHEMA='same-filing-ytd-eps-v1'
MAX_ARCHIVE_BYTES=200*1024*1024


def download_archive(url):
    # One normal official request, bounded size/time; no alternate endpoint or retries.
    try:
        started=time.monotonic();blocks=[];size=0
        with urlopen(Request(url,headers={'User-Agent':'TStockNews/0.1'}),
                     timeout=20,context=tls_context()) as response:
            while True:
                if time.monotonic()-started>120:raise SourceError('EPS archive exceeded wall-clock limit')
                block=response.read(1024*1024)
                if not block:break
                size+=len(block)
                if size>MAX_ARCHIVE_BYTES:raise SourceError('EPS archive exceeded size limit')
                blocks.append(block)
            raw=b''.join(blocks)
    except Exception as exc:
        raise SourceError('MOPS EPS archive request failed') from exc
    if len(raw)>MAX_ARCHIVE_BYTES or not raw.startswith(b'PK'):
        raise SourceError('MOPS EPS archive is oversized or not a ZIP')
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
    from .eps_facts import extract_comparison
    for period,companies in sorted(groups.items()):
        year,quarter=int(period[:4]),int(period[-1])
        url='https://mopsov.twse.com.tw/server-java/FileDownLoad?'+urlencode({
            'step':9,'functionName':'show_file2','fileName':f'tifrs-{year}Q{quarter}.zip','filePath':f'/ifrs/{year}/'})
        raw=download_archive(url);archive_hash=hashlib.sha256(raw).hexdigest()
        wanted={r['symbol'] for r in companies};parsed={}
        try:
            with ZipFile(io.BytesIO(raw)) as z:
                for name in z.namelist():
                    m=re.search(r'-(cr|ir)-(\d{4,6})-'+str(year)+'Q'+str(quarter)+r'\.html$',name)
                    if not m or m[2] not in wanted:continue
                    info=z.getinfo(name)
                    if info.file_size>16*1024*1024:raise SourceError('EPS filing exceeds size bound')
                    body=z.read(name)
                    fact=extract_comparison(body,symbol=m[2],year=year,quarter=quarter,member_name=name)
                    fact.update(source_url=url,source_member=name,archive_sha256=archive_hash,observed_at=datetime.now(TAIPEI).isoformat())
                    parsed.setdefault(m[2],[]).append(fact)
                    artifact=root/'eps-filings'/period/(f'{year}Q{quarter}-{m[1]}-{m[2]}-'+hashlib.sha256(body).hexdigest()+'.json.gz')
                    saved=read(artifact)
                    if saved is None:
                        write(artifact,{'member_name':name,'source_url':url,'archive_sha256':archive_hash,
                            'observed_at':fact['observed_at'],'html':body.decode('utf-8-sig')})
                    elif saved.get('member_name')!=name or hashlib.sha256(saved.get('html','').encode('utf-8')).hexdigest()!=hashlib.sha256(body.decode('utf-8-sig').encode('utf-8')).hexdigest():
                        raise SourceError('Saved EPS filing content changed')
        except (BadZipFile,UnicodeError,ValueError) as exc:
            raise SourceError('Invalid MOPS EPS filing archive') from exc
        for company in companies:
            options=parsed.get(company['symbol'],[])
            selected=[f for f in options if f.get('statement_scope')=='consolidated'] or options
            target=funds[company['market'],company['symbol']]
            target['eps_comparison_status']='UNKNOWN'
            if selected and all(f.get('status')=='KNOWN' for f in selected) and len({
                (f['eps_ytd'],f['eps_prior_ytd'],f['statement_scope']) for f in selected})==1:
                fact=selected[0]
                if fact['eps_ytd']==_float(target.get('eps')):
                    target.update(eps_comparison_status='KNOWN',eps_prior_ytd=fact['eps_prior_ytd'],
                                  eps_comparison=fact,eps_comparison_observed_date=observed)
                else:target['eps_comparison_status']='CURRENT_VALUE_MISMATCH'
        audit.append({'fiscal_period':period,'url':url,'sha256':archive_hash,'bytes':len(raw)})
    if pool and not any(funds[r['market'],r['symbol']].get('eps_comparison_status')=='KNOWN' for r in pool):
        raise SourceError('No C5-eligible company has a verified EPS comparison')
    enriched.update(eps_comparison_schema_version=SCHEMA,eps_input_hash=key,eps_source_audit=audit,eps_target_date=day)
    write(path,enriched)
    return enriched


def apply(result,financial,cutoff):
    if result['candidates'] and financial.get('eps_comparison_schema_version')!=SCHEMA:
        raise SourceError('EPS comparison snapshot is required for v7 recommendations')
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
    result['candidates']=selected[:MAX_CANDIDATES]
    result['diagnostics']['eps_rule']='same-scope YTD EPS > prior-year same-period YTD EPS'
    return result
