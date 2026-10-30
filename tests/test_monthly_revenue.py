from copy import deepcopy
import unittest

from tstocknews.monthly_revenue import apply, comparison, months_ending
from tstocknews.official import SourceError


def row(symbol='1000'):
    return {'market': 'twse', 'symbol': symbol, 'revenue_end_month': '2025-01',
            'revenue_months': [{'month': m, 'current': 120, 'prior_year': 100}
                               for m in months_ending('2025-01')]}


def result(rows):
    return {'candidates': rows, 'diagnostics': {'exclusion_counts': {},
        'symbols': {'twse:' + r['symbol']: {'reasons': []} for r in rows}}}


class MonthlyRevenueTests(unittest.TestCase):
    def test_year_boundary_and_strict_positive_each_month(self):
        self.assertEqual(months_ending('2025-01'), ['2025-01','2024-12','2024-11'])
        for value in (100, 99, 0):
            r = row(); r['revenue_months'][1]['current'] = value
            self.assertEqual(apply(result([r]), '2025-02-01')['eligible_count'], 0)
        r = row(); before = deepcopy(r)
        got = apply(result([r]), '2025-02-01')['candidates'][0]
        self.assertAlmostEqual(got['minimum_monthly_revenue_yoy'], .2)
        self.assertEqual(r, before)

    def test_unknowns_duplicates_bad_dates_and_nonpositive_base(self):
        for current, prior in ((None,100),(120,0),(120,-1),(float('inf'),100),(-1,100)):
            r = row(); r['revenue_months'][0].update(current=current, prior_year=prior)
            self.assertIsNone(comparison(r,'2025-02-01'))
        for field, value in (('revenue_end_month','2025-13'),('revenue_end_month','2025-02'),
                             ('revenue_months',[]),('revenue_months',None)):
            r=row();r[field]=value
            self.assertIsNone(comparison(r,'2025-02-01'))
        r=row();r['revenue_months'].append(deepcopy(r['revenue_months'][0]))
        self.assertIsNone(comparison(r,'2025-02-01'))
        r=row();r['revenue_months'][1]['month']='2024-10'
        self.assertIsNone(comparison(r,'2025-02-01'))

    def test_source_loss_is_error_but_partial_unknown_is_explicit(self):
        bad=row();bad['revenue_months']=[]
        with self.assertRaises(SourceError): apply(result([bad]),'2025-02-01')
        out=apply(result([bad,row('1001')]),'2025-02-01')
        self.assertEqual(len(out['candidates']),1)
        self.assertEqual(out['diagnostics']['exclusion_counts']['MONTHLY_REVENUE_COMPARISON_UNKNOWN'],1)

    def test_filter_complete_pool_before_top20(self):
        rows=[row(str(1000+i)) for i in range(32)]
        for r in rows[:12]:r['revenue_months'][0]['current']=99
        out=apply(result(rows),'2025-02-01')
        self.assertEqual(out['eligible_count'],20)
        self.assertEqual(out['candidates'][-1]['symbol'],'1031')
