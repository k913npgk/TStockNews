import io
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_engine import bars, weekdays
from tstocknews.cli import main, daily
from tstocknews.engine import track
from tstocknews.analysis import recommendations
from tstocknews.preview import enrich_daily_changes, preview, target_session
from tstocknews.storage import read, write


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class PreviewTests(unittest.TestCase):
    def test_legacy_quote_enrichment_uses_official_target_day_and_keeps_source_immutable(self):
        from tstocknews.official import SourceError
        with tempfile.TemporaryDirectory() as tmp:
            source, isolated = Path(tmp) / 'source', Path(tmp) / 'isolated'
            day = '2026-10-02'
            cached = {'date': day, 'prices': [{'date': day, 'market': 'twse', 'symbol': '0001',
                      'name': '示例', 'open': 123, 'high': 126, 'low': 122, 'close': 125.5,
                      'volume_shares': 1000}], 'institutional': [], 'source_audit': []}
            path = Path('days') / (day + '.json.gz')
            write(source / path, cached)
            write(isolated / path, cached)
            before = files(source)
            twse = {'date': '20261002', 'fields': ['證券代號', '證券名稱', '成交股數',
                    '開盤價', '最高價', '最低價', '收盤價', '漲跌(+/-)', '漲跌價差'],
                    'data': [['0001', '示例', '1000', '123', '126', '122', '125.5', '+', '2.5']]}
            tpex = {'date': '20261002', 'fields': ['代號', '名稱', '成交股數', '開盤',
                    '最高', '最低', '收盤', '漲跌'],
                    'data': [['0002', '示例二', '1000', '10', '11', '9', '10', '0.00']]}
            with patch('tstocknews.preview.Client.json', side_effect=[twse, tpex]) as fetch:
                enrich_daily_changes(isolated, day)
            self.assertEqual([c.kwargs['date'] for c in fetch.call_args_list], ['20261002', '2026/10/02'])
            enriched = read(isolated / path)
            self.assertEqual(enriched['prices'][0]['price_change'], 2.5)
            self.assertAlmostEqual(enriched['prices'][0]['price_change_pct'], 2.5 / 123)
            self.assertEqual(enriched['institutional'], cached['institutional'])
            self.assertEqual(files(source), before)
            with patch('tstocknews.preview.Client.json', side_effect=AssertionError('already enriched')):
                enrich_daily_changes(isolated, day)
            # A changed official close must not revise the copied screening inputs.
            write(isolated / path, cached)
            unchanged = (isolated / path).read_bytes()
            twse['data'][0][6] = '126'
            with patch('tstocknews.preview.Client.json', side_effect=[twse, tpex]):
                with self.assertRaisesRegex(SourceError, 'conflict'):
                    enrich_daily_changes(isolated, day)
            self.assertEqual((isolated / path).read_bytes(), unchanged)
            self.assertEqual(files(source), before)

    def seed(self, root, financial=True):
        sessions = weekdays(125)
        day = sessions[-1]
        for px in bars(sessions=sessions):
            px.update(price_change=None, price_change_pct=None)
            write(root / 'days' / (px['date'] + '.json.gz'),
                  {'date': px['date'], 'prices': [px], 'institutional': [
                      {'date': px['date'], 'market': 'twse', 'symbol': '2330', 'net_buy_shares': 10000}]})
        if financial:
            company = {'market': 'twse', 'symbol': '2330', 'name': 'Test'}
            write(root / 'financials' / (day + '.json.gz'), {'observed_date': day,
                  'universe': [company], 'fundamentals': [{**company, 'eps': 1,
                  'revenue_yoy_3m': .2, 'available_date': day, 'fiscal_period': '2025-Q1'}]})
        write(root / 'status.json', {'status': 'production'})
        write(root / 'error.json', {'error': 'preserve'})
        write(root / 'recommendations' / (day + '.json'), {'signals': [{'production': True}]})
        write(root / 'performance' / (day + '.json'), [{'production': True}])
        write(root / 'delivery' / (day + '.json'), {'status': 'SENT', 'retry_key': 'production'})
        return day

    @patch('tstocknews.preview.holiday_days', return_value={'2026-10-01'})
    def test_weekend_holiday_typhoon_and_year_boundary(self, calendar):
        root = Path('unused')
        self.assertEqual(target_session(root, '2026-10-03'), '2026-10-02')
        self.assertEqual(target_session(root, '2026-10-01'), '2026-09-30')
        self.assertEqual(target_session(root, '2026-07-10'), '2026-07-09')
        calendar.side_effect = lambda c, y: {'2026-01-01'} if y == 2026 else set()
        self.assertEqual(target_session(root, '2026-01-01'), '2025-12-31')
        self.assertEqual([c.args[1] for c in calendar.call_args_list][-2:], [2026, 2025])

    @patch('tstocknews.preview.holiday_days', return_value=set())
    def test_complete_report_never_touches_production_or_tracks_performance(self, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            source, runs = Path(tmp) / 'production', Path(tmp) / 'tests'
            day = self.seed(source)
            # Future observations must not enter input hashes or candidate selection.
            write(source / 'days' / '2026-10-02.json.gz', {'date': '2026-10-02', 'prices': [], 'institutional': []})
            before = files(source)
            with patch('tstocknews.preview.sync') as sync:
                result = preview(source, runs, 'run-1', day)
                sync.assert_not_called()
            self.assertEqual(result['analysis_status'], 'COMPLETE')
            self.assertEqual(result['selected'], 1)
            self.assertEqual(files(source), before)
            recommendation = read(runs / 'run-1/data/recommendations' / (day + '.json'))
            self.assertEqual(len(recommendation['signals']), 1)
            self.assertTrue(recommendation['test_only'])
            self.assertTrue(recommendation['signals'][0]['signal_id'].startswith('test:run-1:'))
            self.assertEqual(track(recommendation['signals'], [], [], day), [])
            self.assertFalse((runs / 'run-1/data/performance').exists())
            text = (runs / 'run-1/reports' / (day + '.md')).read_text(encoding='utf-8')
            self.assertIn('【測試報告｜不計入績效】', text)
            self.assertNotIn('歷次入選表現', text)
            with patch('tstocknews.preview.target_session', side_effect=AssertionError('must reuse')):
                self.assertEqual(preview(source, runs, 'run-1', day), result)
            with self.assertRaises(ValueError):
                preview(source, runs, 'run-1', '2026-10-02')
            self.assertEqual(preview(source, runs, 'run-2', day)['selected'], 1)

    @patch('tstocknews.preview.holiday_days', return_value=set())
    def test_missing_historical_snapshot_uses_runtime_financials_with_true_dates(self, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            source, runs = Path(tmp) / 'production', Path(tmp) / 'tests'
            day = self.seed(source, financial=False)
            write(source / 'financials' / '2026-10-03.json.gz', {'observed_date': '2026-10-03'})
            before = files(source)
            runtime = {'observed_date': '2026-10-03', 'universe': [{'symbol': '2330', 'market': 'twse', 'name': 'Test'}],
                       'fundamentals': [{'symbol': '2330', 'market': 'twse', 'eps': 1, 'revenue_yoy_3m': .2,
                                         'available_date': '2026-10-03', 'fiscal_period': '2026-Q2'}]}
            with patch('tstocknews.preview.today', return_value=date(2026, 10, 3)), \
                 patch('tstocknews.preview.universe_and_financials', return_value=runtime) as fetch:
                result = preview(source, runs, 'missing', day)
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[1], '2026-10-03')
            self.assertEqual(result['analysis_status'], 'COMPLETE')
            self.assertEqual(result['selected'], 1)
            self.assertTrue(result['late_financial_snapshot'])
            self.assertEqual(files(source), before)
            saved = read(runs / 'missing/data/recommendations' / (day + '.json'))
            self.assertEqual(saved['candidates'][0]['fundamental_available_date'], '2026-10-03')
            self.assertEqual(saved['diagnostics']['fundamental_as_of'], '2026-10-03')
            self.assertEqual(read(runs / 'missing/data/financials/2026-10-03.json.gz'), runtime)
            self.assertFalse((runs / 'missing/data/performance').exists())
            text = (runs / 'missing/reports' / (day + '.md')).read_text(encoding='utf-8')
            self.assertIn('不是目標日當時資訊的歷史重現', text)

    @patch('tstocknews.preview.holiday_days', return_value=set())
    def test_future_dated_financial_row_fails_without_production_write(self, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'production'
            day = self.seed(source)
            path = source / 'financials' / (day + '.json.gz')
            financial = read(path)
            financial['fundamentals'][0]['available_date'] = '2027-01-01'
            write(path, financial)
            before = files(source)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(['--data-dir', str(source), 'test-report', '--date', day,
                             '--run-id', 'bad', '--test-dir', str(Path(tmp) / 'tests')])
            self.assertEqual(code, 1)
            self.assertEqual(files(source), before)

    def test_paths_and_ids_cannot_escape_into_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'production'
            self.seed(source)
            before = files(source)
            for runs, run_id in [(source, '1'), (source / 'nested', '1'),
                                 (Path(tmp) / 'tests', '../production')]:
                with self.assertRaises(ValueError):
                    preview(source, runs, run_id, '2026-10-03')
            self.assertEqual(files(source), before)

    @patch('tstocknews.preview.holiday_days', return_value=set())
    def test_cached_gap_is_not_silently_treated_as_holiday(self, calendar):
        from tstocknews.official import SourceError
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'production'
            day = self.seed(source)
            missing = sorted((source / 'days').glob('*.json.gz'))[-10]
            missing.unlink()
            before = files(source)
            with self.assertRaisesRegex(SourceError, 'Missing expected warm-up session'):
                preview(source, Path(tmp) / 'tests', 'gap', day)
            self.assertEqual(files(source), before)

    def test_resolved_run_directory_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, runs = Path(tmp) / 'production', Path(tmp) / 'tests'
            self.seed(source)
            runs.mkdir()
            try:
                (runs / 'escape').symlink_to(source, target_is_directory=True)
            except OSError:
                self.skipTest('Creating symlinks requires Windows privilege; Linux CI verifies')
            before = files(source)
            with self.assertRaises(ValueError):
                preview(source, runs, 'escape', '2026-10-03')
            self.assertEqual(files(source), before)

    def test_blocked_send_is_failed_action_when_required(self):
        with tempfile.TemporaryDirectory() as tmp, patch('tstocknews.cli.push', return_value={'status': 'QUOTA_BLOCKED'}):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--data-dir', tmp, 'send', '--require-sent']), 1)

    @patch('tstocknews.preview.holiday_days', return_value=set())
    def test_production_and_test_recommendations_match_for_same_inputs(self, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            source, formal = Path(tmp) / 'source', Path(tmp) / 'formal'
            day = self.seed(source)
            self.seed(formal)
            (formal / 'recommendations' / (day + '.json')).unlink()
            preview(source, Path(tmp) / 'tests', 'parity', day)
            with patch('tstocknews.cli.holiday_days', return_value=set()), patch('tstocknews.cli.sync'):
                daily(formal, Path(tmp) / 'formal-reports', day)
            production = read(formal / 'recommendations' / (day + '.json'))
            test = read(Path(tmp) / 'tests/parity/data/recommendations' / (day + '.json'))
            self.assertEqual(test['candidates'], production['candidates'])
            self.assertEqual(test['data_hash'], production['data_hash'])
            self.assertEqual(test['diagnostics'], production['diagnostics'])
            self.assertEqual(test['strategy_version'], production['strategy_version'])
            self.assertEqual(test['signals'][0]['rank'], production['signals'][0]['rank'])
            self.assertNotEqual(test['signals'][0]['signal_id'], production['signals'][0]['signal_id'])

    def test_trading_day_without_report_fails_but_closed_day_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            for status, expected in [('WARMUP_REQUIRED', 1), ('NON_TRADING_DAY', 0), ('REPORT_READY', 0)]:
                with patch('tstocknews.cli.daily', return_value={'status': status}):
                    self.assertEqual(main(['--data-dir', tmp, 'daily', '--require-report']), expected)

    @patch('tstocknews.preview.holiday_days', return_value=set())
    def test_financial_source_failure_cannot_be_reported_as_success(self, calendar):
        from tstocknews.official import SourceError
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            day = self.seed(source, financial=False)
            before = files(source)
            with patch('tstocknews.preview.universe_and_financials', side_effect=SourceError('source unavailable')):
                with self.assertRaises(SourceError):
                    preview(source, Path(tmp) / 'tests', 'failed', day)
            self.assertEqual(files(source), before)

    @patch('tstocknews.cli.holiday_days', return_value=set())
    @patch('tstocknews.cli.sync')
    def test_test_record_cannot_be_used_by_production(self, sync, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            day = self.seed(source)
            write(source / 'recommendations' / (day + '.json'), {'test_only': True, 'signals': []})
            with self.assertRaisesRegex(ValueError, 'Test recommendations'):
                daily(source, Path(tmp) / 'reports', day)

    def test_late_snapshot_is_test_only_and_production_stays_strict(self):
        from tstocknews.official import SourceError
        sessions = weekdays(125)
        day = sessions[-1]
        financial = {'observed_date': '2026-10-03', 'universe': [{'market': 'twse', 'symbol': '2330'}],
                     'fundamentals': [{'market': 'twse', 'symbol': '2330', 'eps': 1, 'revenue_yoy_3m': .2,
                                       'available_date': '2026-10-03'}]}
        with self.assertRaisesRegex(SourceError, 'Production cannot use'):
            recommendations(day, sessions, bars(sessions=sessions), [], financial)

    def test_complete_financial_source_loss_is_not_zero_qualified_stocks(self):
        from tstocknews.official import SourceError
        sessions = weekdays(125)
        financial = {'observed_date': sessions[-1], 'universe': [{'market': 'twse', 'symbol': '2330'}],
                     'fundamentals': [{'market': 'twse', 'symbol': '2330', 'eps': None,
                                       'revenue_yoy_3m': None, 'available_date': sessions[-1]}]}
        with self.assertRaisesRegex(SourceError, 'no usable EPS or revenue'):
            recommendations(sessions[-1], sessions, bars(sessions=sessions), [], financial)
