"""Synthetic fixtures only. Initial read authority never acquires or publishes."""
import copy,importlib.util
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pytest
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_store import LocalGitStore
from scripts.emit_basis_consumer_delivery import build_delivery,encode_delivery,WORKFLOW
from test_daily_basis_updates import git


def fixture(tmp_path,*,receipt_until='2026-09-10T00:00:00Z'):
    # Reuse the actual single-response seed, not a daily transition/claim.
    from daily_basis_hosted_fixture import seed
    repo,g,_,days,_,_=seed(tmp_path/'seed')
    store=LocalGitStore(repo);selector=g['initial_selector'];now=datetime(2026,9,9,21,tzinfo=timezone.utc)
    # Match retained one-shot evidence: the terminal binds separate literal raw
    # bytes and their durable source commit, not just embedded envelope bytes.
    sp=selector;adpath=B.admission_path(B.PROVIDER,'SOXX',sp['admission_blob_sha'])
    admission=B._parse(store.read(sp['read_sha'],adpath));tp=admission['workflow_evidence']['terminal_ref']['path']
    terminal=B._parse(store.read(sp['read_sha'],tp));env=store.read(sp['read_sha'],admission['envelope_ref']['path'])
    snap=B._parse(store.read(sp['read_sha'],B.snapshot_path(B.PROVIDER,'SOXX',sp['basis_id'])))
    raw=B.verify_envelope(env,snap['envelope_sha256'])['raw_response'];rp='capture-evidence/genesis/response.raw'
    with store.lock():
        retained=store.publish(store.head(),{rp:raw},replace=set())
        terminal['receipts']=[dict(path=rp,blob_sha=B.blob_sha(raw),sha256=B.sha(raw),store_commit_sha=retained,durability_ref='refs/tags/market-data-read/'+retained,market_qualified=False,production_activation=False)]
        terminal_raw=B.encode(terminal);admission['workflow_evidence']['terminal_ref']['blob_sha']=B.blob_sha(terminal_raw)
        ar=B.encode(admission);catalog={k:v for k,v in selector.items() if k!='read_sha'};catalog['admission_blob_sha']=B.blob_sha(ar)
        h=store.publish(store.head(),{tp:terminal_raw,B.admission_path(B.PROVIDER,'SOXX',B.blob_sha(ar)):ar,B.catalog_path(B.PROVIDER,'SOXX'):B.encode(catalog)},replace={tp,B.catalog_path(B.PROVIDER,'SOXX')})
        selector=dict(catalog,read_sha=h)

    # Build an ordinary initial proof at the admitted immutable commit.
    from scripts.build_price_basis_metric_proof import build_price_basis_proof
    dates=days[:-2];compute='6'*40;consumer='b'*40;workflow='c'*40
    authority=B.ReadAuthority(B.sha(B.encode(selector)),selector['admission_blob_sha'])
    proof=build_price_basis_proof(repo,selector,authority,required_sessions=dates,knowledge_cutoff=now.isoformat(),compute_sha=compute,scope='prior_session_core_context')
    path='data/market-data/proofs/price-basis/initial-synthetic.json'
    with store.lock():
        admitted=store.publish(store.head(),{path:B.encode(proof)},replace=set());selector=dict(selector,read_sha=admitted)
        receipt=B.encode(dict(schema_version=1,kind='basis_consumer_receipt',compute_sha=compute,selector=selector,admission_blob_sha=selector['admission_blob_sha'],nonce='d'*64,valid_from='2026-09-09T13:00:00Z',valid_until=receipt_until))
        commitment=B.receipt_commitment(receipt);rc=store.commit(admitted,{'basis-authority/receipt.json':receipt},replace=set())
        ref=f'refs/heads/basis-read-receipts/{compute}/{commitment}';store.cas(ref,rc,'0'*40)
        git(repo,'update-ref',ref.replace('refs/heads/','refs/remotes/origin/'),rc)
        current=store.publish(store.head(),{'data/market-data/fixtures/OTHER.json':B.encode({'synthetic':True})},replace=set())
    calendar={'2026-09-09':dict(previous_trade_date='2026-09-08',previous_sessions=dates,session_type='normal',scheduled_close='16:00',source_url='https://www.nyse.com/trade/hours-calendars',known_at='2026-09-09T12:30:00Z')}
    grant=dict(schema_version=1,kind='finite_initial_price_read',repository='yuatom/stock-market-data-store',repository_id=11,owner_id=12,compute_sha=compute,consumer_sha=consumer,workflow_source_sha=workflow,workflow_id=14,selector=selector,proof_ref=dict(path=path,blob_sha=B.blob_sha(B.encode(proof))),receipt_commitment=commitment,receipt_commit=rc,receipt_blob_sha=B.blob_sha(receipt),verification_run_id=555,calendar_sha256=B.sha(B.encode(calendar)),trade_date='2026-09-09',required_last_trade_date='2026-09-08',valid_from='2026-09-09T13:00:00Z',valid_until='2026-09-09T23:00:00Z',allowed_actions=['read_price_only'],allowed_metric_scopes=['prior_session_core_context'],provider_request_limit=0,verification_limit=1,volume_policy='BLOCKED_UNRESOLVED')
    verified='2026-09-09T13:01:00Z'
    context=dict(repository=grant['repository'],repository_id=11,owner_id=12,private=True,workflow_source_sha=workflow,run_id=555,run_attempt=1,event='workflow_dispatch',actor_id=12,actor='yuatom',triggering_actor='yuatom',workflow_path=WORKFLOW,job='verify-basis-consumer')
    value=build_delivery(repo,commitment,compute,context,now=verified)
    repository=dict(id=11,private=True,full_name=grant['repository'],owner={'id':12})
    run=dict(id=555,workflow_id=14,head_sha=workflow,path=WORKFLOW,event='workflow_dispatch',run_attempt=1,status='completed',conclusion='success',head_branch='basis-consumer-delivery/'+workflow,repository={'id':11},head_repository={'id':11},actor={'id':12},triggering_actor={'id':12},run_started_at='2026-09-09T13:00:59Z')
    jobs=dict(total_count=1,jobs=[dict(id=556,run_id=555,name='verify-basis-consumer',head_sha=workflow,status='completed',conclusion='success',completed_at='2026-09-09T13:01:01Z',steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])])
    return dict(repo=repo,store=store,grant=grant,calendar=calendar,repository=repository,run=run,jobs=jobs,log=dict(job_id=556,text=encode_delivery(value)),now=now.isoformat(),current=current,proof=proof)


def module():
    assert importlib.util.find_spec('scripts.initial_basis_read') is not None,'initial read contract not implemented'
    from scripts import initial_basis_read as I
    return I


def runtime(case,**changes):
    I=module();g=case['grant'];a=I.InitialReadApproval(B.sha(B.encode(g)),g['compute_sha'],g['consumer_sha'],g['workflow_source_sha'],g['calendar_sha256'])
    args=dict(approval=a,grant_raw=B.encode(g),calendar_raw=B.encode(case['calendar']),repository=case['repository'],run=case['run'],jobs=case['jobs'],log=case['log'],current_store_sha=case['current'],consumer_sha=g['consumer_sha'],now=case['now'],calendar_by_trade_date=case['calendar']);args.update(changes)
    return I.InitialRuntimeAuthority(**args)


def binding(case):
    g=case['grant'];return dict(selector=dict(g['selector'],read_sha=case['current']),compute_sha=g['compute_sha'],receipt_commitment=g['receipt_commitment'],volume_policy='BLOCKED_UNRESOLVED',initial_read_authority=dict(kind='finite_initial_read',approval_sha256=B.sha(B.encode(g)),admitted_read_sha=g['selector']['read_sha']))


def test_initial_read_scope_exists():assert callable(module().resolve_initial_selector)


def test_approved_initial_read_lifts_after_fifteen_minutes_without_daily_state(tmp_path):
    c=fixture(tmp_path);I=module();before=git(c['repo'],'show-ref')
    selected,auth=I.resolve_initial_selector(c['repo'],binding(c),runtime(c),trade_date='2026-09-09',knowledge_cutoff=c['now'])
    view=B.read_price_view(c['repo'],selected,auth,fields=B.PRICE_FIELDS,required_sessions=c['calendar']['2026-09-09']['previous_sessions'],knowledge_cutoff=c['now'])
    assert len(view['records'])==60 and selected['read_sha']==c['current']
    assert before==git(c['repo'],'show-ref')
    assert not git(c['repo'],'ls-tree','-r','--name-only','main','--','capture-evidence/daily-basis','data/market-data/collector-state')


@pytest.mark.parametrize('attack',['date','expired','future_cutoff','approval','source','consumer','calendar','run','receipt_blob','declaration','daily_too','post_action','provider_credit','verification_count','scope','volume','public','future_verified','duplicate_log','receipt_longer','missing_tag','catalog_change'])
def test_initial_read_rejects_unapproved_scope_and_mutable_authority(tmp_path,attack):
    c=fixture(tmp_path);I=module();b=binding(c);kwargs={};day='2026-09-09';cut=c['now']
    if attack=='date':day='2026-09-10'
    if attack=='expired':kwargs['now']=c['grant']['valid_until']
    if attack=='future_cutoff':cut='2026-09-10T01:00:00Z'
    if attack=='approval':kwargs['approval']=I.InitialReadApproval('0'*64,'6'*40,'b'*40,'c'*40,c['grant']['calendar_sha256'])
    if attack=='source':c['run']['head_sha']='e'*40
    if attack=='consumer':kwargs['consumer_sha']='e'*40
    if attack=='calendar':c['calendar']['2026-09-09']['previous_trade_date']='2026-09-07'
    if attack=='run':c['grant']['verification_run_id']=556
    if attack=='receipt_blob':c['grant']['receipt_blob_sha']='e'*40
    if attack=='declaration':b['initial_read_authority']['admitted_read_sha']='e'*40
    if attack=='daily_too':b['daily_authority']={}
    if attack=='post_action':c['grant']['allowed_actions'].append('private_verify')
    if attack=='provider_credit':c['grant']['provider_request_limit']=1
    if attack=='verification_count':c['grant']['verification_limit']=2
    if attack=='scope':c['grant']['allowed_metric_scopes']=['current_target']
    if attack=='volume':c['grant']['volume_policy']='allowed'
    if attack=='public':c['repository']['private']=False
    if attack=='future_verified':c['run']['run_started_at']='2026-09-10T01:00:00Z'
    if attack=='duplicate_log':c['log']['text']+='\n'+c['log']['text']
    if attack=='receipt_longer':c['grant']['valid_until']='2026-09-10T01:00:00Z'
    if attack=='missing_tag':git(c['repo'],'update-ref','-d','refs/tags/market-data-read/'+c['current'])
    if attack=='catalog_change':
        with c['store'].lock():c['current']=c['store'].publish(c['current'],{B.catalog_path(B.PROVIDER,'SOXX'):B.encode({'changed':'synthetic'})},replace={B.catalog_path(B.PROVIDER,'SOXX')})
    if attack not in ('declaration','daily_too','approval'):b['initial_read_authority']['approval_sha256']=B.sha(B.encode(c['grant']))
    with pytest.raises(ValueError):I.resolve_initial_selector(c['repo'],b,runtime(c,**kwargs),trade_date=day,knowledge_cutoff=cut)


@pytest.mark.parametrize('path_name',['catalog','snapshot','admission','proof','envelope','terminal','raw'])
def test_initial_read_rejects_restored_protected_history(tmp_path,path_name):
    c=fixture(tmp_path);I=module();s=c['grant']['selector'];repo=c['repo'];store=c['store']
    admission=B._parse(store.read(s['read_sha'],B.admission_path(B.PROVIDER,'SOXX',s['admission_blob_sha'])))
    paths=dict(catalog=B.catalog_path(B.PROVIDER,'SOXX'),snapshot=B.snapshot_path(B.PROVIDER,'SOXX',s['basis_id']),admission=B.admission_path(B.PROVIDER,'SOXX',s['admission_blob_sha']),proof=c['grant']['proof_ref']['path'],envelope=admission['envelope_ref']['path'],terminal=admission['workflow_evidence']['terminal_ref']['path'])
    paths['raw']='capture-evidence/genesis/response.raw'
    path=paths[path_name];original=store.read(store.head(),path)
    with store.lock():
        h=store.publish(store.head(),{path:b'altered'},replace={path})
        c['current']=store.publish(h,{path:original},replace={path})
    with pytest.raises(ValueError):I.resolve_initial_selector(repo,binding(c),runtime(c),trade_date=c['grant']['trade_date'],knowledge_cutoff=c['now'])


def test_initial_read_rejects_merged_restored_side_branch(tmp_path):
    c=fixture(tmp_path);I=module();repo=c['repo'];path=B.catalog_path(B.PROVIDER,'SOXX');s=c['grant']['selector']
    git(repo,'checkout','-qb','side',s['read_sha']);original=(repo/path).read_bytes()
    (repo/path).write_bytes(b'altered');git(repo,'add','.');git(repo,'commit','-qm','side edit')
    (repo/path).write_bytes(original);git(repo,'add','.');git(repo,'commit','-qm','side restore')
    git(repo,'checkout','-q','main');git(repo,'merge','--no-ff','-s','ours','-m','merge','side')
    c['current']=git(repo,'rev-parse','HEAD');git(repo,'update-ref','refs/tags/market-data-read/'+c['current'],c['current'])
    with pytest.raises(ValueError):I.resolve_initial_selector(repo,binding(c),runtime(c),trade_date=c['grant']['trade_date'],knowledge_cutoff=c['now'])


@pytest.mark.parametrize('attack',['wrong_actor','wrong_job','wrong_workflow','wrong_ref','receipt_unrelated','proof_blob','long_window','boolean_count','unknown_field','unknown_declaration','calendar_two_days','calendar_gap','calendar_late','cutoff_before_capture','tag_annotated','target_offbranch','main_changes_mid_read'])
def test_initial_read_closed_authority_attacks(tmp_path,attack,monkeypatch):
    c=fixture(tmp_path);I=module();repo=c['repo'];g=c['grant'];day=g['trade_date'];cut=c['now'];b=binding(c)
    if attack=='wrong_actor':c['run']['actor']['id']=99
    if attack=='wrong_job':c['jobs']['jobs'][0]['head_sha']='e'*40
    if attack=='wrong_workflow':c['run']['path']='.github/workflows/other.yml'
    if attack=='wrong_ref':git(repo,'update-ref',f"refs/heads/basis-read-receipts/{g['compute_sha']}/{g['receipt_commitment']}",c['current'])
    if attack=='receipt_unrelated':
        tree=git(repo,'rev-parse',g['receipt_commit']+'^{tree}');new=git(repo,'commit-tree',tree,'-m','root clone')
        g['receipt_commit']=new
    if attack=='proof_blob':g['proof_ref']['blob_sha']='e'*40
    if attack=='long_window':g['valid_from']='2026-09-08T00:00:00Z'
    if attack=='boolean_count':g['verification_limit']=True
    if attack=='unknown_field':g['renew']=True
    if attack=='unknown_declaration':b['initial_read_authority']['verified']=True
    if attack=='calendar_two_days':c['calendar']['2026-09-10']=copy.deepcopy(c['calendar'][day]);g['calendar_sha256']=B.sha(B.encode(c['calendar']))
    if attack=='calendar_gap':c['calendar'][day]['previous_sessions'].pop(4);g['calendar_sha256']=B.sha(B.encode(c['calendar']))
    if attack=='calendar_late':c['calendar'][day]['known_at']='2026-09-09T22:00:00Z';g['calendar_sha256']=B.sha(B.encode(c['calendar']))
    if attack=='cutoff_before_capture':cut='2026-09-09T11:00:00Z'
    if attack=='tag_annotated':
        git(repo,'update-ref','-d','refs/tags/market-data-read/'+c['current']);git(repo,'tag','-a','market-data-read/'+c['current'],c['current'],'-m','annotated')
    if attack=='target_offbranch':
        with c['store'].lock():off=c['store'].commit(g['selector']['read_sha'],{'off.json':b'{}'},replace=set())
        git(repo,'update-ref','refs/tags/market-data-read/'+off,off);b['selector']['read_sha']=off
    if attack=='main_changes_mid_read':
        old=I._ref;calls=[]
        def changed(git_store,name):
            if name=='refs/heads/main':
                calls.append(name)
                if len(calls)>1:return 'e'*40
            return old(git_store,name)
        monkeypatch.setattr(I,'_ref',changed)
    if attack!='unknown_declaration':b['initial_read_authority']['approval_sha256']=B.sha(B.encode(g))
    with pytest.raises(ValueError):I.resolve_initial_selector(repo,b,runtime(c),trade_date=day,knowledge_cutoff=cut)



def test_initial_read_cannot_roll_into_next_et_day_even_with_short_unexpired_grant(tmp_path):
    c=fixture(tmp_path,receipt_until='2026-09-10T12:00:00Z');I=module();g=c['grant']
    g['valid_until']='2026-09-10T12:00:00Z';c['now']='2026-09-10T05:00:00Z'
    with pytest.raises(ValueError):I.resolve_initial_selector(c['repo'],binding(c),runtime(c),trade_date=g['trade_date'],knowledge_cutoff=c['now'])


def test_initial_read_expiry_must_end_by_next_et_midnight_even_before_midnight(tmp_path):
    c=fixture(tmp_path,receipt_until='2026-09-10T12:00:00Z');I=module()
    c['grant']['valid_until']='2026-09-10T12:00:00Z'
    with pytest.raises(ValueError):I.resolve_initial_selector(c['repo'],binding(c),runtime(c),trade_date=c['grant']['trade_date'],knowledge_cutoff=c['now'])
