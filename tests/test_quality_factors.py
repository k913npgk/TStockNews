"""Material accounting, timing, unknown-data and ranking regression tests."""
from datetime import date, timedelta
from pathlib import Path
import importlib.util
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tstocknews import quality_factors as c


def context(key, instant=None, start=None, end=None, member=False, symbol='2330'):
    period = f'<xbrli:instant>{instant}</xbrli:instant>' if instant else f'<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>'
    dim = '<xbrldi:explicitMember dimension="x:y">x:z</xbrldi:explicitMember>' if member else ''
    return f'<xbrli:context id="{key}"><xbrli:entity><xbrli:identifier>{symbol}</xbrli:identifier></xbrli:entity><xbrli:period>{period}</xbrli:period>{dim}</xbrli:context>'


def fact(name, ctx, value, unit='TWD', scale=3, sign=''):
    return f'<ix:nonFraction name="ifrs-full:{name}" contextRef="{ctx}" unitRef="{unit}" format="ixt:numdotdecimal" scale="{scale}" sign="{sign}">{value}</ix:nonFraction>'


def filing(extra=''):
    return ('<xbrli:unit id="TWD"><xbrli:measure>iso4217:TWD</xbrli:measure></xbrli:unit>'
            '<xbrli:unit id="EPS"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:TWD</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>'
            + context('now', instant='2025-09-30')
            + context('ytd', start='2025-01-01', end='2025-09-30')
            + context('prior', start='2024-01-01', end='2024-09-30')
            + context('quarter', start='2025-07-01', end='2025-09-30')
            + context('dim', instant='2025-09-30', member=True)
            + fact('EquityAttributableToOwnersOfParent', 'now', '2,000')
            + fact('EquityAttributableToOwnersOfParent', 'dim', '999,999')
            + fact('ProfitLossAttributableToOwnersOfParent', 'ytd', '200')
            + fact('ProfitLossAttributableToOwnersOfParent', 'quarter', '900')
            + fact('ProfitLossAttributableToOwnersOfParent', 'prior', '100')
            + fact('BasicEarningsLossPerShare', 'ytd', '4.50', unit='EPS', scale=0)
            + fact('BasicEarningsLossPerShare', 'prior', '2.50', unit='EPS', scale=0)
            + extra).encode()


class FilingTests(unittest.TestCase):
    def test_exact_scope_context_units_and_scale(self):
        r = c.extract_filing(filing(), '2330', '2025Q3', 'cr')
        self.assertEqual(r['equity'], 2_000_000)
        self.assertEqual(r['profit'], 200_000)
        self.assertEqual(r['profit_prior'], 100_000)
        self.assertEqual(r['eps'], 4.5)

    def test_conflicting_same_fact_is_unknown(self):
        r = c.extract_filing(filing(fact('BasicEarningsLossPerShare', 'ytd', '9', unit='EPS', scale=0)), '2330', '2025Q3', 'cr')
        self.assertIsNone(r['eps'])

    def test_bad_entity_and_false_unit_name(self):
        self.assertIsNone(c.extract_filing(filing(), '1101', '2025Q3', 'cr')['eps'])
        body = filing().replace(b'xbrli:shares', b'iso4217:USD')
        self.assertIsNone(c.extract_filing(body, '2330', '2025Q3', 'cr')['eps'])

    def test_conflicting_context_is_not_accepted(self):
        body = filing(context('ytd', start='2025-07-01', end='2025-09-30'))
        self.assertIsNone(c.extract_filing(body, '2330', '2025Q3', 'cr')['profit'])

    def test_negative_and_nil_numbers(self):
        self.assertEqual(c.numeric({'scale': '3', 'sign': '-'}, '2'), -2000)
        self.assertEqual(c.numeric({'scale': '0'}, '(2.5)'), -2.5)
        self.assertIsNone(c.numeric({'xsi:nil': 'true'}, '0'))
        self.assertIsNone(c.numeric({'sign': '-'}, '-2'))


def record(period, eps, prior=0, profit=100, profit_prior=50, scope='cr'):
    return dict(period=period, eps=eps, eps_prior=prior, profit=profit,
                profit_prior=profit_prior, equity=1000, scope=scope)


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.facts = {('2330', '2024Q4'): record('2024Q4', 8, profit=800),
                      ('2330', '2025Q3'): record('2025Q3', 9, prior=6, profit=900, profit_prior=600),
                      ('2330', '2025Q4'): record('2025Q4', 12, profit=1200),
                      ('2330', '2026Q1'): record('2026Q1', 4, prior=3, profit=400, profit_prior=300)}

    def test_ytd_to_ttm_and_release_boundary(self):
        r = c.accounting('2330', '2026-01-02', self.facts)
        self.assertEqual(r['eps_ttm'], 11)
        self.assertAlmostEqual(r['roe_ttm'], 1.1)
        self.assertEqual(c.accounting('2330', '2026-04-29', self.facts)['period'], '2025Q3')
        self.assertEqual(c.accounting('2330', '2026-04-30', self.facts)['eps_ttm'], 12)
        self.assertEqual(c.accounting('2330', '2026-04-30', self.facts, 15)['period'], '2025Q3')

    def test_missing_latest_does_not_fall_back(self):
        del self.facts['2330', '2025Q4']
        self.assertIsNone(c.accounting('2330', '2026-04-30', self.facts))

    def test_scope_mismatch_blocks_ttm(self):
        self.facts['2330', '2024Q4']['scope'] = 'ir'
        self.assertIsNone(c.accounting('2330', '2026-01-02', self.facts))

    def test_revenue_forecast_uses_only_previous_months(self):
        revenue = {('2330', m): (v, 100.) for m, v in [('2025-09', 110), ('2025-10', 120), ('2025-11', 130), ('2025-12', 150), ('2026-01', 9999)]}
        r = c.revenue_surprise('2330', '2026-01-15', revenue)
        self.assertEqual(r['month'], '2025-12')
        self.assertAlmostEqual(r['surprise'], .3)
        del revenue['2330', '2025-12']
        self.assertIsNone(c.revenue_surprise('2330', '2026-01-15', revenue))


class SelectionAndReturnTests(unittest.TestCase):

    def test_average_tied_ranks_and_missing_cannot_score(self):
        rows = [dict(symbol='A', x=5), dict(symbol='B', x=5), dict(symbol='C', x=9)]
        self.assertEqual(c.ranks(rows, 'x'), {'A': .25, 'B': .25, 'C': 1.})
        with self.assertRaises(ValueError):
            c.ranks([dict(symbol='A', x=None)], 'x')

    def test_recent_month_is_skipped_in_momentum_numerator(self):
        bars = [dict(close=100*(1.001**i)*(1.0003 if i % 2 else 1)) for i in range(148)]
        a = c.momentum(bars)
        bars[-1]['close'] *= 2
        b = c.momentum(bars)
        # Volatility changes, but the underlying numerator is still the same skipped-month return.
        self.assertAlmostEqual(a['m6']*a['volatility'], b['m6']*b['volatility'])
        self.assertFalse(a['uses_12m'])
        self.assertIsNone(c.momentum([None]+bars[1:]))

    def test_cohort_requires_intermediate_marks_and_costs(self):
        days = ['2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07']
        picks = [dict(symbol='A', weight=1.)]
        prices = {'A': {d: dict(open=100, close=110) for d in days}}
        r = c.cohort(days, 0, picks, prices, 3, 100)
        self.assertAlmostEqual(r['value'], 110*.995/(100*1.005)-1)
        del prices['A'][days[2]]
        self.assertEqual(c.cohort(days, 0, picks, prices, 3, 100)['status'], 'MISSING_PRICE')
        self.assertEqual(c.cohort(days, 0, [], prices, 3, 100)['value'], 0.)
        self.assertIsNone(c.cohort(days, 1, picks, prices, 3, 100)['value'])



if __name__ == '__main__':
    unittest.main(verbosity=2)
