from pathlib import Path
from datetime import date
from types import SimpleNamespace
import json, os, subprocess, sys

source=Path(sys.argv[2]).resolve()
sys.path[:0] = [str(source), str(source/'scripts'), str(Path(sys.argv[3]).resolve())]
import maintain_fixed_symbols as n
import backfill_market_data_tail as t
import rebase_market_data_baseline as rb
import market_data_collectors as collector
import collection_universe as cu
from scripts import market_data_store as s
from scripts import private_fixed_tail_20261120 as profile
from private_fixed_tail_readback import verify as current

def no_socket(event,args):
    if event.startswith('socket.'): raise RuntimeError('offline fixture')
sys.addaudithook(no_socket)
os.environ.update(GIT_ALLOW_PROTOCOL='file',GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_NOSYSTEM='1',
 GIT_AUTHOR_NAME='Synthetic fixture', GIT_COMMITTER_NAME='Synthetic fixture',
 GIT_AUTHOR_EMAIL='fixture@example.invalid',GIT_COMMITTER_EMAIL='fixture@example.invalid')

root=Path(sys.argv[1]).resolve()
repo=root/'synthetic-work'
origin=root/'synthetic-origin'
repo.mkdir(); origin.mkdir()
def git(where,*args):
    return subprocess.check_output(['git','-C',str(where),*args],text=True,stderr=subprocess.PIPE).strip()
git(repo,'init','-q','--initial-branch=main')
git(origin,'init','--bare','-q','--initial-branch=main')
git(repo,'remote','add','origin',str(origin))

DAY='2026-11-23'; END='2026-11-20'; BASE='2026-11-19'; RUN='42-1'
COMPUTE='1'*40
P=n.r.PREFIX
prefix=f'{P}collector-state/2026-11/{DAY}'
BUD=prefix+'-twelve-data-budget.json'
PLAN=prefix+f'-daily-symbol-maintenance-{RUN}.json'
CLAIM='capture-evidence/'+'b'*64+'/FIXED-TAIL-1120/claim.json'
SCOPE=profile.SCOPE; PROVIDER=profile.PROVIDER; MINUTE=DAY+'T13:01Z'
u=cu.load_collection_universe(source/'config/collection-universe.json'); assets=dict(cu.daily_universe(u))
h=SimpleNamespace(store=s,collection=cu,collectors=collector,rebaser=rb)
def put(path,obj):
    p=repo/path;p.parent.mkdir(parents=True,exist_ok=True)
    # The approved hosted writer uses compact sorted ASCII for claim bytes;
    # the native market objects retain their normal Store canonical shape.
    if path==CLAIM:
        p.write_bytes((json.dumps(obj,sort_keys=True,separators=(",",":"))+"\n").encode("ascii"))
    else:
        p.write_bytes(s.canonical_bytes(obj))
def blob(path): return git(repo,'hash-object',path)
def commit(label,tag=True):
    git(repo,'add','-A')
    git(repo,'commit','-qm',label)
    sha=git(repo,'rev-parse','HEAD')
    if tag: git(repo,'tag','market-data-read/'+sha)
    return sha
def ledger(paths,credit,mode,request=RUN):
    outputs=[dict(path=p[len(P):],blob_sha=blob(p)) for p in sorted(paths)]
    put(BUD,dict(schema_version=1,request_id=request,trade_date=DAY,credits=credit,
                  minutes={MINUTE:credit} if credit else {},stage=mode,required_outputs=outputs))
    return [dict(path=P+x['path'],blob_sha=x['blob_sha']) for x in outputs]
def price(day, base):
    return dict(trade_date=day,open=base,close=base,low=base-1,high=base+1,volume=5)
targets=[]
for i,symbol in enumerate(SCOPE):
    idx=f'{P}series/daily/{PROVIDER}/{symbol}/_index.json'
    s.append_daily_bars(repo/P,provider=PROVIDER,symbol=symbol,asset_class=assets[symbol],
        identity_effective_from=cu.ticker_effective_at(u,symbol), bars=[price(BASE,100.+i)],series_semantics='daily_regular_ohlcv')
    targets.append(dict(symbol=symbol,index_blob_sha=blob(idx)))
selection=dict(schema_version=1,targets=targets)
old=commit('synthetic reviewed base')
put(CLAIM,dict(schema_version=1,kind='fixed_daily_tail_batch_claim',route='fixed_daily_tail_20261120',
    approval_commitment='b'*64,reviewed_store_sha=old,compute_sha=COMPUTE,execution_date=DAY,
    historical_cutoff=END,request_id=RUN,physical_attempt='d'*64))
op=commit('synthetic single-add claim')
assert git(repo,'rev-list','--parents','-n1',op)==f'{op} {old}'
assert git(repo,'diff-tree','--no-commit-id','--name-status','-r',op)==f'A\t{CLAIM}'
assert git(repo,'rev-parse',op+':data/market-data')==git(repo,'rev-parse',old+':data/market-data')
common=dict(compute_sha=COMPUTE,store_start_sha=op,execution_date=DAY,historical_cutoff=END,
            request_id=RUN,trade_date=DAY,automatic_retry_allowed=False)
put(PLAN,dict(common,mode=n.MODE,kind='append_only_batch_plan',status='PLANNED',fixed_selection=selection,
    maximum_provider_requests=10,overall_timeout_seconds=1800,volume_policy_scope='per_symbol',transactions=[],
    requests_attempted=0,budget_debits_confirmed=0,budget_debits_unconfirmed=0,published_count=0,
    last_verified_commit_sha=op, stop=None))
ledger([PLAN],0,n.MODE)
plan=commit('synthetic native-plan shape')
scenario=sys.argv[4] if len(sys.argv)>4 else 'terminal-before-checkpoint'
cursor=plan; rows=[]; witnesses=[]; stopped=None
for i,target in enumerate(targets[:4 if scenario.startswith('debit-') else 3],1):
    sym=target['symbol']
    child_path=prefix+f'-daily-history-tail-backfill-{RUN}-{sym}.json'
    checkpoint_path=prefix+f'-daily-symbol-maintenance-{RUN}-{sym}.json'
    index=f'{P}series/daily/{PROVIDER}/{sym}/_index.json'
    oldindex=json.loads((repo/index).read_bytes())
    existing=s.read_daily_series(repo/P,provider=PROVIDER,symbol=sym)
    entry=dict(symbol=sym,existing=existing,effective=oldindex['identity_effective_from'],old_index=oldindex,
               index_relative=index,old_files={x:blob(x) for x in n.r.series_paths(repo,PROVIDER,sym,oldindex)},
               target_records=len(existing),outputsize=473)
    synth=[dict(x) for x in existing]+[price(END,200.+i)]
    if i==2: synth[0]['volume']=7
    # Exact phases and counters from backfill_market_data_tail.py:381–425.
    report=dict(schema_version=1,mode=t.MODE,compute_sha=COMPUTE,store_start_sha=cursor,
         trade_date=DAY,execution_date=DAY,historical_cutoff=END,request_id=RUN,target_count=1,
         started_at='2026-11-23T13:01:00+00:00',market_fact_authority=False,
         production_recovery_accepted=False,fixed_selection=dict(schema_version=1,targets=[target]),
         timing_diagnostics=[])
    put(child_path,dict(report,status='IN_PROGRESS',requests_attempted=0,budget_debits=0))
    ledger([child_path],i-1,t.MODE)
    initial=commit('native lifecycle: initial report and manifest')
    ledger([child_path],i,t.MODE)
    if i==4 and scenario=='debit-wrong-report-blob':
        bad=json.loads((repo/BUD).read_bytes())
        bad['required_outputs'][0]['blob_sha']='0'*40
        put(BUD,bad)
    debit=commit('native lifecycle: ledger-only durable debit',tag=not(i==4 and scenario=='debit-no-tag'))
    assert git(repo,'diff-tree','--no-commit-id','--name-status','-r',debit)==f'M\t{BUD}'
    if i==4:
        stopped=dict(initial=initial,debit=debit,report=child_path)
        assert git(repo,'diff-tree','--no-commit-id','--name-status','-r',initial,'--',child_path)==f'A\t{child_path}'
        break
    put(child_path,dict(report,status='RESPONSE_RETAINED_UNQUALIFIED',requests_attempted=1,budget_debits=1,
        retained_normalized_response=dict(symbol=sym,records=synth,authority='normalized_not_raw_http')))
    ledger([child_path],i,t.MODE)
    retained=commit('native lifecycle: retained report and manifest')
    t.qualify_tail(h,entry,synth,END,price_only=True)
    outputs=t.stage_tail(h,repo,[entry],DAY,END)
    put(n.r.INVENTORY,dict(synthetic_inventory=True,step=i))
    put(child_path,dict(report,status='CANDIDATE_VALIDATED',requests_attempted=1,budget_debits=1,
        results=[entry['summary']],budget_parent=retained,publication_requires_terminal_receipt=True))
    outputs=ledger(outputs|{child_path,n.r.INVENTORY},i,t.MODE)
    child=commit('synthetic native terminal shape')
    row=dict(symbol=sym,status='PASS',store_start_sha=cursor,store_commit_sha=child,result_state_path=child_path,
             volume_policy=entry['summary']['volume_policy'])
    cp=None
    if i<=2 or scenario.startswith('debit-'):
        put(checkpoint_path,dict(common,mode=n.MODE,kind='append_only_batch_checkpoint',status='PROGRESS_VALIDATED',
             batch_manifest_commit_sha=plan,batch_manifest_path=PLAN,transactions=rows+[row],
             last_verified_commit_sha=child,requests_attempted=i,budget_debits_confirmed=i,
             budget_debits_unconfirmed=0,published_count=i,quarantined_symbols=[],stop=None))
        ledger([checkpoint_path],i,n.MODE)
        cp=commit('synthetic native checkpoint shape')
        rows.append(dict(row,checkpoint_commit_sha=cp, checkpoint_path=checkpoint_path))
    witnesses.append(dict(row=row,checkpoint=cp,checkpoint_path=checkpoint_path,outputs=outputs,initial=initial,debit=debit,retained=retained))
    cursor=cp or child
# EOD-style unrelated work moves main and even the shared ledger; no new target
# row is attributed retroactively to the stopped run.
other=prefix+'-previous_session_eod-SYNTHETIC-unrelated.json'
put(other,dict(mode='previous_session_eod',synthetic=True,request_id='99-1'))
ledger([other],5 if stopped else 4,'previous_session_eod','99-1')
latest=commit('unrelated synthetic EOD')
git(repo,'push','-q','origin','HEAD:refs/heads/main','--tags')
available=set()
n._metadata_proof(repo,plan,op,PLAN,DAY,RUN)
for i, witness in enumerate(witnesses):
    w=witness['row']; target=targets[i]; available.add(w['symbol'])
    child_before=n._immutable_budget(repo,w['store_start_sha'],BUD)
    child_report=n._verify_append_terminal(h,repo,start=w['store_start_sha'],sha=w['store_commit_sha'],
       path=w['result_state_path'],day=DAY,cutoff=END,request_id=RUN,symbol=w['symbol'],
       selector=dict(schema_version=1,targets=[target]),before=child_before,remaining=targets,
       passed=available,compute_sha=COMPUTE,result=dict(status='PASS',appended_count=1,
                                                       verified_required_outputs=witness['outputs']))
    assert child_report['results'][0]['new_records']==1
    if witness['checkpoint']: n._metadata_proof(repo,witness['checkpoint'],w['store_commit_sha'],witness['checkpoint_path'],DAY,RUN)
    else: assert not git(repo,'ls-tree','--name-only',latest,'--',witness['checkpoint_path'])
    git(repo,'merge-base','--is-ancestor',w['store_commit_sha'],latest)
    print(f"{w['symbol']} durable_price=1 debit=1 checkpoint={'verified' if witness['checkpoint'] else 'absent'} volume={w['volume_policy']}")
results=dict(common,status='PARTIAL',batch_manifest_commit_sha=plan,batch_manifest_path=PLAN,transactions=rows,
             requests_attempted=3,budget_debits_confirmed=3,budget_debits_unconfirmed=0,published_count=3,
             not_attempted_symbols=list(SCOPE[3:]),quarantined_symbols=[],last_verified_commit_sha=witnesses[-1]['row']['store_commit_sha'])
try: current(h=h,repo=repo,result=results,compute_sha=COMPUTE,store_sha=op,execution_date=DAY,
              historical_cutoff=END,request_id=RUN,fixed_selector=selection)
except ValueError: print('PR110_CURRENT_PARTIAL_VERIFY_REJECTED')
else: raise AssertionError('current full reader accepted partial')
last_child=witnesses[-1]['row']['store_commit_sha']
try: n._remote_exact(repo,last_child)
except n.r.RecoveryError: print('PR110_CURRENT_REMOTE_MAIN_EQUALITY_REJECTS_LATER_EOD')
else: raise AssertionError('remote main not moved')
print('synthetic scenario:',scenario)
(root/'LOCATORS.json').write_text(json.dumps(dict(reviewed=old,claim=op,claim_path=CLAIM,plan=plan,plan_path=PLAN,
    later_et_main=latest,request_id=RUN,execution_date=DAY,historical_cutoff=END,compute_sha=COMPUTE,selector=selection,
    children=witnesses,stopped=stopped),indent=2,sort_keys=True)+'\n')
