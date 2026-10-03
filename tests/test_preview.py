import io
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_engine import bars, weekdays
from tstocknews.cli import main
from tstocknews.preview import preview, target_session
from tstocknews.storage import read, write


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class PreviewTests(unittest.TestCase):
    def seed(self, root, financial=True):
        sessions = weekdays(125)
        day = sessions[-1]
        for px in bars(sessions=sessions):
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
            self.assertFalse((runs / 'run-1/data/recommendations').exists())
            self.assertFalse((runs / 'run-1/data/performance').exists())
            text = (runs / 'run-1/reports' / (day + '.md')).read_text(encoding='utf-8')
            self.assertIn('【測試報告｜不計入績效】', text)
            self.assertNotIn('歷次推薦績效', text)
            with patch('tstocknews.preview.target_session', side_effect=AssertionError('must reuse')):
                self.assertEqual(preview(source, runs, 'run-1', day), result)
            with self.assertRaises(ValueError):
                preview(source, runs, 'run-1', '2026-10-02')
            self.assertEqual(preview(source, runs, 'run-2', day)['selected'], 1)

    @patch('tstocknews.preview.holiday_days', return_value=set())
    def test_missing_historical_snapshot_is_unknown_not_zero_or_latest_backfill(self, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            source, runs = Path(tmp) / 'production', Path(tmp) / 'tests'
            day = self.seed(source, financial=False)
            write(source / 'financials' / '2026-10-03.json.gz', {'observed_date': '2026-10-03'})
            before = files(source)
            with patch('tstocknews.preview.universe_and_financials', side_effect=AssertionError('future backfill')):
                result = preview(source, runs, 'missing', day)
            self.assertEqual(result['analysis_status'], 'INCOMPLETE')
            self.assertIsNone(result['selected'])
            self.assertIn('HISTORICAL_FINANCIAL_SNAPSHOT_MISSING', result['warnings'])
            self.assertEqual(files(source), before)
            self.assertFalse((runs / 'missing/screen.json').exists())

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
