"""Deployment-boundary tests with no real provider or remote endpoints."""
import importlib.util
import json
from datetime import datetime,timezone
import pytest
from scripts import eleven_symbol_append as core

def runner():
    assert importlib.util.find_spec('scripts.run_twelve_symbol_append') is not None, 'twelve runner missing'
    from scripts import run_twelve_symbol_append
    return run_twelve_symbol_append

def test_exact_twelve_approval_rejects_eleven_and_limit_drift():
    r=runner();now=datetime(2026,11,11,13,tzinfo=timezone.utc)
    value=r.example_request(compute='c'*40,registry_sha='d'*64,day='2026-11-11')
    assert value['mode']=='twelve_symbol_conditional_append'
    assert value['request_limit']==value['credit_limit']==12
    assert r.validate_approval(core.encode(value),core.sha(core.encode(value)),now)==value
    for key,val in [('mode','eleven_symbol_conditional_append'),('request_limit',11),('credit_limit',13)]:
        changed=dict(value,**{key:val});raw=core.encode(changed)
        with pytest.raises(r.OfficialStop):r.validate_approval(raw,core.sha(raw),now)

def test_missing_gld_identity_stops_before_any_io(tmp_path,monkeypatch):
    r=runner();now=datetime(2026,11,11,13,tzinfo=timezone.utc);events=[]
    registry=json.loads(r.REGISTRY.read_bytes())
    gld=next(p for p in registry['profiles'] if p['symbol']=='GLD')
    gld.update(capture_identity_verified=False,exchange=None,profile_mic_code=None,url=None)
    path=tmp_path/'missing-gld-registry.json';path.write_bytes(core.encode(registry))
    monkeypatch.setattr(r,'REGISTRY',path)
    value=r.example_request(compute='c'*40,registry_sha=core.sha(r.REGISTRY.read_bytes()),day='2026-11-11')
    def forbidden(*args,**kwargs):
        events.append('io');raise AssertionError('identity must fail before IO')
    with pytest.raises(r.OfficialStop):
        r.run_official(repo='/not-a-store',compute='/not-a-compute',approval_raw=core.encode(value),
            approval_digest=core.sha(core.encode(value)),request_id='1-1',key='synthetic-key',
            store_token='synthetic-token',clock=lambda:now,transport=forbidden,
            surface_guard=forbidden,visibility=forbidden,retain=forbidden)
    assert events==[]

def test_unbound_hosted_execution_surface_is_rejected(tmp_path):
    r=runner()
    with pytest.raises(r.OfficialStop):
        r.execution_guard(tmp_path,tmp_path,'c'*40,'1-1',env={})

def test_public_gld_identity_allows_only_capture_profile():
    r=runner();registry=json.loads(r.REGISTRY.read_bytes())
    gld=next(p for p in registry['profiles'] if p['symbol']=='GLD')
    assert gld['capture_identity_verified'] is True
    assert (gld['exchange'],gld['profile_mic_code'],gld['type'],gld['country'])==('NYSE','ARCX','ETF','United States')
    assert gld['url']=='https://twelvedata.com/markets/163377/etf/nyse/gld'
    profiles=r.qualify_registry(registry,core.sha(r.REGISTRY.read_bytes()),for_append=False)
    assert (profiles['GLD']['exchange'],profiles['GLD']['mic_code'])==('NYSE','ARCX')

def test_public_gld_identity_does_not_qualify_response_mic_or_legacy_basis():
    r=runner();registry=json.loads(r.REGISTRY.read_bytes())
    gld=next(p for p in registry['profiles'] if p['symbol']=='GLD')
    assert gld['response_mic_equivalence_verified'] is False
    assert gld['legacy_basis_equivalence_verified'] is False
    with pytest.raises(r.OfficialStop):
        r.qualify_registry(registry,core.sha(r.REGISTRY.read_bytes()),for_append=True)

from test_twelve_symbol_append import data

def test_twelve_publication_uses_closed_module_not_scope_dict(data):
    from scripts import twelve_symbol_append as twelve
    r=runner();base,profiles,responses,pins,contract=data
    scope=contract['targets'][0];lineage={'ingest_kind':twelve.INGEST_KIND}
    changes,conflicts=twelve.prepare_candidate(base,'GLD',responses['GLD'],
        profiles['GLD']['request'],lineage,pins['GLD'],scope)
    result,manifest=r.publication_changes(base,changes,'GLD',lineage,conflicts,'2026-11-11',
        '771-1',datetime(2026,11,11,13,tzinfo=timezone.utc),scope)
    assert manifest in result and len(result)>len(changes)
    assert json.loads(result[manifest])['stage']==twelve.INGEST_KIND

import copy
from pathlib import Path
from scripts import run_twelve_symbol_append as r
from scripts import twelve_symbol_append as twelve
def git(root,*args):
    import subprocess
    return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()


def install_private_contract(store,remote,baseline,pins,template):
    contract=copy.deepcopy(template);contract['baseline_store_sha']=baseline
    raw=core.encode(contract);digest=core.sha(raw)
    path=store/f'capture-contracts/{digest}.json';path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
    git(store,'add',str(path.relative_to(store)))
    git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','private synthetic scope')
    head=git(store,'rev-parse','HEAD');git(store,'push','-q','origin','HEAD:main')
    return head,digest


@pytest.fixture
def real_fixture(tmp_path, monkeypatch, data):
    import shutil
    files, profiles, responses, pins, template = data
    registry=json.loads(r.REGISTRY.read_bytes())
    for p in registry['profiles']:
        p.update(exchange='SYNTHETIC',profile_mic_code='TEST',url='https://twelvedata.com/markets/SYNTHETIC',capture_identity_verified=True)
    registry_path=tmp_path/'synthetic-registry.json';registry_path.write_bytes(core.encode(registry))
    monkeypatch.setattr(r,'REGISTRY',registry_path)
    store = tmp_path/'store';store.mkdir()
    remote = tmp_path/'store.git'
    git(tmp_path,'init','--bare','-q',str(remote))
    git(store,'init','-q','-b','main')
    for path,raw in files.items():
        dest=store/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
    git(store,'add','.')
    git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','synthetic base')
    base=git(store,'rev-parse','HEAD');git(store,'remote','add','origin',str(remote));git(store,'push','-q','origin','HEAD:main')
    compute=tmp_path/'compute';compute.mkdir()
    shutil.copytree(r.ROOT/'config',compute/'config')
    git(compute,'init','-q','-b','main');git(compute,'add','.')
    git(compute,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','synthetic compute')
    compute_remote=tmp_path/'compute.git';git(tmp_path,'init','--bare','-q',str(compute_remote))
    git(compute,'remote','add','origin',str(compute_remote));git(compute,'push','-q','origin','HEAD:main')
    compute_sha=git(compute,'rev-parse','HEAD')
    base,contract_sha=install_private_contract(store,remote,base,pins,template)
    request=r.example_request(compute=compute_sha,registry_sha=core.sha(r.REGISTRY.read_bytes()),day='2026-11-11',
                              store=base,contract_sha=contract_sha,cutoff=core.DATES[-1])
    real_profiles=r.qualify_registry(json.loads(r.REGISTRY.read_bytes()),request['registry_sha256'],for_append=False)
    for symbol,raw in responses.items():
        doc=json.loads(raw)
        doc['meta']['exchange']=real_profiles[symbol]['exchange']
        doc['meta']['mic_code']=real_profiles[symbol]['mic_code']
        responses[symbol]=core.encode(doc)
    state={'minute':0,'requests':[],'base_reads':0}
    def clock(): return datetime(2026,11,11,13,state['minute'],10,tzinfo=timezone.utc)
    class FakeDatetime:
        @staticmethod
        def now(tz=None): return clock()
    h=r.admission.load_helpers()
    monkeypatch.setattr(h.collectors,'datetime',FakeDatetime)
    actual_tree=r.market_tree
    def tree(*args): state['base_reads']+=1;return actual_tree(*args)
    monkeypatch.setattr(r,'market_tree',tree)
    def guard(compute,repo,sha,request_id):
        assert git(compute,'rev-parse','HEAD')==sha
        assert git(compute,'status','--porcelain')==''
        assert git(compute_remote,'rev-parse','main')==sha
    def transport(request,key,access,before):
        before();symbol=request['symbol'];state['requests'].append(symbol)
        raw=responses[symbol]
        state['minute']+=1  # Injected clock; no real wait, debit+guard stayed in same minute.
        return raw
    kwargs=dict(repo=store,compute=compute,approval_raw=core.encode(request),
        approval_digest=core.sha(core.encode(request)),request_id='771-1',key='synthetic-provider-key',
        store_token='synthetic-store-token',clock=clock,transport=transport,surface_guard=guard,
        visibility=lambda token: None)
    return store,remote,base,kwargs,state,responses

def test_real_git_debit_retention_fact_terminal_chain_all_twelve(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    result=r.run_official(**kwargs)
    assert result['requests']==12 and sum(x['new_rows'] for x in result['outcomes'])==59
    assert state['base_reads']==1
    assert git(remote,'rev-parse','main')==result['store_commit']
    files=r.market_tree(store,result['store_commit'])
    for symbol in twelve.SYMBOLS:
        assert len(twelve.read_series(files,symbol))==265
        prefix=f"capture-evidence/{kwargs['approval_digest']}/{symbol}/"
        terminal=json.loads((store/(prefix+'terminal.json')).read_bytes())
        rawcommit=terminal['retention']['store_commit_sha']
        assert git(remote,'rev-parse','refs/tags/market-data-read/'+rawcommit)==rawcommit
        assert (store/(prefix+'response.raw')).read_bytes()==responses[symbol]
    ledger=json.loads((store/'data/market-data/collector-state/2026-11/2026-11-11-twelve-data-budget.json').read_bytes())
    assert ledger['credits']==12 and max(ledger['minutes'].values())==1
    with pytest.raises(r.OfficialStop): r.run_official(**kwargs)
    assert len(state['requests'])==12


def test_real_git_identity_rejection_is_retained_and_other_symbols_continue(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    doc=json.loads(responses['GLD']);doc['meta']['mic_code']='XNAS'
    responses['GLD']=core.encode(doc)
    result=r.run_official(**kwargs)
    assert result['outcomes'][0]==dict(symbol='GLD',status='REJECTED',reason='IDENTITY_INVALID',new_rows=0)
    assert result['outcomes'][1]['status']=='APPENDED'
    assert len(twelve.read_series(r.market_tree(store,result['store_commit']), 'GLD'))==260


@pytest.mark.parametrize('point',['debit','transport','raw','facts','terminal'])
def test_unknown_real_boundary_stops_without_retry_or_next_symbol(real_fixture,monkeypatch,point):
    store,remote,base,kwargs,state,responses=real_fixture
    attempts=[]
    if point in ('debit','facts'):
        original=r.ExactPublisher.publish
        def publish(self,paths,**options):
            result=original(self,paths,**options)
            is_debit=paths==[self.budget_relative]
            if is_debit == (point=='debit'):
                attempts.append(point);raise OSError('lost publication acknowledgment')
            return result
        monkeypatch.setattr(r.ExactPublisher,'publish',publish)
    elif point=='transport':
        original=kwargs['transport']
        def transport(*args):
            original(*args);attempts.append(point);raise TimeoutError('unknown provider completion')
        kwargs['transport']=transport
    else:
        original=r.retain_capture_object
        def retain(**args):
            result=original(**args)
            if args['name']==('response.raw' if point=='raw' else 'terminal.json'):
                attempts.append(point);raise OSError('lost retention acknowledgment')
            return result
        kwargs['retain']=retain
    with pytest.raises(r.OfficialStop): r.run_official(**kwargs)
    assert attempts==[point]
    assert state['requests']==([] if point=='debit' else ['GLD'])
    assert not (store/f"capture-evidence/{kwargs['approval_digest']}/IBIT").exists()
    if point in ('debit','transport','raw','facts'):
        assert not (store/f"capture-evidence/{kwargs['approval_digest']}/GLD/terminal.json").exists()

