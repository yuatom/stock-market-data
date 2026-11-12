"""Synthetic local Git authority only; no live receipt, workflow, or provider."""
import copy
import hashlib
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from scripts import private_authority_input as selection
from scripts import private_authority_receipt as receipt
from scripts import private_authority_preflight as preflight
from scripts import private_authority_promotion as promotion
from scripts import run_private_authority_route as route
from scripts import twelve_symbol_append as twelve
from tests.test_private_authority_input import COMPUTE,encode,git,add_commit,install_descriptor
from tests.test_private_authority_promotion import exercise
from tests.test_private_authority_route import setup

ROUTE='twelve_symbol_conditional_append'
WORKFLOW='Twelve Symbol Append'

def make_twelve(tmp_path,mutate_contract=None,mutate_approval=None,contract_raw_transform=None):
    repo=tmp_path/'store';repo.mkdir()
    git(repo,'init','-q','-b','main')
    git(repo,'config','user.name','Synthetic')
    git(repo,'config','user.email','synthetic@example.invalid')
    files={'synthetic.txt':b'NOT MARKET DATA\n'};targets=[]
    for symbol in twelve.SYMBOLS:
        pins={}
        for name in ('000000.json','_index.json'):
            raw=b'{}\n';path='data/market-data/series/daily/twelve_data_basic/'+symbol+'/'+name
            files[path]=raw;pins[name]=hashlib.sha1(b'blob 3\0'+raw).hexdigest()
        targets.append(dict(symbol=symbol,old_count=2,first_date='2024-01-02',
            last_date='2024-01-03',addition_dates=['2024-01-04'],files=pins))
    baseline=add_commit(repo,files)
    contract=dict(schema_version=1,kind='twelve_symbol_append_contract',baseline_store_sha=baseline,
        historical_cutoff='2024-01-04',targets=targets,request_limit=12,credit_limit=12,per_symbol_request_limit=1)
    if mutate_contract:mutate_contract(contract)
    raw=encode(contract)
    if contract_raw_transform:raw=contract_raw_transform(raw)
    digest=hashlib.sha256(raw).hexdigest()
    authority_path='capture-contracts/'+digest+'.json'
    head=add_commit(repo,{authority_path:raw})
    approval=dict(schema_version=1,mode=ROUTE,compute_sha=COMPUTE,execution_day='2026-11-11',
        valid_from='2026-11-11T12:00:00+00:00',valid_until='2026-11-11T23:00:00+00:00',
        evidence_repository=selection.STORE_REPOSITORY,evidence_prefix='capture-evidence',
        request_limit=12,credit_limit=12,expected_store_sha=head,private_contract_sha256=digest,
        registry_sha256='b'*64,historical_cutoff='2024-01-04',basis_policy='full_ohlc_overlap_preserve_volume_v1')
    if mutate_approval:mutate_approval(approval)
    approval_raw=encode(approval);approval_digest=hashlib.sha256(approval_raw).hexdigest()
    descriptor=dict(schema_version=1,kind='capture_authority_selection',route=ROUTE,
        compute_sha=COMPUTE,authority_store_sha=head,approval_sha256=approval_digest,approval=approval)
    f=dict(repo=repo,route=ROUTE,head=head,baseline=baseline,authority_path=authority_path,
        ref='refs/heads/capture-authority/'+ROUTE+'/'+COMPUTE,descriptor=descriptor,
        approval_raw=approval_raw,contract=contract)
    install_descriptor(f)
    return f

def select(f,**changes):
    args=dict(store=f['repo'],route=ROUTE,compute_sha=COMPUTE,
        expected_store_head=f['head'],descriptor_commit=f['commit'])
    args.update(changes)
    return selection.load_authority(**args)

def test_twelve_is_explicit_in_all_closed_route_owners():
    expected={'soxx_capture_only','eleven_symbol_conditional_append',ROUTE}
    assert selection.ROUTES==receipt.ROUTES==expected
    assert preflight.WORKFLOWS[ROUTE]=='twelve-symbol-append.yml'

def test_twelve_receipt_roundtrip_still_binds_route_and_compute():
    value=dict(schema_version=1,kind='capture_authority_receipt',route=ROUTE,
        compute_sha=COMPUTE,authority_store_sha='a'*40,descriptor_commit='b'*40,nonce='d'*64)
    raw=receipt.canonical_receipt(value)
    digest=receipt.receipt_commitment(raw)
    assert receipt.verify_receipt(raw,digest,ROUTE,COMPUTE).route==ROUTE
    for wrong_route,wrong_sha in [('eleven_symbol_conditional_append',COMPUTE),(ROUTE,'f'*40)]:
        with pytest.raises(receipt.PrivateReceiptError):
            receipt.verify_receipt(raw,digest,wrong_route,wrong_sha)

def test_twelve_local_selection_is_read_only_not_authorization(tmp_path):
    f=make_twelve(tmp_path);before=git(f['repo'],'rev-parse','HEAD')
    result=select(f)
    assert result.approval_raw==f['approval_raw']
    assert result.authorization_authenticated is False and result.production_activation is False
    assert git(f['repo'],'rev-parse','HEAD')==before and git(f['repo'],'status','--porcelain')==''

@pytest.mark.parametrize('key,value',[('request_limit',11),('credit_limit',13),('request_limit',True),
    ('mode','eleven_symbol_conditional_append'),('compute_sha','f'*40),('registry_sha256','bad')])
def test_twelve_approval_limits_and_source_reject(tmp_path,key,value):
    f=make_twelve(tmp_path,mutate_approval=lambda x:x.update({key:value}))
    with pytest.raises(selection.PrivateAuthorityError):select(f)

@pytest.mark.parametrize('mutation',['kind','limit','per_symbol','duplicate','missing','order','cutoff','blob','extra'])
def test_twelve_contract_is_exact_not_old_eleven(tmp_path,mutation):
    def change(c):
        if mutation=='kind':c['kind']='eleven_symbol_append_contract'
        elif mutation=='limit':c['credit_limit']=11
        elif mutation=='per_symbol':c['per_symbol_request_limit']=2
        elif mutation=='duplicate':c['targets'][1]=copy.deepcopy(c['targets'][0])
        elif mutation=='missing':c['targets'].pop()
        elif mutation=='order':c['targets'].reverse()
        elif mutation=='cutoff':c['historical_cutoff']='2024-01-05'
        elif mutation=='blob':c['targets'][0]['files']['000000.json']='0'*40
        else:c['unexpected']=True
    f=make_twelve(tmp_path,mutate_contract=change)
    with pytest.raises(selection.PrivateAuthorityError):select(f)

@pytest.mark.parametrize('field',['compute_sha','expected_store_head','descriptor_commit'])
def test_twelve_selection_pins_cannot_be_retargeted(tmp_path,field):
    f=make_twelve(tmp_path)
    with pytest.raises(selection.PrivateAuthorityError):select(f,**{field:'f'*40})

@pytest.mark.parametrize('name,module',[('soxx_capture_only','run_soxx_capture'),
    ('eleven_symbol_conditional_append','run_eleven_symbol_append'),(ROUTE,'run_twelve_symbol_append')])
def test_coordinator_explicit_dispatch(name,module,monkeypatch):
    seen=[];marker=object()
    monkeypatch.setattr(route.importlib,'import_module',lambda path:(seen.append(path),marker)[1])
    assert route._coordinator(name) is marker and seen==[module]

@pytest.mark.parametrize('unknown',['unknown','',None,[]])
def test_coordinator_never_falls_back_to_eleven(unknown,monkeypatch):
    import_call=Mock()
    monkeypatch.setattr(route.importlib,'import_module',import_call)
    with pytest.raises(route.PrivateRouteError):route._coordinator(unknown)
    import_call.assert_not_called()

def env_for(sha):
    return dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REF='refs/heads/main',
        GITHUB_SHA=sha,GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='123',GITHUB_REPOSITORY='yuatom/stock-market-data',
        GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_WORKFLOW=WORKFLOW,
        GITHUB_WORKFLOW_REF='yuatom/stock-market-data/.github/workflows/twelve-symbol-append.yml@refs/heads/main')

def test_twelve_preflight_identity_is_bound_to_exact_manual_workflow():
    env=env_for(COMPUTE)
    preflight._validate_identity(ROUTE,COMPUTE,env)
    for key,value in [('GITHUB_RUN_ATTEMPT','2'),('GITHUB_SHA','f'*40),
        ('GITHUB_WORKFLOW_REF','yuatom/stock-market-data/.github/workflows/eleven-symbol-append.yml@refs/heads/main')]:
        with pytest.raises(ValueError):preflight._validate_identity(ROUTE,COMPUTE,dict(env,**{key:value}))

@pytest.mark.parametrize('bad_name',[False,True])
def test_twelve_route_checks_exact_name_before_private_access(setup,monkeypatch,bad_name):
    m,root,args,prepare,invoke,_=setup
    args['route']=ROUTE
    for k,v in env_for(args['compute_sha']).items():monkeypatch.setenv(k,v)
    if bad_name:monkeypatch.setenv('GITHUB_WORKFLOW','Eleven Symbol Append')
    if bad_name:
        with pytest.raises(m.PrivateRouteError):m.run_route(**args)
        prepare.assert_not_called();invoke.assert_not_called()
    else:
        assert m.run_route(**args)=='PRIVATE_ROUTE_COMPLETE'
        assert prepare.call_count==invoke.call_count==1
        assert invoke.call_args.kwargs['route']==ROUTE

def test_twelve_route_source_drift_stops_before_private_access(setup,monkeypatch):
    m,root,args,prepare,invoke,_=setup;args['route']=ROUTE
    for k,v in env_for(args['compute_sha']).items():monkeypatch.setenv(k,v)
    monkeypatch.setattr(m,'_remote_main',lambda root:'f'*40)
    with pytest.raises(m.PrivateRouteError):m.run_route(**args)
    prepare.assert_not_called();invoke.assert_not_called()

@pytest.fixture
def promoted_fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(promotion,'_utc_now',lambda:datetime(2026,11,11,13,tzinfo=timezone.utc))
    f=make_twelve(tmp_path);remote=tmp_path/'remote.git'
    git(tmp_path,'init','--bare','-q',str(remote))
    git(f['repo'],'push','-q',str(remote),f['baseline']+':refs/heads/main',f['ref']+':'+f['ref'])
    return f,remote

def test_twelve_exact_local_promotion_is_one_cas_not_execution(promoted_fixture,tmp_path):
    f,remote=promoted_fixture
    result,seen,pushes=exercise(f,remote,tmp_path/'checkout')
    assert result.approval_raw==f['approval_raw'] and result.production_activation is False
    assert len(pushes)==1 and '--force-with-lease=refs/heads/main:'+f['baseline'] in pushes[0]
    assert git(remote,'rev-parse','main')==f['head']

@pytest.mark.parametrize('fault',['push_rejected','push_unknown','readback_unknown','readback_ref_changed','race_at_push'])
def test_twelve_uncertain_promotion_never_retries(promoted_fixture,tmp_path,fault):
    f,remote=promoted_fixture
    with pytest.raises(promotion.PrivatePromotionError):
        exercise(f,remote,tmp_path/'checkout',fault=fault)
    assert len(f['observed_pushes'])==1
    if fault in ('push_unknown','readback_unknown','readback_ref_changed'):
        assert git(remote,'rev-parse','main')==f['head']

@pytest.mark.parametrize('unknown',['unknown',None])
def test_common_approval_rejects_unknown_without_outer_gate(unknown):
    from scripts.run_twelve_symbol_append import example_request
    value=example_request(compute=COMPUTE,registry_sha='b'*64,day='2026-11-11')
    value.update(mode=unknown,request_limit=11,credit_limit=11)
    with pytest.raises(ValueError):selection._approval_common(value,unknown,COMPUTE)

@pytest.mark.parametrize('name,limit',[(ROUTE,11),('eleven_symbol_conditional_append',12)])
def test_common_approval_never_mixes_eleven_and_twelve_limits(name,limit):
    from scripts.run_twelve_symbol_append import example_request
    value=example_request(compute=COMPUTE,registry_sha='b'*64,day='2026-11-11')
    value.update(mode=name,request_limit=limit,credit_limit=limit)
    with pytest.raises(ValueError):selection._approval_common(value,name,COMPUTE)

def test_twelve_private_contract_must_be_canonical_even_with_exact_digest(tmp_path):
    import json
    f=make_twelve(tmp_path,contract_raw_transform=lambda raw:(json.dumps(json.loads(raw),indent=2)+'\n').encode())
    with pytest.raises(selection.PrivateAuthorityError):select(f)
