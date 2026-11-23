import json, os, subprocess, sys, tempfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from private_fixed_tail_readback import native, verify

def test_exact_private_readback_rejects_volume_upgrade_or_missing_tag():
    # Native child receipt boundary is substituted; metadata/tree/tag checks
    # still run against real file-only Git. No provider or real Store.
    env=dict(GIT_ALLOW_PROTOCOL="file",GIT_CONFIG_GLOBAL="/dev/null",GIT_CONFIG_NOSYSTEM="1",
             GIT_AUTHOR_NAME="Fixture",GIT_COMMITTER_NAME="Fixture",
             GIT_AUTHOR_EMAIL="fixture@example.invalid",GIT_COMMITTER_EMAIL="fixture@example.invalid")
    with patch.dict(os.environ,env):
        P = native.r.PREFIX
        day='2026-11-23'; cutoff='2026-11-20'; run='42-1'; compute='a'*40
        prefix=P+'collector-state/2026-11/'+day
        budget=prefix+'-twelve-data-budget.json'
        plan_path=prefix+'-daily-symbol-maintenance-'+run+'.json'
        targets=[{'symbol':'S'+str(i),'index_blob_sha':'b'*40} for i in range(10)]
        selector={'schema_version':1,'targets':targets}
        def git(repo,*args):
            return subprocess.check_output(['git','-C',str(repo),*args], text=True, stderr=subprocess.DEVNULL).strip()
        def write(repo,path,val):
            p=repo/path; p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(val)+'\n')
        def commit(repo,path,val,credits,mode):
            write(repo,path,val)
            entries=[{'path':path[len(P):],'blob_sha':git(repo,'hash-object',path)}]
            write(repo,budget,dict(credits=credits,minutes={},schema_version=1,request_id=run,
                 trade_date=day,stage=mode,required_outputs=entries))
            git(repo,'add','--',path,budget)
            git(repo,'commit','-qm','fixture')
            sha=git(repo,'rev-parse','HEAD')
            git(repo,'tag','market-data-read/'+sha)
            return sha
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); origin=root/'origin'; repo=root/'repo'; origin.mkdir(); repo.mkdir()
            git(origin,'init','-q','--bare','--initial-branch=main')
            git(repo,'init','-q','--initial-branch=main')
            git(repo,'remote','add','origin',str(origin))
            operational=commit(repo,P+'fixture.json',{'fixture':True},0,native.MODE)
            common=dict(compute_sha=compute,store_start_sha=operational,execution_date=day,
                        historical_cutoff=cutoff,request_id=run,automatic_retry_allowed=False)
            plan=commit(repo,plan_path,dict(common,mode=native.MODE,trade_date=day,kind='append_only_batch_plan',
                status='PLANNED',fixed_selection=selector,maximum_provider_requests=10, overall_timeout_seconds=1800,
                volume_policy_scope='per_symbol',transactions=[],requests_attempted=0,budget_debits_confirmed=0,
                budget_debits_unconfirmed=0,published_count=0,last_verified_commit_sha=operational,stop=None),0,native.MODE)
            cursor=plan; rows=[]; durable={}
            for n,target in enumerate(targets,1):
                symbol=target['symbol']; childpath=f'{prefix}-daily-history-tail-backfill-{run}-{symbol}.json'
                cppath=f'{prefix}-daily-symbol-maintenance-{run}-{symbol}.json'
                policy='BLOCKED_UNRESOLVED' if n==2 else 'PRESERVED_PROVIDER_VOLUME'
                child=commit(repo,childpath,{'synthetic':True},n,'daily_history_tail_backfill')
                durable[child]=(childpath,policy)
                row=dict(symbol=symbol,status='PASS',store_start_sha=cursor,store_commit_sha=child,
                         result_state_path=childpath,volume_policy=policy)
                cursor=commit(repo,cppath,dict(common,mode=native.MODE,trade_date=day,kind='append_only_batch_checkpoint',
                       status='PROGRESS_VALIDATED',batch_manifest_commit_sha=plan,batch_manifest_path=plan_path,
                       transactions=rows+[row],last_verified_commit_sha=child,requests_attempted=n,
                       budget_debits_confirmed=n,budget_debits_unconfirmed=0,published_count=n,quarantined_symbols=[],stop=None),n,native.MODE)
                rows.append(dict(row,checkpoint_commit_sha=cursor,checkpoint_path=cppath))
            git(repo,'push','-q','origin','HEAD:refs/heads/main','--tags')
            result=dict(common,status='PASS',batch_manifest_commit_sha=plan,batch_manifest_path=plan_path,
                        requests_attempted=10,budget_debits_confirmed=10,budget_debits_unconfirmed=0,published_count=10,
                        not_attempted_symbols=[],quarantined_symbols=[],stop=None,transactions=rows,last_verified_commit_sha=cursor)
            def child_boundary(h,repo,**kw):
                path,policy=durable[kw['sha']]
                assert kw['path']==path and kw['result']['verified_required_outputs'][0]['path']==path
                return {'results':[dict(new_records=1,last_trade_date=cutoff,volume_policy=policy)]}
            def runverify():
                return verify(h=None,repo=repo,result=result,compute_sha=compute,store_sha=operational,
                      execution_date=day,historical_cutoff=cutoff,request_id=run,fixed_selector=selector)
            with patch.object(native,'_verify_append_terminal',side_effect=child_boundary) as child:
                assert runverify() is True and child.call_count==10
                rows[1]['volume_policy']='PRESERVED_PROVIDER_VOLUME'
                try: runverify()
                except ValueError: pass
                else: raise AssertionError('claimed volume upgrade accepted')
                rows[1]['volume_policy']='BLOCKED_UNRESOLVED'
                git(origin,'update-ref','-d','refs/tags/market-data-read/'+cursor)
                try: runverify()
                except Exception: pass
                else: raise AssertionError('missing durability ref accepted')
        print('PASS real Git plan/checkpoint/tag readback and early reject tampered volume/missing tag; terminal verifier explicitly stubbed')


if __name__ == '__main__':
    test_exact_private_readback_rejects_volume_upgrade_or_missing_tag()
