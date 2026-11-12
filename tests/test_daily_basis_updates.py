"""All market/provider/platform values in this module are fabricated fixtures."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import pytest
from scripts import provider_basis_snapshot as B
from scripts.raw_response_evidence import build_envelope


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.invalid',*args]).decode().strip()


def rows():
    return [dict(datetime=d,open=p,high=p,low=p,close=p,volume=v) for d,p,v in
        [('2026-11-03','10.000001','300'),('2026-11-04','11','303'),
         ('2026-11-05','12.00','120'),('2026-11-06','13','130'),
         ('2026-11-09','14.0000001','140'),('2026-11-10','15','150')]]


def request(end):
    return dict(provider='twelve_data',symbol='SOXX',interval='1day',exchange='NASDAQ',mic_code='XNMS',currency='USD',type='ETF',adjust='splits',dp=-1,start_date='2026-11-03',end_date=end,outputsize=5000,order='asc')


def response(req,values):
    return B.encode(dict(status='ok',meta={k:req[k] for k in ('symbol','interval','currency','exchange','mic_code','type')}|{'exchange_timezone':'America/New_York'},values=values))


def fixture(tmp_path):
    repo=tmp_path/'store';repo.mkdir();git(repo,'init','-qb','main');git(repo,'remote','add','origin','https://github.com/yuatom/stock-market-data-store')
    req=request('2026-11-06');e=build_envelope(response(req,rows()[:4]),req,'2026-11-07T12:00:00Z')
    baseline=B.encode([dict(trade_date=d,open=p,high=p,low=p,close=p,volume=100) for d,p in [('2026-11-03',30),('2026-11-04',33)]])
    split=dict(kind='forward_split',numerator=3,denominator=1,effective_date='2026-11-05',source_url='https://example.invalid/synthetic-split')
    snapshot=B.prepare_snapshot(e,B.sha(e),baseline,split);bid=snapshot['basis_id']
    terminal=B.encode(dict(status='CAPTURE_RETAINED_UNQUALIFIED',request_id='100-1',compute_sha='a'*40,envelope_sha256=B.sha(e),raw_response_sha256=snapshot['raw_response_sha256']))
    admission=dict(schema_version=1,kind='provider_snapshot_price_admission',status='ADMITTED_PRICE_ONLY',basis_id=bid,allowed_fields=list(B.PRICE_FIELDS),volume_policy='BLOCKED_UNRESOLVED',admitted_at='2026-11-07T12:00:01Z',proof_model='reviewed_workflow_receipt_not_provider_signature',envelope_ref={'path':'capture-evidence/genesis/envelope.json','blob_sha':B.blob_sha(e)},workflow_evidence={'compute_sha':'a'*40,'request_id':'100-1','terminal_ref':{'path':'capture-evidence/genesis/terminal.json','blob_sha':B.blob_sha(terminal)}},unresolved_volume_dates=snapshot['unresolved_volume_dates'])
    ar=B.encode(admission);sr=B.encode(snapshot)
    catalog=dict(schema_version=1,provider=B.PROVIDER,symbol='SOXX',basis_id=bid,snapshot_blob_sha=B.blob_sha(sr),admission_blob_sha=B.blob_sha(ar))
    paths={B.catalog_path(B.PROVIDER,'SOXX'):B.encode(catalog),B.snapshot_path(B.PROVIDER,'SOXX',bid):sr,B.admission_path(B.PROVIDER,'SOXX',B.blob_sha(ar)):ar,'capture-evidence/genesis/envelope.json':e,'capture-evidence/genesis/terminal.json':terminal,'capture-evidence/genesis/baseline.json':baseline}
    paths.update({f'data/market-data/series/daily/{B.PROVIDER}/SOXX/{name}':B.encode({'legacy':'unchanged'}) for name in ['_index.json','000000.json','000128.json','000256.json']})
    paths['unrelated.json']=B.encode({'eleven_symbols':65})
    for p,raw in paths.items():
        f=repo/p;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(raw)
    git(repo,'add','.');git(repo,'commit','-qm','synthetic genesis');head=git(repo,'rev-parse','HEAD')
    calendar=dict(schema_version=1,kind='reviewed_finite_daily_calendar',source_url='https://www.nyse.com/trade/hours-calendars',known_at='2026-11-08T12:00:00Z',sessions=[{'trade_date':r['datetime'],'scheduled_close':'16:00'} for r in rows()])
    slots=[dict(slot_id='nov10',execution_day='2026-11-10',cutoff='2026-11-09',valid_from='2026-11-10T12:00:00Z',valid_until='2026-11-10T20:00:00Z',cleanup_until='2026-11-10T20:30:00Z',receipt_until='2026-11-11T00:00:00Z'),dict(slot_id='nov11',execution_day='2026-11-11',cutoff='2026-11-10',valid_from='2026-11-11T12:00:00Z',valid_until='2026-11-11T20:00:00Z',cleanup_until='2026-11-11T20:30:00Z',receipt_until='2026-11-12T00:00:00Z')]
    grant=dict(schema_version=1,kind='finite_daily_basis_grant',repository='yuatom/stock-market-data-store',repository_id=11,owner_id=12,compute_sha='a'*40,consumer_sha='b'*40,workflow_source_sha='c'*40,workflow_id=14,calendar_sha256=B.sha(B.encode(calendar)),initial_selector=catalog|{'read_sha':head},baseline_ref=dict(commit=head,path='capture-evidence/genesis/baseline.json',blob_sha=B.blob_sha(baseline),sha256=B.sha(baseline)),request_template=req,slots=slots,request_limit=2,credit_limit=2,verification_limit=2,hard_credits_per_day=200,hard_credits_per_minute=8,maturity_next_day_et='07:00',private_destination='capture-evidence/daily-basis',actions_free_only=True,allowed_actions=['capture','private_verify','immutable_catalog_update'],volume_policy='BLOCKED_UNRESOLVED')
    return repo,grant,calendar,paths


def setup(tmp_path):
    from scripts import daily_basis_updates as D
    repo,g,c,paths=fixture(tmp_path)
    auth=D.GrantAuthority(B.sha(B.encode(g)),'a'*40,'b'*40,'c'*40,B.sha(B.encode(c)))
    store=D.LocalGitStore(repo)
    current=[datetime(2026,11,10,12,0,5,tzinfo=timezone.utc)]
    updater=D.DailyUpdater(store,B.encode(g),B.encode(c),auth,clock=lambda:current[0])
    return D,repo,g,c,paths,store,current,updater


def capture(values=None,fail=False,calls=None):
    def run(*,request,before_request,retain_raw,retain_envelope,clock):
        before_request()
        if calls is not None:calls.append(request)
        if fail:raise TimeoutError('uncertain provider result')
        raw=response(request,values if values is not None else [r for r in rows() if r['datetime']<=request['end_date']])
        retain_raw(raw)
        envelope=build_envelope(raw,request,clock().isoformat())
        retain_envelope(envelope)
        return dict(status='CAPTURE_RETAINED_UNQUALIFIED',envelope_sha256=B.sha(envelope),raw_response_sha256=B.sha(raw))
    return run


def test_daily_module_exists_before_implementation():
    from scripts import daily_basis_updates
    assert callable(daily_basis_updates.DailyUpdater)


def test_two_daily_candidates_preserve_every_old_byte_and_existing_reader(tmp_path):
    D,repo,g,c,paths,store,now,u=setup(tmp_path)
    with store.lock():
        first=u.run_slot('nov10','101-1',capture=capture())
        assert store.head()!=first.selector['read_sha']
        old=store.read(store.head(),B.catalog_path(B.PROVIDER,'SOXX'))
        assert old==paths[B.catalog_path(B.PROVIDER,'SOXX')]
        proof=synthetic_delivery(D,u,first,now[0])
        u.promote(first,proof)
        v=u.read_current(proof,fields=('close',))
        assert len(v['records'])==5 and v['records'][-1]['close']=='14.0000001'
    now[0]=datetime(2026,11,11,12,0,5,tzinfo=timezone.utc)
    with store.lock():
        second=u.run_slot('nov11','102-1',capture=capture())
        assert second.selector['basis_id']!=first.selector['basis_id']
        proof=synthetic_delivery(D,u,second,now[0]);u.promote(second,proof)
        assert len(u.read_current(proof,fields=('open','close'))['records'])==6
        for path,raw in paths.items():
            if path!=B.catalog_path(B.PROVIDER,'SOXX'):assert store.read(store.head(),path)==raw
        assert store.read(store.head(),B.snapshot_path(B.PROVIDER,'SOXX',first.selector['basis_id']))==store.read(first.selector['read_sha'],B.snapshot_path(B.PROVIDER,'SOXX',first.selector['basis_id']))
        with pytest.raises(D.DailyBasisError):u.run_slot('nov11','103-1',capture=capture())


def synthetic_delivery(D,u,candidate,now,mutate=None):
    """Fabricated authenticated-provider responses for local validator testing only."""
    u.reserve_verification(candidate)
    data=u.delivery_expectation(candidate,run_id=555)
    from scripts.emit_basis_consumer_delivery import encode_delivery
    context=dict(repository='yuatom/stock-market-data-store',repository_id=11,owner_id=12,private=True,workflow_source_sha='c'*40,run_id=555,run_attempt=1,event='workflow_dispatch',actor_id=12,actor='yuatom',triggering_actor='yuatom',workflow_path='.github/workflows/basis-consumer-delivery.yml',job='verify-basis-consumer')
    value=dict(schema_version=1,kind='private_basis_consumer_delivery',compute_sha='a'*40,receipt_commitment=candidate.receipt_commitment,context=context,receipt_ref=candidate.receipt_ref,receipt_commit=candidate.receipt_commit,receipt_blob_sha=candidate.receipt_blob_sha,selector=candidate.selector,verified_at=now.isoformat(),valid_until=candidate.receipt_until)
    repository=dict(id=11,private=True,full_name='yuatom/stock-market-data-store',owner={'id':12})
    run=dict(id=555,workflow_id=14,head_sha='c'*40,path=context['workflow_path'],event='workflow_dispatch',run_attempt=1,status='completed',conclusion='success',head_branch='basis-consumer-delivery/'+'c'*40,repository={'id':11},head_repository={'id':11},actor={'id':12},triggering_actor={'id':12},run_started_at=now.isoformat())
    jobs=dict(total_count=1,jobs=[dict(id=556,run_id=555,name='verify-basis-consumer',head_sha='c'*40,status='completed',conclusion='success',completed_at=now.isoformat(),steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])])
    log=dict(job_id=556,text=encode_delivery(value))
    if mutate:mutate(repository,run,jobs,log)
    return D.verify_daily_delivery(data,repository,run,jobs,log,now=now.isoformat())


@pytest.mark.parametrize('attack',['price','volume','missing','extra','identity','precision','reorder','maturity','window','source','grant_hash','calendar_hash','calendar_known_later','calendar_missing','calendar_extra','slot_skipped','quota','daily_budget','minute_budget','unlocked'])
def test_rejects_bad_input_without_catalog_change(tmp_path,attack):
    D,repo,g,c,paths,store,now,u=setup(tmp_path);calls=[];values=rows()[:5];slot='nov10';transport=capture(values,calls=calls)
    if attack=='price':values[0]['close']='10.000002';values[0]['high']='10.000002'
    if attack=='volume':values[0]['volume']='301'
    if attack=='missing':values.pop(2)
    if attack=='extra':values.insert(3,dict(values[2],datetime='2026-11-07'))
    if attack=='reorder':values.reverse()
    if attack=='maturity':now[0]=datetime(2026,11,10,11,59,0,tzinfo=timezone.utc)
    if attack=='window':now[0]=datetime(2026,11,10,21,0,0,tzinfo=timezone.utc)
    if attack=='identity':g['request_template']['mic_code']='XNAS'
    if attack=='precision':g['request_template']['dp']=5
    if attack=='source':g['consumer_sha']='d'*40
    if attack=='quota':g['request_limit']=1
    if attack=='calendar_known_later':c['known_at']='2026-11-11T00:00:00Z'
    if attack=='calendar_missing':c['sessions'].pop(2)
    if attack=='calendar_extra':c['sessions'].insert(3,dict(trade_date='2026-11-07',scheduled_close='16:00'))
    if attack=='slot_skipped':slot='nov11';now[0]=datetime(2026,11,11,12,0,5,tzinfo=timezone.utc)
    auth=D.GrantAuthority('0'*64 if attack=='grant_hash' else B.sha(B.encode(g)),'a'*40,'b'*40,'c'*40,'0'*64 if attack=='calendar_hash' else B.sha(B.encode(c)))
    g['calendar_sha256']=B.sha(B.encode(c))
    if attack not in ('grant_hash',):auth=D.GrantAuthority(B.sha(B.encode(g)),auth.compute_sha,auth.consumer_sha,auth.workflow_source_sha,auth.calendar_sha256)
    with pytest.raises(D.DailyBasisError):
        u=D.DailyUpdater(store,B.encode(g),B.encode(c),auth,clock=lambda:now[0])
        if attack=='unlocked':u.run_slot(slot,'101-1',capture=transport)
        else:
            with store.lock():
                if attack in ('daily_budget','minute_budget'):
                    path='data/market-data/collector-state/2026-11/2026-11-10-twelve-data-budget.json'
                    raw=B.encode(dict(schema_version=1,trade_date='2026-11-10',credits=200 if attack=='daily_budget' else 8,minutes={'2026-11-10T12:00Z':8 if attack=='minute_budget' else 1}))
                    store.publish(store.head(),{path:raw},replace=set())
                u.run_slot(slot,'101-1',capture=transport)
    assert store.read(store.head(),B.catalog_path(B.PROVIDER,'SOXX'))==paths[B.catalog_path(B.PROVIDER,'SOXX')]
    if attack not in ('price','volume','missing','extra','reorder','calendar_missing','calendar_extra'):assert calls==[]


def test_uncertain_request_consumes_one_slot_and_one_debit_without_retry(tmp_path):
    D,repo,g,c,paths,store,now,u=setup(tmp_path);calls=[]
    with store.lock():
        with pytest.raises(D.DailyBasisError):u.run_slot('nov10','101-1',capture=capture(fail=True,calls=calls))
        with pytest.raises(D.DailyBasisError):u.run_slot('nov10','102-1',capture=capture(calls=calls))
    assert len(calls)==1
    b=B._parse(store.read(store.head(),'data/market-data/collector-state/2026-11/2026-11-10-twelve-data-budget.json'))
    assert b['credits']==1


@pytest.mark.parametrize('attack',['public','source','actor','attempt','expired','duplicate','no_verified_delivery','moved_head','volume_read'])
def test_promotion_and_current_reader_fail_closed(tmp_path,attack):
    D,repo,g,c,paths,store,now,u=setup(tmp_path)
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture())
        def mutate(r,run,jobs,log):
            if attack=='public':r['private']=False
            if attack=='source':run['head_sha']='0'*40
            if attack=='actor':run['actor']['id']=99
            if attack=='attempt':run['run_attempt']=2
            if attack=='duplicate':log['text']+='\n'+log['text']
        if attack in ('public','source','actor','attempt','duplicate'):
            with pytest.raises(D.DailyBasisError):synthetic_delivery(D,u,candidate,now[0],mutate)
        else:
            delivery=synthetic_delivery(D,u,candidate,now[0])
            if attack=='expired':now[0]=datetime(2026,11,11,0,0,0,tzinfo=timezone.utc)
            if attack=='moved_head':store.publish(store.head(),{'unrelated-new.json':b'{}\n'},replace=set())
            with pytest.raises(D.DailyBasisError):
                if attack=='volume_read':u.promote(candidate,delivery);u.read_current(delivery,fields=('volume',))
                else:u.promote(candidate,None if attack=='no_verified_delivery' else delivery)


def test_one_daily_attestation_remains_valid_only_within_approved_receipt_window(tmp_path):
    D,repo,g,c,paths,store,now,u=setup(tmp_path);calls=[]
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture(calls=calls))
        first=synthetic_delivery(D,u,candidate,now[0]);u.promote(candidate,first)
        now[0]=datetime(2026,11,10,17,0,5,tzinfo=timezone.utc)
        assert len(u.read_current(first,fields=('close',))['records'])==5
        with pytest.raises(D.DailyBasisError):u.reserve_verification(candidate)
        now[0]=datetime(2026,11,11,0,0,0,tzinfo=timezone.utc)
        with pytest.raises(D.DailyBasisError):u.read_current(first,fields=('close',))
    assert len(calls)==1


def test_independent_promotion_rejects_altered_candidate_closure(tmp_path):
    D,repo,g,c,paths,store,now,u=setup(tmp_path)
    from dataclasses import replace
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture())
        # An arbitrary ref is not admission: a candidate must have EXACT closure.
        rogue=store.commit(candidate.expected_head,{'unauthorized.json':B.encode({'bad':True})},replace=set())
        forged=replace(candidate,selector=dict(candidate.selector,read_sha=rogue))
        with pytest.raises(D.DailyBasisError):u.validate_candidate(forged)


@pytest.mark.parametrize('change',['expired_window','moved_main'])
def test_started_response_is_retained_unqualified_after_promotion_gate_closes(tmp_path,change):
    D,repo,g,c,paths,store,now,u=setup(tmp_path);calls=[]
    now[0]=datetime(2026,11,10,19,59,5,tzinfo=timezone.utc)
    def delayed(**kw):
        original=kw['before_request']
        def before():
            original()
            if change=='expired_window':now[0]=datetime(2026,11,10,20,0,5,tzinfo=timezone.utc)
            else:store.publish(store.head(),{'unrelated-new.json':B.encode({'preserve':True})},replace=set())
        kw['before_request']=before
        return capture(calls=calls)(**kw)
    with store.lock():
        with pytest.raises(D.DailyBasisError):u.run_slot('nov10','101-1',capture=delayed)
        ref=u._attempt_ref(u._slot('nov10'));evidence=store.ref(ref)
        assert evidence is not None
        prefix=u._prefix(u._slot('nov10'))
        assert B._parse(store.read(evidence,prefix+'/terminal.json'))['market_qualified'] is False
        assert B._parse(store.read(evidence,prefix+'/envelope.json'))['claims']['production_activation'] is False
        assert store.read(evidence,prefix+'/response.raw')
        assert store.read(store.head(),B.catalog_path(B.PROVIDER,'SOXX'))==paths[B.catalog_path(B.PROVIDER,'SOXX')]
    assert len(calls)==1


def test_daily_current_view_lifts_only_unchanged_basis_to_latest_store_sha(tmp_path):
    D,repo,g,c,paths,store,now,u=setup(tmp_path)
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture());delivery=synthetic_delivery(D,u,candidate,now[0]);u.promote(candidate,delivery)
        lifted=store.publish(store.head(),{'intraday/other.json':B.encode({'new':True})},replace=set())
        view=u.read_current(delivery,fields=('close',))
        assert view['read_sha']==lifted and view['admitted_candidate_sha']==candidate.selector['read_sha']
        assert view['basis_id']==candidate.selector['basis_id']
        receipt=B._parse(store.read(candidate.receipt_commit,'basis-authority/receipt.json'))
        assert receipt['selector']['read_sha']==candidate.selector['read_sha']


def test_lift_rejects_history_edit_even_if_current_blob_is_restored(tmp_path):
    D,repo,g,c,paths,store,now,u=setup(tmp_path)
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture());delivery=synthetic_delivery(D,u,candidate,now[0]);u.promote(candidate,delivery)
        path=B.snapshot_path(B.PROVIDER,'SOXX',candidate.selector['basis_id']);raw=store.read(store.head(),path)
        store.publish(store.head(),{path:B.encode({'tamper':True})},replace={path})
        store.publish(store.head(),{path:raw},replace={path})
        with pytest.raises(D.DailyBasisError):u.read_current(delivery,fields=('close',))


def test_current_read_requires_normal_immutable_target_retention_ref(tmp_path):
    D,repo,g,c,paths,store,now,u=setup(tmp_path)
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture());delivery=synthetic_delivery(D,u,candidate,now[0]);u.promote(candidate,delivery)
        tag='refs/tags/market-data-read/'+store.head()
        assert store.ref(tag)==store.head()
        git(repo,'update-ref','-d',tag)
        with pytest.raises(D.DailyBasisError):u.read_current(delivery,fields=('close',))


def test_verification_intent_must_be_exact_child_of_candidate(tmp_path):
    D,repo,g,c,paths,store,now,u=setup(tmp_path)
    with store.lock():
        candidate=u.run_slot('nov10','101-1',capture=capture());ref=u.reserve_verification(candidate)
        reservation=store.ref(ref);tree=git(repo,'rev-parse',reservation+'^{tree}')
        root=git(repo,'commit-tree',tree,'-m','forged unrelated root')
        git(repo,'update-ref',ref,root,reservation)
        with pytest.raises(D.DailyBasisError):u.delivery_expectation(candidate,run_id=555)
