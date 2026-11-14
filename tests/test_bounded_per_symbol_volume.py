"""Synthetic per-symbol bounded append checks; no provider/production access."""
import copy
import importlib
import json
import unittest
from scripts import backfill_market_data_tail as tail

class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.h=importlib.import_module('admit_market_data_baseline').load_helpers()
        self.old=[dict(trade_date='2026-11-11',open=10.0,high=12.0,low=9.0,close=11.0,volume=100.0)]
        self.fetched=copy.deepcopy(self.old)+[dict(self.old[0],trade_date='2026-11-12')]
    def item(self,segments=None):
        return dict(symbol='TME',existing=copy.deepcopy(self.old),effective=None,target_records=1,outputsize=3,
                    old_index=dict(lineage_segments=segments or []))
    def qualify(self,item=None,rows=None):
        item=item or self.item();tail.qualify_tail(self.h,item,rows or self.fetched,'2026-11-12',price_only=True)
        return item,tail.tail_lineage(item,'2026-11-14','2026-11-12')
    def test_clean_and_numeric_equivalent_keep_volume(self):
        for rows in [self.fetched,[dict(self.fetched[0],volume=100),self.fetched[1]]]:
            item,lineage=self.qualify(rows=rows)
            self.assertEqual(lineage['ingest_kind'],tail.MODE)
            self.assertEqual(lineage['volume_policy'],'PRESERVED_PROVIDER_VOLUME')
            self.assertEqual(json.loads(item['price_only_evidence'][1])['volume_policy'],lineage['volume_policy'])
            self.assertEqual(item['summary']['volume_policy'],lineage['volume_policy'])
    def test_actual_conflict_and_existing_block_preserve_block(self):
        changed=copy.deepcopy(self.fetched);changed[0]['volume']=101.0
        for item,rows in [(self.item(),changed),(self.item([dict(volume_policy='BLOCKED_UNRESOLVED')]),self.fetched),
                          (self.item([dict(ingest_kind='collector_price_only_revision_v1')]),self.fetched)]:
            item,lineage=self.qualify(item,rows)
            self.assertEqual(lineage['ingest_kind'],'collector_price_only_revision_v1')
            self.assertEqual(lineage['volume_policy'],'BLOCKED_UNRESOLVED')
            self.assertEqual(item['summary']['volume_policy'],'BLOCKED_UNRESOLVED')
    def test_price_metadata_rejection_unchanged(self):
        for update in [dict(high=12.1),dict(source_timestamp='different')]:
            rows=copy.deepcopy(self.fetched);rows[0].update(update)
            with self.assertRaises(Exception):self.qualify(rows=rows)
    def test_malformed_or_missing_old_lineage_fails_closed(self):
        for index in [None,dict(lineage_segments='bad'),dict(lineage_segments=[None])]:
            item=self.item();item['old_index']=index
            with self.assertRaises(Exception):self.qualify(item)

import test_append_git_chain as chain
import test_guarded_history_recovery as fixture
from urllib.parse import urlparse,parse_qs
from unittest.mock import patch
import maintain_fixed_symbols as maintenance

class MixedGitChainTests(chain.AppendGitChainTests):
    def test_mixed_symbols_no_volume_policy_cross_talk(self):
        original=self.payload
        def payload(url,**kwargs):
            result=original(url,**kwargs)
            if parse_qs(urlparse(url).query)['symbol'][0] in ('AAA','CCC'):
                for row in result['values']:
                    if row['datetime']=='2026-10-15':row['volume']='100.00'
            return result
        with patch.object(self,'payload',side_effect=payload):result=self.run_append()
        self.assertEqual(result['status'],'PASS',result)
        self.assertEqual(self.http_count,3)
        policies=['PRESERVED_PROVIDER_VOLUME','BLOCKED_UNRESOLVED','PRESERVED_PROVIDER_VOLUME']
        self.assertEqual([x['volume_policy'] for x in result['transactions']],policies)
        head=result['last_verified_commit_sha']
        for symbol,policy in zip(('AAA','BBB','CCC'),policies):
            index,rows,_=maintenance._immutable_series(self.repo,head,symbol,self.h.store)
            self.assertEqual(rows[:len(self.old)],self.old)
            segment=index['lineage_segments'][-1]
            self.assertEqual(segment['volume_policy'],policy)
            expected_kind='collector_price_only_revision_v1' if policy=='BLOCKED_UNRESOLVED' else tail.MODE
            self.assertEqual(segment['ingest_kind'],expected_kind)
            evidence=json.loads(fixture.git(self.repo,'show',head+':data/market-data/'+segment['normalized_evidence_path']))
            self.assertEqual(evidence['volume_policy'],policy)
        plan=json.loads(fixture.git(self.repo,'show',result['batch_manifest_commit_sha']+':'+result['batch_manifest_path']))
        self.assertEqual(plan['volume_policy_scope'],'per_symbol');self.assertEqual(plan['overall_timeout_seconds'],1800)
        self.assertNotIn('volume_policy',plan)

    def test_independent_validator_rejects_forged_clean_lineage(self):
        original=maintenance._verify_append_terminal
        def forged(*args,**kwargs):
            original_series=maintenance._immutable_series
            def series(repo,sha,symbol,store):
                index,rows,paths=original_series(repo,sha,symbol,store)
                if sha==kwargs['sha'] and symbol==kwargs['symbol']:
                    index=copy.deepcopy(index);segment=index['lineage_segments'][-1]
                    segment['ingest_kind']=tail.MODE;segment['volume_policy']='PRESERVED_PROVIDER_VOLUME'
                return index,rows,paths
            with patch.object(maintenance,'_immutable_series',side_effect=series):return original(*args,**kwargs)
        with patch.object(maintenance,'_verify_append_terminal',side_effect=forged):result=self.run_append()
        self.assertIsNotNone(result['stop']);self.assertEqual(self.http_count,1)
        self.assertEqual(result['not_attempted_symbols'],['BBB','CCC'])

    def test_no_new_date_actual_volume_conflict_is_durably_quarantined(self):
        import backfill_market_data_tail as transaction
        self.new=self.new[:-1]
        from datetime import datetime
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return fixture.NOW if tz is None else fixture.NOW.astimezone(tz)
        with patch.object(self.h.collectors,'datetime',Clock),patch.object(self.h.collectors,'_http_json',side_effect=self.payload):
            with self.assertRaises(transaction.RecoveryError) as caught:
                transaction.backfill(self.h,compute=self.compute,compute_sha=self.compute_sha,
                    repo=self.repo,store_sha=self.base,execution_date=fixture.DAY,historical_cutoff='2026-10-15',
                    request_id='976-1',config=self.config,universe=self.universe,access=self.access,key='SYNTHETIC',
                    fixed_selector={'schema_version':1,'targets':self.selector['targets'][:1]},
                    clock=lambda:fixture.NOW,symbol_transaction='AAA')
        self.assertEqual(caught.exception.code,'replacement_rejected')
        self.assertIsNotNone(caught.exception.confirmed_terminal)
        head=caught.exception.confirmed_terminal.store_commit_sha
        index,rows,_=maintenance._immutable_series(self.repo,head,'AAA',self.h.store)
        self.assertEqual(rows,self.old)
        self.assertEqual(index['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        self.assertEqual(index['lineage_segments'][-1]['ingest_kind'],'volume_conflict_quarantine_v1')
        self.assertEqual(self.http_count,1)
