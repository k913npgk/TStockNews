"""Verify forward-only statistics, return denominators and state isolation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tstocknews import quality_performance as perf, quality_shadow as shadow
from tstocknews.engine import track
from tstocknews.storage import read, write


VERSION = shadow.VERSION
SESSIONS = ['2026-11-06', '2026-11-09', '2026-11-10', '2026-11-11']


def observation(day, symbols=('1000',), **extra):
    return dict(date=day, version=VERSION, test_only=True,
                excluded_from_performance=True, retrospective=False,
                members=[dict(market='twse',symbol=s) for s in symbols], **extra)


def quote(day, symbol, **extra):
    return dict(date=day, market='twse', symbol=symbol, open=100, close=110, **extra)


class QualityPerformanceTests(unittest.TestCase):
    def test_next_session_entry_horizons_and_unknown_denominators(self):
        history = [observation(SESSIONS[0], ('1000', '1001', '1002')),
                   observation(SESSIONS[-1])]
        prices = [quote(SESSIONS[1],'1000'), quote(SESSIONS[3],'1000'),
                  dict(quote(SESSIONS[1],'1001'),close=90),
                  dict(quote(SESSIONS[3],'1001'),close=120)]
        result = perf.calculate(history,SESSIONS,prices,SESSIONS[-1],VERSION)
        one = [r for r in result['rows'] if r['horizon']==1]
        three = [r for r in result['rows'] if r['horizon']==3]
        self.assertEqual(result['signal_count'],4)
        self.assertEqual(result['observation_days'],2)
        self.assertEqual(one[0]['entry_date'],SESSIONS[1])
        self.assertEqual(three[0]['exit_date'],SESSIONS[-1])
        self.assertAlmostEqual(one[0]['price_return'],.1)
        self.assertAlmostEqual(one[1]['price_return'],-.1)
        self.assertIsNone(one[2]['price_return'])
        self.assertEqual(one[2]['status'],'MISSING_PRICE')
        self.assertEqual(one[3]['status'],'PENDING_ENTRY')
        text = '\n'.join(perf.render(result))
        self.assertIn('平均：+0.00%｜中位數：+0.00%｜上漲比例：50.0%',text)
        self.assertIn('有效：2／4筆｜待追蹤：1筆｜資料待確認：1筆',text)
        self.assertIn('平均：+15.00%｜中位數：+15.00%｜上漲比例：100.0%',text)

    def test_excludes_other_versions_retrospective_and_future_without_mutation(self):
        live = observation(SESSIONS[0])
        old = dict(live, version='old', date='2026-11-05')
        retrospective = dict(live, retrospective=True, date='2026-11-04')
        future = observation(SESSIONS[-1])
        history = [old,retrospective,live,future]
        before = deepcopy(history)
        result = perf.calculate(history,SESSIONS,[],SESSIONS[0],VERSION)
        self.assertEqual(history,before)
        self.assertEqual(result['signal_count'],1)
        self.assertEqual(result['start_date'],SESSIONS[0])
        self.assertEqual(set(result['observation_hashes']),{SESSIONS[0]})
        self.assertTrue(all(r['test_only'] and r['excluded_from_performance'] for r in result['rows']))
        self.assertEqual(track(result['rows'],SESSIONS,[],SESSIONS[-1]),[])
        text='\n'.join(perf.render(result))
        self.assertEqual(text.count('有效：0／1筆｜待追蹤：1筆'),5)
        self.assertNotIn('0.00%',text)

    def test_suspension_corporate_actions_and_unmatured_returns_are_unknown(self):
        history=[observation(SESSIONS[0],('1000','1001','1002'))]
        prices=[quote(SESSIONS[1],'1000',suspended=True),
                quote(SESSIONS[1],'1001',corporate_action_unresolved=True),
                quote(SESSIONS[1],'1002')]
        result=perf.calculate(history,SESSIONS,prices,SESSIONS[1],VERSION)
        rows={r['symbol']:r for r in result['rows'] if r['horizon']==3}
        self.assertEqual(rows['1000']['status'],'SUSPENDED')
        self.assertEqual(rows['1001']['status'],'CORPORATE_ACTION_UNRESOLVED')
        self.assertEqual(rows['1002']['status'],'NOT_MATURE')
        self.assertTrue(all(r['price_return'] is None for r in rows.values()))

    def test_rejects_nonisolated_and_duplicate_observations(self):
        live=observation(SESSIONS[0])
        for history in ([dict(live,test_only=False)], [live,live],
                        [observation(SESSIONS[0],('1000','1000'))]):
            with self.subTest(history=history),self.assertRaises(ValueError):
                perf.calculate(history,SESSIONS,[],SESSIONS[0],VERSION)

    def test_build_and_report_save_are_isolated_immutable_and_idempotent(self):
        day=SESSIONS[0]
        snapshot=observation(day)
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            source,root=base/'official',base/'test'
            write(source/'days'/f'{day}.json.gz',dict(date=day,prices=[quote(day,'1000')]))
            write(root/'snapshots'/f'{day}.json',snapshot)
            # Retrospective resend namespace must never be loaded.
            write(root/'resends'/day/'snapshot.json',dict(snapshot,retrospective=True))
            original={p:p.read_bytes() for p in base.rglob('*') if p.is_file()}
            result=perf.build(source,root,day,VERSION)
            self.assertEqual(result['signal_count'],1)
            self.assertEqual(set(result['market_hashes']),{day})
            shadow.save_report(root,day,snapshot,{},result)
            saved=root/'performance'/f'{day}.json'
            self.assertEqual(read(saved),result)
            shadow.save_report(root,day,snapshot,{},result)
            self.assertIn('測試策略績效統計',(root/'reports'/f'{day}.md').read_text(encoding='utf-8'))
            for path,raw in original.items():self.assertEqual(path.read_bytes(),raw)
            changed=dict(result,market_hashes={})
            with self.assertRaisesRegex(ValueError,'performance changed'):
                shadow.save_report(root,day,snapshot,{},changed)
            self.assertEqual(read(saved),result)

    def test_report_rejects_statistics_from_other_day_or_strategy(self):
        snapshot=observation(SESSIONS[0])
        result=perf.calculate([snapshot],SESSIONS,[],SESSIONS[0],VERSION)
        for bad in (dict(result,date=SESSIONS[1]),dict(result,version='old')):
            with self.assertRaisesRegex(ValueError,'identity mismatch'):
                shadow.render(snapshot,{},bad)


if __name__ == '__main__':
    unittest.main()
