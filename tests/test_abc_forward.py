"""IO-boundary tests for immutable same-day A/B/C research observations."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tstocknews import STRATEGY_VERSION
from tstocknews import abc_forward as f
from tstocknews.official import SourceError
from tstocknews.storage import read, write


DAY='2026-10-30'
NOW=datetime(2026,10,30,13,30,tzinfo=timezone.utc)  # 21:30 Taipei


def sessions_ending(day=DAY,count=120):
    result=[]; current=date.fromisoformat(day)
    while len(result)<count:
        if current.weekday()<5:result.append(current.isoformat())
        current-=timedelta(days=1)
    return sorted(result)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        (f.REPO/'test-runs').mkdir(exist_ok=True)
        self.temp=Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=f.REPO/'test-runs')))
        self.root=self.temp/'run';self.source=self.temp/'source'
        self.manifest={'fixture.py':'known-source-hash'}
        declaration=self.temp/'declaration.json'
        write(declaration,dict(registered_at='2026-10-30T12:00:00+00:00',code_manifest=self.manifest,
            production_version=STRATEGY_VERSION,horizon=60,minimum_decision_dates=360,minimum_increment=.005))
        self.stack.enter_context(patch.object(f,'DECLARATION',declaration))
        self.stack.enter_context(patch.object(f,'code_manifest',return_value=self.manifest))
        self.stack.enter_context(patch.object(f,'now',return_value=NOW))
        f.register(self.root)
        self.sessions=sessions_ending()
        self.a=dict(market='twse',symbol='1000',close=100)
        self.other=dict(market='twse',symbol='1001',close=100)
        for day in self.sessions:
            rows=[dict(date=day,market='twse',symbol=s,open=100,high=101,low=99,close=100,
                       volume_shares=3000000) for s in ('1000','1001')]
            write(self.source/'days'/(day+'.json.gz'),dict(date=day,prices=rows,
                institutional=[dict(date=day,market='twse',symbol=s,net_buy_shares=1000) for s in ('1000','1001')],
                source_audit=[dict(url='https://fixture.invalid/official',fetched_at=NOW.isoformat())]))
        self.financial=dict(observed_date=DAY,source_audit=[dict(url='https://fixture.invalid/financial',fetched_at=NOW.isoformat())])
        write(self.source/'financials'/f'{DAY}-{DAY}-eps.json.gz',self.financial)
        write(self.source/'recommendations'/(DAY+'.json'),dict(date=DAY,strategy_version=STRATEGY_VERSION,candidates=[self.a]))
        self.comparison=dict(strategies={
            'A':dict(candidates=[self.a]),'B':dict(candidates=[self.a,self.other]),
            'C':dict(candidates=[self.other,self.a])},eligible_pool=[self.a,self.other])

    def mocks(self):
        stack=ExitStack()
        broad=stack.enter_context(patch.object(f,'broad_c5',return_value=dict(candidates=[self.a,self.other])))
        prepare=stack.enter_context(patch.object(f,'prepare_eps',return_value=dict(self.financial,expanded=True)))
        build=stack.enter_context(patch.object(f,'build',return_value=deepcopy(self.comparison)))
        return stack,broad,prepare,build

    def capture(self):
        stack,broad,prepare,build=self.mocks()
        with stack:
            result=f.capture(self.root,self.source,DAY)
        return result,broad,prepare,build

    def test_registration_is_immutable_and_code_change_rejected(self):
        p=self.root/'registration.json'; before=p.read_bytes();mtime=p.stat().st_mtime_ns
        f.register(self.root)
        self.assertEqual(p.read_bytes(),before);self.assertEqual(p.stat().st_mtime_ns,mtime)
        with patch.object(f,'code_manifest',return_value={'fixture.py':'changed'}):
            with self.assertRaises(ValueError):f.register(self.root)
            with self.assertRaises(ValueError):f.verify(self.root)
        saved=read(p);saved['registered_at']='2026-10-30T11:00:00+00:00';write(p,saved)
        with self.assertRaises(ValueError):f.verify(self.root)

    def test_old_future_and_early_capture_rejected(self):
        for day in ('2026-10-29','2026-10-31'):
            with self.assertRaises(ValueError):f.capture(self.root,self.source,day)
        with patch.object(f,'now',return_value=NOW.replace(hour=12)):
            with self.assertRaises(ValueError):f.capture(self.root,self.source,DAY)
        self.assertFalse((self.root/'snapshots').exists())

    def test_capture_crossing_midnight_is_not_sealed(self):
        stack,*_=self.mocks()
        with stack,patch.object(f,'now',side_effect=[NOW,datetime(2026,10,30,16,1,tzinfo=timezone.utc)]):
            with self.assertRaises(SourceError):f.capture(self.root,self.source,DAY)
        self.assertFalse((self.root/'snapshots'/DAY).exists())

    def test_weekend_needs_no_source_folder(self):
        stamp=datetime(2026,10,31,4,tzinfo=timezone.utc)
        with patch.object(f,'now',return_value=stamp):
            result=f.capture(self.root,self.temp/'absent','2026-10-31')
        self.assertEqual(result['status'],'NON_TRADING_DAY')

    def test_current_daily_and_financial_source_audits_required(self):
        for filename in (self.source/'days'/(DAY+'.json.gz'),self.source/'financials'/f'{DAY}-{DAY}-eps.json.gz'):
            original=read(filename)
            for audit in ([],[dict(fetched_at='2026-10-29T13:30:00+00:00')],
                          [dict(fetched_at='2026-10-30T14:00:00+00:00')],
                          [dict(fetched_at='2026-10-30T13:30:00')]):
                write(filename,dict(original,source_audit=audit))
                with self.assertRaises(SourceError):f.capture(self.root,self.source,DAY)
            write(filename,original)

    def test_capture_uses_complete_pool_and_preserves_source_and_snapshot(self):
        before={str(p.relative_to(self.source)):(p.read_bytes(),p.stat().st_mtime_ns)
                for p in self.source.rglob('*') if p.is_file()}
        result,broad,prepare,build=self.capture()
        self.assertEqual(result['status'],'CAPTURED')
        self.assertEqual(prepare.call_args.kwargs['candidate_pool'],[self.a,self.other])
        self.assertTrue(build.call_args.args[-1]['expanded'])
        self.assertEqual(len(build.call_args.args[2]),240)
        after={str(p.relative_to(self.source)):(p.read_bytes(),p.stat().st_mtime_ns)
               for p in self.source.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        folder=self.root/'snapshots'/DAY
        sealed={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in folder.iterdir()}
        self.assertEqual(f.capture(self.root,self.source,DAY)['status'],'ALREADY_CAPTURED_IMMUTABLE')
        self.assertEqual(sealed,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in folder.iterdir()})
        snapshot=read(folder/'snapshot.json.gz')
        self.assertTrue(snapshot['test_only']);self.assertTrue(snapshot['excluded_from_performance'])
        self.assertEqual(snapshot['action_coverage'],'UNKNOWN')

    def test_saved_formal_reference_parity_is_required(self):
        stack,*_=self.mocks()
        with stack:
            wrong=deepcopy(self.comparison);wrong['strategies']['A']['candidates']=[self.other]
            with patch.object(f,'build',return_value=wrong):
                with self.assertRaises(ValueError):f.capture(self.root,self.source,DAY)
        self.assertFalse((self.root/'snapshots'/DAY).exists())

    def test_saved_formal_reference_body_date_must_match(self):
        write(self.source/'recommendations'/(DAY+'.json'),dict(as_of='2026-10-29',
            strategy_version=STRATEGY_VERSION,candidates=[self.a]))
        stack,*_=self.mocks()
        with stack:
            with self.assertRaisesRegex(SourceError,'content date'):f.capture(self.root,self.source,DAY)

    def test_modified_snapshot_and_market_history_are_rejected(self):
        self.capture();f.verify(self.root)
        p=self.root/'snapshots'/DAY/'snapshot.json.gz';original=p.read_bytes()
        write(p,dict(read(p),date='2026-10-29'))
        with self.assertRaises(ValueError):f.verify(self.root)
        p.write_bytes(original);f.verify(self.root)
        history=read(self.root/'snapshots'/DAY/'inputs.json.gz')['market_history'][0]
        p=self.root/history['path'];p.write_bytes(b'tampered')
        with self.assertRaises(ValueError):f.verify(self.root)

    def test_unknown_actions_cannot_escape_blocked_scoring_wrapper(self):
        self.capture()
        with patch('tstocknews.abc_scoring.evaluate') as evaluate:
            result=f.score(self.root)
            evaluate.assert_not_called()
        self.assertEqual(result['status'],'BLOCKED_ACTION_COVERAGE')
        self.assertFalse(result['formal_holdout_pass'])
        saved=read(Path(result['report']))
        self.assertEqual(saved['missing_action_dates'],[DAY])
        self.assertNotIn('portfolios',saved)

    def test_assessment_needs_rank_contribution_and_complete_capital(self):
        protocol=dict(minimum_decision_dates=360,minimum_increment=.005)
        stats={claim:{str(cost):dict(n=360,increment=.006,lower_bound=.001,
            differing_selection_dates=30) for cost in (60,100)} for claim in ('C_minus_A','C_minus_B')}
        nav=dict(completed=True,total_return=.1,max_drawdown=-.05)
        portfolios={v:{str(cost):dict(primary_window=dict(nav,total_return=.12 if v=='C' else .1))
            for cost in (60,100)} for v in ('A','B','C')}
        result=dict(primary_statistics=stats,portfolios=portfolios)
        self.assertEqual(f.assess(result,protocol)['status'],'RESEARCH_PASS_REQUIRES_REVIEW')
        stats['C_minus_B']['60']['lower_bound']=None
        self.assertEqual(f.assess(result,protocol)['status'],'INCONCLUSIVE_OR_NOT_PASSED')
        stats['C_minus_B']['60']['lower_bound']=.001
        portfolios['C']['60']['primary_window']['completed']=False
        self.assertFalse(f.assess(result,protocol)['claims']['C_minus_A']['capital_window_complete'])


if __name__=='__main__':unittest.main()
