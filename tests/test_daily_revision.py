import copy
import unittest
from scripts.eleven_symbol_append import classify_daily_overlap

class DailyRevisionTests(unittest.TestCase):
    def test_numeric_format_volume_and_price(self):
        old=dict(trade_date='2026-11-04',open=10,high=12,low=9,close=11,volume=100)
        cases=[(dict(old,open='10.00'), 'EQUIVALENT'),(dict(old,volume=101),'VOLUME_ONLY'),(dict(old,open=10.01),'PRICE_REVISION'),(dict(old,source_timestamp='unknown'),'METADATA_REVISION')]
        before=copy.deepcopy(old)
        for new,status in cases:
            with self.subTest(status=status):
                r=classify_daily_overlap(old,new)
                self.assertEqual(r['classification'],status)
                self.assertEqual(r['price_eligible'],status in ('EQUIVALENT','VOLUME_ONLY'))
                self.assertEqual(r['volume_policy'],'BLOCKED_UNRESOLVED')
        self.assertEqual(old,before)

    def test_invalid_and_date_mismatch(self):
        old=dict(trade_date='2026-11-04',open=10,high=12,low=9,close=11,volume=100)
        for key,val in [('open',float('nan')),('volume',-1),('high',8),('trade_date','2026-11-05')]:
            with self.subTest(key=key):
                self.assertFalse(classify_daily_overlap(old,dict(old,**{key:val}))['price_eligible'])

class CollectorIntegrationTests(unittest.TestCase):
    def test_opt_in_flow_and_unchanged_legacy(self):
        import os
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from contextlib import ExitStack
        from types import SimpleNamespace
        from scripts import market_data_collectors as c
        old=dict(trade_date='2026-11-04',open=10,high=12,low=9,close=11,volume=100)
        for policy,change,expected in [(None,dict(volume=101),False),('price_only_volume_blocked_v1',dict(volume=101),True),('price_only_volume_blocked_v1',dict(open=10.01),False),('price_only_volume_blocked_v1',dict(__new_high=8),False)]:
            with self.subTest(policy=policy,change=change), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root=Path(tmp); index=root/'index.json';index.write_text('{}')
                config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},'twelve_data_daily':{'history_outputsize':64,'bootstrap_minimum_records':20}}}
                if policy:config['collector']['twelve_data_daily']['overlap_revision_policy']=policy
                new=[dict(old,**{k:v for k,v in change.items() if k!='__new_high'}),dict(old,trade_date='2026-11-05')]
                if '__new_high' in change:new[1]['high']=change['__new_high']
                stack.enter_context(patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'synthetic-no-network'}))
                mocks={
                    '_daily_universe':[('AMD','stock')], '_load_daily_basis_owner':{},
                    'assert_legacy_basis':None, 'wait_for_twelve_budget':None,
                    'consume_twelve_credit':None, 'fetch_twelve_daily':new,
                    '_identity_effective_date':None, '_series_index_path':index,
                    'read_daily_series':[old], '_conflict_record':{},
                    '_write_private_eod_diagnostic':None,
                    'quarantine_daily_volume':True,
                    'append_daily_bars':SimpleNamespace(changed=True)}
                active={k:stack.enter_context(patch.object(c,k,return_value=v)) for k,v in mocks.items()}
                result=c.collect_previous_session_eod(trade_date='2026-11-06',store_root=root,watchlist={},completeness={},config=config,access={})
                self.assertEqual(active['append_daily_bars'].called,expected)
                evidence=list(root.glob('collector-state/**/*.json'))
                self.assertEqual(len(evidence),1 if policy else 0)
                if policy:self.assertEqual(json.loads(evidence[0].read_text())['records'],new)
                if expected:
                    call=active['append_daily_bars'].call_args.kwargs
                    self.assertEqual(call['lineage']['volume_policy'],'BLOCKED_UNRESOLVED')
                    self.assertEqual(call['bars'],[new[-1]])
                self.assertEqual(old['volume'],100)

class FiniteTailTests(unittest.TestCase):
    def test_shared_classifier_exact_tail_and_default(self):
        import importlib
        from scripts import backfill_market_data_tail as tail
        h=importlib.import_module('admit_market_data_baseline').load_helpers()
        old=[dict(trade_date='2026-11-04',open=10.,high=12.,low=9.,close=11.,volume=100.)]
        fetched=[dict(old[0],volume=101.),dict(old[0],trade_date='2026-11-05')]
        def item():return dict(symbol='AMD',existing=copy.deepcopy(old),target_records=1,effective=None,outputsize=2)
        with self.assertRaises(Exception):tail.qualify_tail(h,item(),fetched,'2026-11-05')
        candidate=item();tail.qualify_tail(h,candidate,fetched,'2026-11-05',price_only=True)
        self.assertEqual(candidate['rows'][0],old[0])
        self.assertEqual(candidate['new_rows'],[fetched[-1]])
        lineage=tail.tail_lineage(candidate,'2026-11-06','2026-11-05')
        self.assertEqual(lineage['volume_policy'],'BLOCKED_UNRESOLVED')
        self.assertEqual(lineage['ingest_kind'],'collector_price_only_revision_v1')
        fetched[0]['open']=10.01
        with self.assertRaises(Exception):tail.qualify_tail(h,item(),fetched,'2026-11-05',price_only=True)

class AppendControllerTests(unittest.TestCase):
    def test_success_then_unknown_stops_without_rebase(self):
        from unittest.mock import patch
        from contextlib import ExitStack
        from pathlib import Path
        from types import SimpleNamespace
        import maintain_fixed_symbols as m
        import backfill_market_data_tail as t
        selected=[dict(symbol=s,index_blob_sha='a'*40) for s in ('AMD','GLD','IBIT')]
        config={'collector':{'twelve_data_daily':{'bounded_overlap_revision_policy':'price_only_volume_blocked_v1','settlement_maturity':{'minimum_next_day_et':'07:00'}}}}
        success=dict(status='PASS',store_commit_sha='b'*40,result_state_path='synthetic',requests_attempted=1,budget_debits=1)
        with ExitStack() as stack:
            for owner,name,value in [(m,'_clean',None),(m,'_remote_exact',None),(m,'_adopt',None),(m,'_budget',{'credits':0,'minutes':{}}),(m,'_publish_metadata',('c'*40,'synthetic')),(m,'_verify_append_terminal',{}),(m.r,'source_guard',None),(t,'validate_dates','2026-11-12')]:
                stack.enter_context(patch.object(owner,name,return_value=value))
            stack.enter_context(patch.object(m,'resolve_fixed_targets',side_effect=lambda *a,**k:k['selector']['targets']))
            legacy=stack.enter_context(patch.object(m.r,'recover',side_effect=AssertionError('rebase must never be called')))
            transact=stack.enter_context(patch.object(t,'backfill',side_effect=[success,m.r.RecoveryError('publication_unconfirmed')]))
            result=m.maintain_append(SimpleNamespace(),compute=Path('.'),compute_sha='d'*40,repo=Path('.'),store_sha='e'*40,execution_date='2026-11-13',historical_cutoff='2026-11-12',request_id='123-1',config=config,universe={},access={},key='synthetic',fixed_selector={'schema_version':1,'targets':selected})
            self.assertEqual(transact.call_count,2)
            self.assertFalse(legacy.called)
            self.assertEqual(result['status'],'PARTIAL')
            self.assertEqual(result['published_count'],1)
            self.assertEqual(result['not_attempted_symbols'],['IBIT'])
            self.assertEqual(result['stop']['failure_code'],'publication_unconfirmed')
