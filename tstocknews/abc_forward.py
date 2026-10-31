"""Immutable same-day forward research. No production writes or messaging."""
import argparse
from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from . import STRATEGY_VERSION
from .abc_selection import build, broad_c5
from .eps_improvement import prepare as prepare_eps
from .official import TAIPEI, SourceError, today
from .storage import digest, read, write

REPO=Path(__file__).resolve().parents[1]
DECLARATION=REPO/'docs/abc-forward-protocol-v1.json'
CODE=['tstocknews/'+n for n in ('abc_forward.py','abc_selection.py','abc_scoring.py','engine.py',
    'analysis.py','eps_improvement.py','eps_facts.py','monthly_revenue.py','official.py','storage.py','network.py','__init__.py')]


def code_manifest():
    # Canonical source text allows Git LF/CRLF checkout differences, not code changes.
    return {n:hashlib.sha256((REPO/n).read_text(encoding='utf-8').replace('\r\n','\n').encode()).hexdigest() for n in CODE}


def file_hash(p): return hashlib.sha256(p.read_bytes()).hexdigest()


def now(): return datetime.now(timezone.utc)


def register(root):
    root=Path(root)
    declaration=read(DECLARATION)
    if not declaration or declaration.get('code_manifest')!=code_manifest():
        raise ValueError('Unsealed or changed declaration; register a new reviewed version')
    if (root/'registration.json').exists():
        verify(root)
        return status(root)
    if root.exists() and any(root.iterdir()): raise ValueError('Nonempty unregistered directory')
    root.parent.mkdir(parents=True,exist_ok=True)
    staging=root.with_name(root.name+'.initializing-'+uuid.uuid4().hex)
    staging.mkdir()
    write(staging/'registration.json',declaration)
    write(staging/'registration-receipt.json',dict(registration_hash=digest(declaration)))
    staging.rename(root)
    return status(root)


def verify(root):
    root=Path(root); p=read(root/'registration.json')
    if not p or digest(p)!=read(root/'registration-receipt.json',{}).get('registration_hash'):
        raise ValueError('Missing or changed registration')
    if p['code_manifest']!=code_manifest(): raise ValueError('Research code changed; new registration required')
    verified_history={}
    for folder in sorted((root/'snapshots').glob('*')):
        proof=read(folder/'receipt.json')
        if not proof or proof['registration_hash']!=digest(p): raise ValueError('Snapshot registration mismatch')
        for name,h in proof['files'].items():
            if name.startswith('/') or '..' in Path(name).parts: raise ValueError('Unsafe snapshot path')
            if file_hash(folder/name)!=h: raise ValueError('Snapshot input changed: '+name)
        snapshot=read(folder/'snapshot.json.gz')
        if snapshot['date']!=folder.name: raise ValueError('Snapshot date mismatch')
        for record in read(folder/'inputs.json.gz')['market_history']:
            path=root/record['path']
            if '..' in Path(record['path']).parts or not path.resolve().is_relative_to(root.resolve()): raise ValueError('Unsafe history path')
            actual=verified_history.get(path)
            if actual is None:
                actual=file_hash(path); verified_history[path]=actual
            if actual!=record['sha256']: raise ValueError('Sealed market history changed')
    return p


def status(root):
    root=Path(root); folders=sorted((root/'snapshots').glob('*'))
    return dict(status='REGISTERED' if (root/'registration.json').exists() else 'NOT_REGISTERED',
        root=str(root),snapshot_dates=[p.name for p in folders],snapshot_count=len(folders),
        formal_holdout_pass=False,production_version=STRATEGY_VERSION)


def _audit_today(records,day,stamp):
    if not records: raise SourceError('Missing actual source observation timestamps')
    for r in records:
        observed=datetime.fromisoformat(r['fetched_at'])
        if observed.tzinfo is None or observed.astimezone(TAIPEI).date().isoformat()!=day or observed>stamp:
            raise SourceError('Source was not observed on the capture date')


def capture(root,source,day):
    root,source=Path(root),Path(source); protocol=verify(root); stamp=now(); local=stamp.astimezone(TAIPEI)
    if day!=local.date().isoformat(): raise ValueError('Forward capture accepts today only; no historical backfill')
    if date.fromisoformat(day).weekday()>=5: return dict(status='NON_TRADING_DAY',date=day)
    if local.time()<time(21,7): raise ValueError('Capture window begins at 21:07 Taipei')
    if datetime.fromisoformat(protocol['registered_at'])>=stamp: raise ValueError('Registration must precede capture')
    dest=root/'snapshots'/day
    if dest.exists():
        return dict(status='ALREADY_CAPTURED_IMMUTABLE',date=day)
    daily=read(source/'days'/(day+'.json.gz'))
    production=read(source/'recommendations'/(day+'.json'))
    if not daily:
        state=read(source/'status.json',{})
        if state.get('date')==day and state.get('status')=='NON_TRADING_DAY': return dict(status='NON_TRADING_DAY',date=day)
        raise SourceError('No fresh official trading-day observation')
    if not production or production.get('strategy_version')!=STRATEGY_VERSION or production.get('test_only') or production.get('approved_formal_catchup'):
        raise SourceError('Current same-day formal v8 reference is required')
    if production.get('as_of',production.get('date'))!=day:
        raise SourceError('Formal reference content date differs from capture date')
    if daily['date']!=day: raise SourceError('Daily date mismatch')
    _audit_today(daily.get('source_audit'),day,stamp)
    financial=read(source/'financials'/f'{day}-{day}-eps.json.gz')
    if not financial or financial.get('observed_date')!=day: raise SourceError('Current same-day financial comparison snapshot is required')
    _audit_today(financial.get('source_audit'),day,stamp)
    # Historical market observations are sealed by content address and reused, not rewritten.
    files=sorted(p for p in (source/'days').glob('*.json.gz') if p.name[:10]<=day)
    sessions=[read(p)['date'] for p in files]
    if sessions!=sorted(set(sessions)) or sessions[-1]!=day: raise SourceError('Invalid source calendar')
    used=files[-120:]
    if len(used)<120: raise SourceError('120 observed sessions required')
    prices=[]; institutions=[]; history=[]
    for p in used:
        h=file_hash(p); rel=Path('market-history')/p.name[:10]/(h+'.json.gz'); target=root/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists(): shutil.copyfile(p,target)
        if file_hash(target)!=h: raise ValueError('Content-addressed market input changed')
        history.append(dict(path=rel.as_posix(),sha256=h))
        item=read(p); prices.extend(item['prices']); institutions.extend(item['institutional'])
    broad=broad_c5(day,sessions,prices,institutions,financial)
    expanded=prepare_eps(root/'observations'/day,day,sessions,prices,institutions,financial,candidate_pool=broad['candidates'])
    comparison=build(day,sessions,prices,institutions,expanded)
    ids=lambda rs:[(r['market'],r['symbol']) for r in rs]
    if ids(comparison['strategies']['A']['candidates'])!=ids(production['candidates']):
        raise ValueError('Shadow A differs from saved formal v8 reference')
    end=now()
    if end.astimezone(TAIPEI).date().isoformat()!=day: raise SourceError('Observation crossed midnight; cannot capture')
    # No assumption of complete dividend/action coverage is made from ordinary quote data.
    snapshot=dict(date=day,captured_at=end.isoformat(),sessions=sessions,prices=daily['prices'],
        selections={v:r['candidates'] for v,r in comparison['strategies'].items()},
        corporate_actions=[],action_coverage='UNKNOWN',registration_hash=digest(protocol),
        test_only=True,excluded_from_performance=True,production_reference_hash=digest(production))
    staging=root/'staging'/uuid.uuid4().hex; staging.mkdir(parents=True)
    write(staging/'snapshot.json.gz',snapshot)
    write(staging/'inputs.json.gz',dict(market_history=history,financial=expanded,production_reference=production))
    write(staging/'comparison.json.gz',comparison)
    write(staging/'receipt.json',dict(registration_hash=digest(protocol),
        files={n:file_hash(staging/n) for n in ('snapshot.json.gz','inputs.json.gz','comparison.json.gz')}))
    dest.parent.mkdir(exist_ok=True); staging.rename(dest)
    return dict(status='CAPTURED',date=day,counts={v:len(rows) for v,rows in snapshot['selections'].items()},
        action_coverage='UNKNOWN',formal_holdout_pass=False)


def attest_actions(root,path):
    """Attach reviewed, versioned evidence without rewriting any captured observation."""
    root=Path(root); verify(root); ledger=read(Path(path)); day=ledger['date']
    if not (root/'snapshots'/day).exists(): raise ValueError('No matching snapshot')
    if ledger.get('coverage')!='VERIFIED' or set(ledger.get('markets',[]))!={'twse','tpex'} or not ledger.get('source_urls'):
        raise ValueError('Complete two-market action coverage with evidence is required')
    reviewed=datetime.fromisoformat(ledger['reviewed_at'])
    if reviewed.tzinfo is None or reviewed>now(): raise ValueError('Invalid actual review time')
    if not isinstance(ledger.get('corporate_actions'),list) or not isinstance(ledger.get('suspensions'),list):
        raise ValueError('Explicit action and suspension lists required')
    target=root/'action-reviews'/day/(digest(ledger)+'.json')
    if not target.exists(): write(target,ledger)
    return dict(status='ACTION_REVIEW_ATTACHED',date=day,hash=digest(ledger))


def score(root):
    from .abc_scoring import evaluate
    root=Path(root); protocol=verify(root); snapshots=[]; missing=[]
    for folder in sorted((root/'snapshots').glob('*')):
        snap=read(folder/'snapshot.json.gz')
        ledgers=sorted((root/'action-reviews'/folder.name).glob('*.json'))
        if len(ledgers)>1: raise ValueError('Conflicting action reviews require a new explicit reviewed version')
        if ledgers:
            ledger=read(ledgers[0])
            if digest(ledger)!=ledgers[0].stem: raise ValueError('Action evidence changed')
            snap['corporate_actions']=ledger['corporate_actions']; snap['action_coverage']='VERIFIED'
            px={(r['market'],r['symbol']):r for r in snap['prices']}
            for suspension in ledger['suspensions']:
                if not suspension.get('source_url'): raise ValueError('Suspension lacks evidence')
                key=suspension['market'],suspension['symbol']
                if key not in px:
                    px[key]=dict(market=key[0],symbol=key[1],date=snap['date'])
                px[key]['suspended']=True
            snap['prices']=list(px.values())
        else: missing.append(snap['date'])
        snapshots.append(snap)
    if missing:
        result=dict(status='BLOCKED_ACTION_COVERAGE',missing_action_dates=missing,
            nominal_mature_decision_dates=max(0,len(snapshots)-protocol['horizon']),
            formal_holdout_pass=False)
    else:
        result=evaluate(snapshots,protocol)
        result['decision_checks']=assess(result,protocol)
    result['snapshot_count']=len(snapshots)
    stamp=now().strftime('%Y%m%dT%H%M%S%fZ'); path=root/'evaluations'/(stamp+'.json')
    write(path,result); write(root/'latest-evaluation.json',dict(path=path.relative_to(root).as_posix(),sha256=file_hash(path)))
    return dict(status=result.get('status'),snapshot_count=len(snapshots),report=str(path),formal_holdout_pass=False)


def assess(result,protocol):
    """Evaluate the frozen first-window claims; never auto-promote production."""
    checks={}
    for claim,older in (('C_minus_A','A'),('C_minus_B','B')):
        primary=result.get('primary_statistics',{}).get(claim,{}).get('60',{})
        stress=result.get('primary_statistics',{}).get(claim,{}).get('100',{})
        cv=result.get('portfolios',{}).get('C',{})
        ov=result.get('portfolios',{}).get(older,{})
        nav=cv.get('60',{}).get('primary_window',{})
        ref=ov.get('60',{}).get('primary_window',{})
        stress_nav=cv.get('100',{}).get('primary_window',{})
        complete=nav.get('completed') is True and ref.get('completed') is True
        delta=primary.get('increment'); lower=primary.get('lower_bound')
        required=protocol['minimum_increment'] if older=='A' else 0
        checks[claim]=dict(
            fixed_window_complete=primary.get('n',0)==protocol['minimum_decision_dates'],
            differing_selection_dates=primary.get('differing_selection_dates',0),
            mean_improved=delta is not None and (delta>=required if older=='A' else delta>0),
            corrected_lower_bound_positive=lower is not None and lower>0,
            stress_increment_positive=stress.get('increment') is not None and stress['increment']>0,
            stress_capital_positive=stress_nav.get('completed') is True and stress_nav['total_return']>0,
            capital_window_complete=complete,
            capital_return_improved=complete and nav['total_return']>ref['total_return'],
            drawdown_not_worse=complete and nav['max_drawdown']>=ref['max_drawdown'])
    passing=all(all(value is True for key,value in c.items() if key!='differing_selection_dates') for c in checks.values())
    return dict(claims=checks,status='RESEARCH_PASS_REQUIRES_REVIEW' if passing else 'INCONCLUSIVE_OR_NOT_PASSED',
        auto_promote=False)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('action',choices=['register','capture','status','verify','score','attest-actions'])
    p.add_argument('--run-dir',default='test-runs/abc-forward-v1'); p.add_argument('--source-dir',default='data')
    p.add_argument('--date',default=None); p.add_argument('--file')
    a=p.parse_args(argv); root=Path(a.run_dir)
    if a.action=='register': result=register(root)
    elif a.action=='capture': result=capture(root,Path(a.source_dir),a.date or today().isoformat())
    elif a.action=='status': result=status(root)
    elif a.action=='verify': verify(root); result=dict(status='SEALED_INPUTS_PASS')
    elif a.action=='score': result=score(root)
    else:
        if not a.file: p.error('--file required')
        result=attest_actions(root,Path(a.file))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__': main()
