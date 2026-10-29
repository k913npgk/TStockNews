from copy import deepcopy
from datetime import date
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tstocknews import eps_improvement as eps
from tstocknews.official import SourceError
from tests.test_eps_facts import document,fact


def finance(n=1):
    universe=[{'symbol':str(1000+i),'market':'twse','name':'Test'} for i in range(n)]
    return {'observed_date':'2026-10-29','universe':universe,'fundamentals':[
        {**c,'eps':2,'fiscal_period':'2026-Q2','available_date':'2026-10-29'} for c in universe]}


def pool(financial):
    return {'candidates':deepcopy(financial['fundamentals'])}


def archive(symbols,prior='1'):
    stream=io.BytesIO()
    with ZipFile(stream,'w') as z:
        for symbol in symbols:
            z.writestr(f'tifrs-ci-cr-{symbol}-2026Q2.html',document([fact('2','cur'),fact(prior,'prior')]))
    return stream.getvalue()


class EPSIntegrationTests(unittest.TestCase):
    def test_download_rejects_non_zip_oversize_and_transport_failure(self):
        for raw in (b'<html>busy</html>',b'PK'+b'0'*10):
            with patch.object(eps,'urlopen',return_value=io.BytesIO(raw)),patch.object(eps,'MAX_ARCHIVE_BYTES',8):
                with self.assertRaises(SourceError):eps.download_archive('https://example.test/archive')
        with patch.object(eps,'urlopen',side_effect=OSError('unavailable')) as get:
            with self.assertRaises(SourceError):eps.download_archive('https://example.test/archive')
            self.assertEqual(get.call_count,1)

    def test_source_batch_cache_resume_and_original_financial_immutable(self):
        f=finance(2);before=deepcopy(f)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(eps,'full_c5',return_value=pool(f)),patch.object(eps,'today',return_value=date(2026,10,29)), \
                 patch.object(eps,'download_archive',return_value=archive(['1000','1001'])) as download:
                result=eps.prepare(root,'2026-10-29',[],[],[],f)
                self.assertEqual(download.call_count,1)
                self.assertTrue(all(r['eps_comparison_status']=='KNOWN' for r in result['fundamentals']))
                self.assertEqual(result['fundamentals'][0]['eps_prior_ytd'],1)
                with patch.object(eps,'today',return_value=date(2026,10,30)):
                    resumed=eps.prepare(root,'2026-10-29',[],[],[],f)
                self.assertEqual(resumed,result)
                self.assertEqual(download.call_count,1)
                self.assertEqual(len(list((root/'eps-filings').rglob('*.gz'))),2)
        self.assertEqual(f,before)

    def test_missing_or_mismatched_whole_source_is_fatal_not_zero_picks(self):
        for raw in (archive([]),archive(['1001'])):
            f=finance()
            with tempfile.TemporaryDirectory() as tmp,patch.object(eps,'full_c5',return_value=pool(f)), \
                 patch.object(eps,'today',return_value=date(2026,10,29)),patch.object(eps,'download_archive',return_value=raw):
                with self.assertRaisesRegex(SourceError,'No C5-eligible'):eps.prepare(Path(tmp),'2026-10-29',[],[],[],f)

    def test_current_value_conflict_and_historical_backfill_rejected(self):
        f=finance();f['fundamentals'][0]['eps']=99
        with tempfile.TemporaryDirectory() as tmp,patch.object(eps,'full_c5',return_value=pool(f)), \
             patch.object(eps,'download_archive',return_value=archive(['1000'])):
            with self.assertRaisesRegex(SourceError,'No C5-eligible'):eps.prepare(Path(tmp),'2026-10-29',[],[],[],f)
        f['observed_date']='2026-10-28'
        with tempfile.TemporaryDirectory() as tmp,patch.object(eps,'full_c5',return_value=pool(f)):
            with self.assertRaisesRegex(SourceError,'Historical snapshot'):eps.prepare(Path(tmp),'2026-10-28',[],[],[],f)

    def test_filter_precedes_top20_and_equal_eps_fails(self):
        f=finance(30)
        with tempfile.TemporaryDirectory() as tmp,patch.object(eps,'full_c5',return_value=pool(f)), \
             patch.object(eps,'download_archive',return_value=archive([c['symbol'] for c in f['universe']])):
            enriched=eps.prepare(Path(tmp),'2026-10-29',[],[],[],f)
        for r in enriched['fundamentals'][:10]:
            r['eps_prior_ytd']=2;r['eps_comparison']['eps_prior_ytd']=2
        original=pool(f)
        original['diagnostics']={'symbols':{r['market']+':'+r['symbol']:{'reasons':[]} for r in original['candidates']},'exclusion_counts':{}}
        result=eps.apply(original,enriched,'2026-10-29')
        self.assertEqual(result['eligible_count'],20)
        self.assertEqual(result['candidates'][-1]['symbol'],'1029')
        self.assertEqual(result['diagnostics']['exclusion_counts']['EPS_NOT_IMPROVED_YOY'],10)

    def test_late_or_wrong_period_is_excluded_even_with_known_flag(self):
        f=finance()
        with tempfile.TemporaryDirectory() as tmp,patch.object(eps,'full_c5',return_value=pool(f)), \
             patch.object(eps,'download_archive',return_value=archive(['1000'])):
            enriched=eps.prepare(Path(tmp),'2026-10-29',[],[],[],f)
        for field,value in [('period_start','2026-04-01'),('prior_period_end','2024-06-30')]:
            altered=deepcopy(enriched);altered['fundamentals'][0]['eps_comparison'][field]=value
            original=pool(f);original['diagnostics']={'symbols':{'twse:1000':{'reasons':[]}},'exclusion_counts':{}}
            self.assertEqual(eps.apply(original,altered,'2026-10-29')['eligible_count'],0)
        original=pool(f);original['diagnostics']={'symbols':{'twse:1000':{'reasons':[]}},'exclusion_counts':{}}
        self.assertEqual(eps.apply(original,enriched,'2026-10-28')['eligible_count'],0)


if __name__=='__main__':unittest.main()
