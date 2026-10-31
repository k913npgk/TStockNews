from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.eps_fixture import ready
from tests.test_engine import bars, weekdays
from tstocknews import abc_selection as abc
from tstocknews import engine
from tstocknews import eps_improvement
from tstocknews.analysis import recommendations
from tstocknews.official import SourceError


def fixture(n=25):
    sessions = weekdays(125); day = sessions[-1]
    universe = [dict(market='twse', symbol=f'{1000+i}', name='fixture') for i in range(n)]
    px = [r for c in universe for r in bars(c['symbol'], sessions=sessions)]
    inst = [dict(market='twse',symbol=c['symbol'],date=d,net_buy_shares=100000-i*1000)
            for i,c in enumerate(universe) for d in sessions[-3:]]
    financial = ready(dict(universe=universe,observed_date=day,fundamentals=[
        dict(market='twse',symbol=c['symbol'],eps=2,revenue_yoy_3m=.2,
             revenue_yoy_prev_3m=.1,available_date=day,fiscal_period='2025-Q1') for c in universe]))
    return day,sessions,px,inst,financial


def indicators(rows):
    strong = int(rows[-1]['symbol']) >= 1013
    k = [50.] * 118 + ([59.,60.] if strong else [49.,48.])
    d = [50.] * 118 + ([54.,55.] if strong else [55.,55.])
    dif = [0.] * 117 + ([1.,2.,3.] if strong else [3.,2.,1.])
    return dict(k=k,d=d,dif=dif,dea=[0.] * 120)


class ABCSelectionTests(unittest.TestCase):
    @patch('tstocknews.engine._indicators',side_effect=indicators)
    def test_exact_v8_and_shared_pool_ranking_over_twenty(self,_):
        args=fixture(); before=deepcopy(args)
        got=abc.build(*args); ref=recommendations(*args)
        self.assertEqual(got['strategies']['A']['candidates'],ref['candidates'])
        self.assertEqual(got['strategies']['A']['data_hash'],ref['data_hash'])
        for name, result in got['strategies'].items():
            self.assertTrue(result['test_only']);self.assertTrue(result['excluded_from_performance'])
            for signal in result['signals']:
                self.assertTrue(signal['signal_id'].startswith(f'test:abc:{name}:'))
                self.assertTrue(signal['test_only']);self.assertTrue(signal['excluded_from_performance'])
        b=got['strategies']['B']['candidates']; c=got['strategies']['C']['candidates']
        self.assertEqual(len(got['eligible_pool']),25)
        self.assertEqual(len(b),20);self.assertEqual(len(c),20)
        self.assertNotEqual({r['symbol'] for r in b},{r['symbol'] for r in c})
        self.assertEqual(c[0]['symbol'],'1013')
        self.assertEqual(args,before)
        weak=next(r for r in b if r['symbol']=='1000')
        self.assertAlmostEqual(weak['macd_dif_slope_3_sessions'],(1.-3.)/130.)

    @patch('tstocknews.engine._indicators',side_effect=indicators)
    def test_other_gates_and_eps_financials_remain_decisive(self,_):
        args=list(fixture()); args[4]['fundamentals'][0]['eps_prior_ytd']=3
        args[4]['fundamentals'][0]['eps_comparison']['eps_prior_ytd']=3
        args[3]=[r for r in args[3] if r['symbol']!='1001']
        got=abc.build(*args)
        symbols={r['symbol'] for r in got['eligible_pool']}
        self.assertNotIn('1000',symbols);self.assertNotIn('1001',symbols)
        self.assertIn('1002',symbols)

    @patch('tstocknews.engine._indicators',side_effect=indicators)
    def test_monthly_filter_precedes_final_cap(self,_):
        args=list(fixture(35))
        for f in args[4]['fundamentals'][:10]: f['revenue_months'][0]['current']=90
        got=abc.build(*args)
        self.assertEqual(len(got['eligible_pool']),25)
        self.assertEqual(got['strategies']['B']['candidates'][0]['symbol'],'1010')

    def test_future_snapshot_and_conflicting_bar_rejected(self):
        args=list(fixture(1));args[4]['observed_date']='2099-01-01'
        with self.assertRaises(SourceError):abc.build(*args)
        for bad in (None,'','not-a-date'):
            args=list(fixture(1));args[4]['observed_date']=bad
            with self.assertRaises(SourceError):abc.build(*args)
        args=list(fixture(1));args[2].append(dict(args[2][-1],close=999))
        with self.assertRaises(ValueError):abc.broad_c5(*args)

    @patch('tstocknews.engine._indicators',side_effect=indicators)
    def test_missing_eps_schema_is_source_failure(self,_):
        args=list(fixture());del args[4]['eps_comparison_schema_version']
        with self.assertRaises(SourceError):abc.build(*args)

    @patch('tstocknews.engine._indicators',side_effect=indicators)
    def test_unknown_technical_values_are_excluded_from_common_pool(self,_):
        original=engine.screen
        def incomplete(*args,**kwargs):
            result=original(*args,**kwargs)
            result['diagnostics']['symbols']['twse:1000']['macd_dif']=float('nan')
            return result
        with patch('tstocknews.engine.screen',side_effect=incomplete):
            got=abc.broad_c5(*fixture())
        self.assertNotIn('1000',{r['symbol'] for r in got['candidates']})
        self.assertEqual(got['diagnostics']['symbols']['twse:1000']['reasons'],['ABC_TECHNICAL_INPUT_UNKNOWN'])

    @patch('tstocknews.engine._indicators',side_effect=indicators)
    def test_prepare_broad_pool_fetches_only_missing_comparisons(self,_):
        args=list(fixture());day=args[0];financial=args[4]
        missing=financial['fundamentals'][0]
        fact=deepcopy(missing['eps_comparison'])
        missing['eps_comparison_status']='UNKNOWN';missing.pop('eps_comparison')
        # Valid negative growth is an observed comparison, not a download gap.
        negative=financial['fundamentals'][1]
        negative['eps_prior_ytd']=3;negative['eps_comparison']['eps_prior_ytd']=3
        broad=abc.broad_c5(*args)['candidates']
        self.assertIn('1000',{r['symbol'] for r in broad})
        identity=dict(symbol='1000',year=2025,quarter=1,statement_scope='consolidated')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'test-runs') as temp, \
                patch('tstocknews.eps_improvement.today',return_value=date.fromisoformat(day)), \
                patch('tstocknews.eps_improvement.download_filing',return_value=b'fixture') as download, \
                patch('tstocknews.eps_facts.identify_filing',return_value=identity), \
                patch('tstocknews.eps_facts.extract_comparison',return_value=fact):
            result=eps_improvement.prepare(Path(temp),*args,candidate_pool=broad)
        self.assertEqual(download.call_count,1)
        self.assertIn('CO_ID=1000',download.call_args.args[0])
        self.assertEqual(result['fundamentals'][0]['eps_comparison_status'],'KNOWN')
        self.assertEqual(result['fundamentals'][1]['eps_prior_ytd'],3)
        self.assertEqual(financial['fundamentals'][0]['eps_comparison_status'],'UNKNOWN')


if __name__=='__main__':unittest.main()
