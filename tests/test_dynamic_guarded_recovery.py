"""Dynamic selection through real guarded publisher with synthetic histories."""
from __future__ import annotations
import copy
import json
from datetime import datetime
from unittest.mock import patch
from pathlib import Path

from test_guarded_history_recovery import RecoveryIntegrationTests, git, DAY, NOW


class DynamicGuardedRecoveryTests(RecoveryIntegrationTests):
    def setUp(self):
        super().setUp()
        _, self.discovery = self.new_repo('discovery')
        request={'schema_version':1,'request_id':'synthetic-request','requested_at':'2026-10-17T12:00:00-04:00',
            'trade_date':'2026-10-17','stage':'close','request_purpose':'opportunity_discovery',
            'research_repository':'yuatom/stock-dairy','research_repository_commit_sha':'a'*40,
            'market_data_contract_sha':'b'*40,'candidate_symbols':['DYA','DYB','DYC'],
            'transaction_id':'synthetic-close','research_universe_resolution_status':'resolved',
            'personal_state_proof':{'dynamic_state_read_sha':'c'*40,'blob_sha':'d'*40,'state_version':1,
                'authority_state':'canonical','content_hash_status':'canonical_sha256','content_sha256':'e'*64}}
        req=self.discovery/'requests/dynamic-candidate-request.json';req.parent.mkdir();req.write_text(json.dumps(request))
        git(self.discovery,'add','.');git(self.discovery,'commit','-m','request')
        source=git(self.discovery,'rev-parse','HEAD');git(self.discovery,'update-ref','refs/remotes/origin/discovery-requests',source)
        for symbol in ('DYA','DYB','DYC'):
            self.h.store.append_daily_bars(self.repo/'data/market-data',provider='twelve_data_basic',
                symbol=symbol,asset_class='stocks',bars=self.old,series_semantics='daily_regular_ohlcv',shard_record_limit=2)
        result={'schema_version':1,'request_id':request['request_id'],'request_purpose':request['request_purpose'],
            'research_repository_commit_sha':request['research_repository_commit_sha'],
            'market_data_contract_sha':request['market_data_contract_sha'],'candidate_symbols':request['candidate_symbols'],
            'mode':'discovery_close','snapshot_stage':'discovery_close','persistent_collection_membership':False,
            'daily_history_available':['DYC'],'daily_history_failures':[{'symbol':s,'reason':f'MarketDataConflict:conflicting bar for {s} 2026-10-15'} for s in ('DYA','DYB')]}
        path=self.repo/'data/market-data/collector-state/2026-10/2026-10-17-dynamic-candidate-close.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result))
        git(self.repo,'add','.');git(self.repo,'commit','-m','dynamic source');git(self.repo,'push','origin','main')
        self.base=git(self.repo,'rev-parse','HEAD')
        self.selector={'request_commit_sha':source,'request_blob_sha':git(self.discovery,'rev-parse',f'{source}:requests/dynamic-candidate-request.json'),
            'result_blob_sha':git(self.repo,'rev-parse',f'{self.base}:{path.relative_to(self.repo)}'),
            'targets':[{'symbol':s,'index_blob_sha':git(self.repo,'rev-parse',f'{self.base}:data/market-data/series/daily/twelve_data_basic/{s}/_index.json')} for s in ('DYA','DYB')]}

    def dynamic_recovery(self, http=None, selector=None):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW if tz is None else NOW.astimezone(tz)
        with patch.object(self.h.collectors,'datetime',Clock),patch.object(self.h.collectors,'_http_json',side_effect=http or self.payload):
            return self.m.recover(self.h,compute=self.compute,compute_sha=self.compute_sha,repo=self.repo,
                store_sha=self.base,day=DAY,request_id='123-1',config=self.config,universe=self.universe,
                access=self.access,key='SYNTHETIC',clock=lambda:NOW,dynamic_selector=self.selector if selector is None else selector,discovery_repo=self.discovery)

    def test_selected_dynamic_recovery_archives_and_leaves_unselected_unchanged(self):
        result=self.dynamic_recovery();self.assertEqual(result['status'],'PASS');self.assertEqual(result['recovered_count'],2)
        commit=result['store_commit_sha'];self.assertEqual(git(self.store_remote,'rev-parse','refs/tags/market-data-read/'+commit),commit)
        for symbol in ('DYA','DYB'):
            archive=f'data/market-data/recovery/{DAY}/twelve_data_basic/daily-baseline-rebase/{symbol}.json'
            self.assertEqual(json.loads(git(self.repo,'show',commit+':'+archive))['old_records'],self.old)
        for symbol in ('AAA','BBB','DYC'):
            path=f'data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json'
            self.assertEqual(git(self.repo,'rev-parse',self.base+':'+path),git(self.repo,'rev-parse',commit+':'+path))
        self.assertTrue(result['verified_required_outputs'])

    def test_invalid_selection_causes_no_debit_or_provider_call(self):
        selector=copy.deepcopy(self.selector);selector['targets'][1]['index_blob_sha']='0'*40
        with self.assertRaises(Exception):self.dynamic_recovery(selector=selector)
        self.assertEqual(self.http_count,0);self.assertEqual(git(self.store_remote,'rev-parse','refs/heads/main'),self.base)

    def test_failed_second_dynamic_target_never_publishes_partial_history(self):
        def fail_second(url, **kwargs):
            if self.http_count:raise RuntimeError('synthetic provider failure')
            return self.payload(url,**kwargs)
        with self.assertRaises(Exception):self.dynamic_recovery(http=fail_second)
        self.assert_no_remote_facts()

    def test_dynamic_admission_publishes_diagnostics_only(self):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW if tz is None else NOW.astimezone(tz)
        writer=self.a.StorePublisher(self.repo,self.base,DAY,'234-1')
        with patch.object(self.h.collectors,'datetime',Clock),patch.object(self.h.collectors,'_http_json',side_effect=self.payload):
            report,commit=self.a.execute(self.h,writer,compute=self.compute,compute_sha=self.compute_sha,day=DAY,
                request_id='234-1',config=self.config,universe=self.universe,access=self.access,key='SYNTHETIC',
                clock=lambda:NOW,dynamic_selector=self.selector,discovery_repo=self.discovery)
        self.assertEqual(report['status'],'PASS');self.assertEqual(report['target_count'],2)
        paths=git(self.repo,'diff','--name-only',self.base,commit).splitlines()
        self.assertTrue(all(p.startswith('data/market-data/collector-state/') for p in paths))
        self.assertEqual(report['dynamic_selection'],self.selector)

    def test_invalid_dynamic_admission_does_not_debit_or_write(self):
        selector=copy.deepcopy(self.selector);selector['result_blob_sha']='0'*40
        writer=self.a.StorePublisher(self.repo,self.base,DAY,'234-1')
        with patch.object(self.h.collectors,'_http_json') as http, self.assertRaises(self.a.AdmissionError):
            self.a.execute(self.h,writer,compute=self.compute,compute_sha=self.compute_sha,day=DAY,
                request_id='234-1',config=self.config,universe=self.universe,access=self.access,key='SYNTHETIC',
                clock=lambda:NOW,dynamic_selector=selector,discovery_repo=self.discovery)
        http.assert_not_called();self.assertEqual(git(self.store_remote,'rev-parse','refs/heads/main'),self.base)

    def test_dynamic_archive_collision_blocks_before_request_or_debit(self):
        path=self.repo/f'data/market-data/recovery/{DAY}/twelve_data_basic/daily-baseline-rebase/DYA.json'
        path.parent.mkdir(parents=True);path.write_text('{}');git(self.repo,'add','.');git(self.repo,'commit','-m','prior archive');git(self.repo,'push','origin','main')
        self.base=git(self.repo,'rev-parse','HEAD')
        with self.assertRaises(Exception):self.dynamic_recovery()
        self.assertEqual(self.http_count,0);self.assertEqual(git(self.store_remote,'rev-parse','refs/heads/main'),self.base)
