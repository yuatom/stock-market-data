"""Synthetic local Git/HTTP only. No production writes or provider requests."""
import copy
import json
import unittest
import test_append_git_chain as chain
import test_guarded_history_recovery as fixture
import backfill_market_data_tail as tail

class BoundedVolumeQuarantineTests(chain.AppendGitChainTests):
    def test_price_and_volume_rejection_has_durable_metadata_only_quarantine(self):
        result=self.run_append(reject_first=True)
        self.assertEqual(result['status'],'BLOCKED',result)
        self.assertEqual(result['requests_attempted'],1)
        self.assertEqual(result['budget_debits_confirmed'],1)
        self.assertEqual(result['not_attempted_symbols'],['BBB','CCC'])
        head=result['last_verified_commit_sha']
        base='data/market-data/series/daily/twelve_data_basic/AAA/'
        old=json.loads(fixture.git(self.repo,'show',self.base+':'+base+'_index.json'))
        index=json.loads(fixture.git(self.repo,'show',head+':'+base+'_index.json'))
        self.assertEqual(index['lineage_segments'][:-1],old.get('lineage_segments',[]))
        self.assertEqual(index['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        self.assertEqual(index['record_count'],len(self.old))
        for shard in old['shards']:
            self.assertEqual(fixture.git(self.repo,'rev-parse',self.base+':'+base+shard['path']),fixture.git(self.repo,'rev-parse',head+':'+base+shard['path']))
        for symbol in ['BBB','CCC']:
            p=f'data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json'
            self.assertEqual(fixture.git(self.repo,'rev-parse',self.base+':'+p),fixture.git(self.repo,'rev-parse',head+':'+p))
        self.assertEqual(fixture.git(self.store_remote,'rev-parse','refs/tags/market-data-read/'+head),head)

    def test_price_only_rejection_does_not_block_volume(self):
        self.new[-2]['volume']=self.old[-1]['volume']
        result=self.run_append(reject_first=True)
        self.assertEqual(result['status'],'BLOCKED',result)
        p='data/market-data/series/daily/twelve_data_basic/AAA/_index.json'
        self.assertEqual(fixture.git(self.repo,'rev-parse',self.base+':'+p),fixture.git(self.repo,'rev-parse',result['last_verified_commit_sha']+':'+p))

    def test_quarantine_stage_repetition_is_idempotent(self):
        item=dict(symbol='AAA',existing=self.old,index_relative='data/market-data/series/daily/twelve_data_basic/AAA/_index.json')
        rows=copy.deepcopy(self.new);rows[0]['high']+=0.01
        evidence=tail.volume_conflict_evidence(self.h,item,rows,'2026-10-16')
        _,first=tail.stage_volume_quarantine(self.h,self.repo,item,evidence,fixture.DAY)
        first_bytes=(self.repo/item['index_relative']).read_bytes()
        _,second=tail.stage_volume_quarantine(self.h,self.repo,item,evidence,fixture.DAY)
        self.assertTrue(first['annotation_added']);self.assertFalse(second['annotation_added'])
        self.assertEqual((self.repo/item['index_relative']).read_bytes(),first_bytes)

    def test_invalid_or_duplicate_volume_is_not_quarantine_evidence(self):
        item=dict(symbol='AAA',existing=self.old)
        rows=copy.deepcopy(self.old);rows[-1]['volume']=float('nan')
        self.assertIsNone(tail.volume_conflict_evidence(self.h,item,rows,'2026-10-16'))
        rows[-1]['volume']=-1
        self.assertIsNone(tail.volume_conflict_evidence(self.h,item,rows,'2026-10-16'))
        self.assertIsNone(tail.volume_conflict_evidence(self.h,item,self.new+[self.new[0]],'2026-10-16'))

    def test_quarantine_durability_unknown_stops_without_retry(self):
        from unittest.mock import patch
        original=tail.recovery.verify_outputs
        def verify(repo,sha,manifest,outputs,request_id,day,**kwargs):
            report_path=next((x['path'] for x in outputs if 'daily-history-tail-backfill-' in x['path']),None)
            if report_path:
                report=json.loads(fixture.git(repo,'show',sha+':'+report_path))
                if report.get('volume_quarantine'):
                    raise RuntimeError('SYNTHETIC_DURABILITY_UNKNOWN')
            return original(repo,sha,manifest,outputs,request_id,day,**kwargs)
        with patch.object(tail.recovery,'verify_outputs',side_effect=verify):
            result=self.run_append(reject_first=True)
        self.assertEqual(self.http_count,1)
        self.assertEqual(result['not_attempted_symbols'],['BBB','CCC'])
        self.assertEqual(result['stop']['failure_code'],'publication_unconfirmed',result)
        self.assertFalse(result['automatic_retry_allowed'])
        self.assertEqual(result['transactions'],[])

    def test_forged_quarantine_annotation_rejected_by_controller(self):
        from unittest.mock import patch
        import maintain_fixed_symbols as maintenance
        original=maintenance._verify_append_terminal
        def forged(*args,**kwargs):
            original_git=maintenance.a.git
            def git(repo,*command,**options):
                value=original_git(repo,*command,**options)
                if len(command)==2 and command[0]=='show' and command[1]==kwargs['sha']+':'+kwargs['path']:
                    report=json.loads(value)
                    if report.get('volume_quarantine'):
                        report['volume_quarantine']['annotation_added']=False
                        return json.dumps(report)
                return value
            with patch.object(maintenance.a,'git',side_effect=git):
                return original(*args,**kwargs)
        with patch.object(maintenance,'_verify_append_terminal',side_effect=forged):
            result=self.run_append(reject_first=True)
        self.assertEqual(self.http_count,1)
        self.assertIsNotNone(result['stop'])
        self.assertEqual(result['transactions'],[])
        self.assertEqual(result['not_attempted_symbols'],['BBB','CCC'])
