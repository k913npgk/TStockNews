import unittest
from unittest.mock import patch
from tstocknews.abc_scoring import evaluate,simulate


def fixtures(n=7, price=100):
    days=[f'2026-01-{i+1:02d}' for i in range(n)]
    return [dict(date=d,sessions=days[:i+1],selections={v:[dict(market='twse',symbol='A')] for v in ('A','B','C')},
        prices=[dict(date=d,market='twse',symbol='A',open=price,high=price,low=price,close=price)],corporate_actions=[])
        for i,d in enumerate(days)]


class CapitalTests(unittest.TestCase):
    def test_overlapping_sleeves_hold_actual_fixed_shares(self):
        snaps=fixtures(5)
        for i,p in enumerate((100,100,110,121,121)):
            snaps[i]['prices'][0].update(open=p,high=p,low=p,close=p)
        result=simulate(snaps,'A',0,dict(horizon=2),[snaps[0]['date'],snaps[1]['date']])
        self.assertTrue(result['completed'])
        self.assertAlmostEqual(result['total_return'],.1)
        self.assertAlmostEqual(result['fills'][0]['shares'],.005)
        self.assertAlmostEqual(result['fills'][1]['shares'],.5/110)

    def test_costs_overlap_and_no_leverage(self):
        snaps=fixtures(); protocol=dict(horizon=2,max_entry_wait_sessions=1)
        result=simulate(snaps,'A',60,protocol,[snaps[0]['date'],snaps[1]['date']])
        self.assertTrue(result['completed'])
        expected=(1-.003)/(1+.003)-1
        self.assertAlmostEqual(result['total_return'],expected)
        self.assertTrue(all(x['cash']>=-1e-10 and x['invested_fraction']<=1 for x in result['curve']))
        self.assertEqual(len(result['fills']),2)

    def test_split_and_cash_use_old_shares(self):
        snaps=fixtures(4)
        snaps[2]['corporate_actions']=[dict(date=snaps[2]['date'],market='twse',symbol='A',verified=True,
            source_url='https://official.example/event',share_multiplier=2,cash_per_old_share=10,
            cash_payment_date=snaps[2]['date'])]
        snaps[2]['prices'][0].update(open=45,high=45,low=45,close=45)
        result=simulate(snaps,'A',0,dict(horizon=2),[snaps[0]['date']],snaps[2]['date'])
        self.assertTrue(result['completed'])
        self.assertAlmostEqual(result['total_return'],0)

    def test_cash_entitlement_cannot_be_reinvested_before_payment(self):
        snaps=fixtures(6)
        snaps[2]['corporate_actions']=[dict(date=snaps[2]['date'],market='twse',symbol='A',verified=True,
            source_url='https://official.example/event',share_multiplier=1,cash_per_old_share=10,
            cash_payment_date=snaps[4]['date'])]
        snaps[2]['prices'][0].update(open=90,high=90,low=90,close=90)
        result=simulate(snaps,'A',0,dict(horizon=2),[snaps[0]['date']],snaps[2]['date'])
        self.assertFalse(result['completed'])
        self.assertAlmostEqual(result['curve'][-1]['receivables'],.05)
        self.assertAlmostEqual(result['curve'][-1]['cash'],.95)
        result=simulate(snaps,'A',0,dict(horizon=2),[snaps[0]['date']])
        self.assertTrue(result['completed']);self.assertAlmostEqual(result['total_return'],0)

    def test_explicit_halt_entry_defers_then_actual_holding_period(self):
        snaps=fixtures(5)
        snaps[1]['prices'][0].update(open=0,high=0,low=0,close=0,suspended=True)
        result=simulate(snaps,'A',0,dict(horizon=2,max_entry_wait_sessions=2),[snaps[0]['date']])
        self.assertTrue(result['completed'])
        self.assertEqual(result['fills'][0]['date'],snaps[2]['date'])
        self.assertEqual(result['last_liquidation_date'],snaps[3]['date'])

    def test_known_no_fill_stays_cash_zero_quote_unknown_blocks(self):
        snaps=fixtures(4)
        for i in (1,2): snaps[i]['prices'][0].update(suspended=True,open=0,high=0,low=0,close=0)
        result=simulate(snaps,'A',60,dict(horizon=2,max_entry_wait_sessions=1),[snaps[0]['date']])
        self.assertTrue(result['completed']);self.assertEqual(result['total_return'],0)
        self.assertEqual(len(result['no_fills']),1)
        snaps[1]['prices'][0].pop('suspended')
        blocked=simulate(snaps,'A',60,dict(horizon=2),[snaps[0]['date']])
        self.assertEqual(blocked['status'],'BLOCKED_ACCOUNTING_INPUT')
        self.assertIsNone(blocked['total_return'])

    def test_halt_mark_carried_but_missing_mark_blocked(self):
        snaps=fixtures(5)
        snaps[2]['prices'][0].update(suspended=True,open=0,high=0,low=0,close=0)
        result=simulate(snaps,'A',0,dict(horizon=2),[snaps[0]['date']])
        self.assertTrue(result['completed'])
        self.assertEqual(result['last_liquidation_date'],snaps[3]['date'])
        self.assertAlmostEqual(result['curve'][2]['nav'],1)
        snaps[2]['prices']=[]
        result=simulate(snaps,'A',0,dict(horizon=2),[snaps[0]['date']])
        self.assertEqual(result['status'],'BLOCKED_ACCOUNTING_INPUT')

    def test_unknown_action_blocks_and_cannot_reuse_busy_sleeve(self):
        snaps=fixtures(6)
        snaps[2]['corporate_actions']=[dict(date=snaps[2]['date'],market='twse',symbol='A',verified=False)]
        result=simulate(snaps,'A',0,dict(horizon=2),[snaps[0]['date']])
        self.assertEqual(result['missing'][0]['stage'],'CORPORATE_ACTION_UNKNOWN')
        snaps[2]['corporate_actions']=[]
        snaps[2]['prices'][0].update(suspended=True,open=0,high=0,low=0,close=0)
        result=simulate(snaps,'A',0,dict(horizon=2),[snaps[0]['date'],snaps[2]['date']])
        self.assertEqual(len(result['skipped_signals']),1)


class EvaluationTests(unittest.TestCase):
    def test_fixed_primary_window_does_not_expand_with_later_observations(self):
        snaps=fixtures(7)
        for snap in snaps:
            snap['selections']['A']=[];snap['selections']['B']=[]
        # First two C cohorts both have known 10% gains; later observations have
        # different performance and a missing quote, neither revises that test.
        for i,p in enumerate((100,100,110,121,200,10,10)):
            snaps[i]['prices'][0].update(open=p,high=p,low=p,close=p)
        protocol=dict(horizon=2,minimum_decision_dates=2,costs_bps=[60],bootstrap_reps=10)
        calls=[]
        def lower(values,*args):
            calls.append(list(values)); return sum(values)/len(values)
        with patch('tstocknews.abc_scoring._bootstrap_lower',side_effect=lower):
            first=evaluate(snaps[:4],protocol)
            first_values=list(calls)
            snaps[5]['prices']=[]
            calls.clear(); later=evaluate(snaps,protocol)
        self.assertEqual(first_values,calls)
        self.assertEqual(first['primary_statistics'],later['primary_statistics'])
        self.assertEqual(later['fixed_primary_dates'],[snaps[0]['date'],snaps[1]['date']])
        self.assertEqual(later['primary_statistics']['C_minus_A']['60']['n'],2)
        self.assertIsNone(later['primary_statistics']['B_minus_A']['60']['lower_bound'])
        self.assertIsNone(later['comparisons']['C_minus_A']['60']['lower_bound'])

    def test_incomplete_fixed_primary_window_never_uses_surviving_subset(self):
        snaps=fixtures(6)
        snaps[2]['prices']=[]
        with patch('tstocknews.abc_scoring._bootstrap_lower') as bootstrap:
            result=evaluate(snaps,dict(horizon=2,minimum_decision_dates=2,costs_bps=[60]))
        bootstrap.assert_not_called()
        self.assertIsNone(result['primary_statistics']['C_minus_A']['60']['increment'])
        self.assertIsNone(result['primary_statistics']['C_minus_A']['60']['lower_bound'])

    def test_stress_cost_is_not_a_second_primary_test_family(self):
        snaps=fixtures(5)
        for snap in snaps:
            snap['selections']['A']=[];snap['selections']['B']=[]
        with patch('tstocknews.abc_scoring._bootstrap_lower',return_value=.01) as bootstrap:
            result=evaluate(snaps,dict(horizon=2,minimum_decision_dates=2))
        self.assertEqual(bootstrap.call_count,2)
        self.assertIsNone(result['primary_statistics']['C_minus_A']['100']['lower_bound'])
        self.assertEqual(result['primary_statistics']['C_minus_A']['100']['status'],'STRESS_DIAGNOSTIC_ONLY')

    def test_immaturity_and_identical_exposure_never_pass(self):
        result=evaluate(fixtures(2),dict(horizon=2))
        self.assertEqual(result['mature_common_dates'],[])
        self.assertFalse(result['formal_holdout_pass'])
        result=evaluate(fixtures(6),dict(horizon=2))
        self.assertEqual(result['comparisons']['C_minus_B']['60']['status'],'NO_INCREMENTAL_EXPOSURE')
        self.assertIsNone(result['comparisons']['C_minus_B']['60']['lower_bound'])

    def test_missing_observed_session_detected(self):
        snaps=fixtures(5);snaps.pop(2)
        result=evaluate(snaps,dict(horizon=2))
        self.assertEqual(result['status'],'BLOCKED_MISSING_SESSION')


if __name__=='__main__':unittest.main()
