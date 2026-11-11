"""Official orchestration tested only against local bare Git and fake HTTP."""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest
import yaml

from scripts import run_eleven_symbol_append as r
from scripts import eleven_symbol_append as core
from scripts.simulate_eleven_symbol_append import fixture_inputs


def test_shipped_qualification_is_blocked_before_any_io():
    registry = json.loads(r.REGISTRY.read_bytes())
    with pytest.raises(r.OfficialStop): r.qualify_registry(registry, core.sha(r.REGISTRY.read_bytes()))


def test_official_workflow_manual_main_shared_lock_and_no_artifacts():
    text = (r.ROOT / '.github/workflows/eleven-symbol-append.yml').read_text()
    doc = yaml.safe_load(text)
    triggers = doc.get('on', doc.get(True))
    assert set(triggers) == {'workflow_dispatch'}
    job = doc['jobs']['append']
    assert "github.ref == 'refs/heads/main'" in job['if']
    assert job['concurrency'] == {'group': 'market-data-collector', 'cancel-in-progress': False, 'queue': 'max'}
    assert 'upload-artifact' not in text
    assert set(triggers['workflow_dispatch']['inputs']) == {'expected_compute_sha', 'expected_receipt_commitment'}
    assert 'PRIVATE_AUTHORITY_CHANNEL_NOT_CONFIGURED' in text
    assert 'secrets.' not in text and 'actions/checkout' not in text
    assert 'approval_json' not in text and 'GITHUB_OUTPUT' not in text


def test_exact_request_and_validity_preflight():
    request = r.example_request(compute='c'*40, registry_sha='d'*64, day='2026-11-11')
    now = datetime(2026,11,11,13,tzinfo=timezone.utc)
    assert r.validate_approval(core.encode(request), core.sha(core.encode(request)), now)['expected_store_sha'] == 'a'*40
    for key, value in [('request_limit',12), ('historical_cutoff','bad-date'), ('expected_store_sha','bad-sha'), ('execution_day','2026-11-10')]:
        bad = dict(request, **{key:value})
        with pytest.raises(r.OfficialStop): r.validate_approval(core.encode(bad),core.sha(core.encode(bad)),now)


def git(root,*args):
    import subprocess
    return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()


def install_private_contract(store,remote,baseline,pins):
    contract=dict(schema_version=1,kind='eleven_symbol_append_contract',baseline_store_sha=baseline,
                  historical_cutoff=core.DATES[-1],targets=[core.synthetic_scope(s,pins[s]) for s in core.SYMBOLS])
    raw=core.encode(contract);digest=core.sha(raw)
    path=store/f'capture-contracts/{digest}.json';path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
    git(store,'add',str(path.relative_to(store)))
    git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','private synthetic scope')
    head=git(store,'rev-parse','HEAD');git(store,'push','-q','origin','HEAD:main')
    return head,digest


@pytest.fixture
def real_fixture(tmp_path, monkeypatch):
    import shutil
    files, profiles, responses, pins = fixture_inputs()
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
    base,contract_sha=install_private_contract(store,remote,base,pins)
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


def test_real_git_debit_retention_fact_terminal_chain_all_eleven(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    result=r.run_official(**kwargs)
    assert result['requests']==11 and sum(x['new_rows'] for x in result['outcomes'])==54
    assert state['base_reads']==1
    assert git(remote,'rev-parse','main')==result['store_commit']
    files=r.market_tree(store,result['store_commit'])
    for symbol in core.SYMBOLS:
        assert len(core.read_series(files,symbol))==265
        prefix=f"capture-evidence/{kwargs['approval_digest']}/{symbol}/"
        terminal=json.loads((store/(prefix+'terminal.json')).read_bytes())
        rawcommit=terminal['retention']['store_commit_sha']
        assert git(remote,'rev-parse','refs/tags/market-data-read/'+rawcommit)==rawcommit
        assert (store/(prefix+'response.raw')).read_bytes()==responses[symbol]
    ledger=json.loads((store/'data/market-data/collector-state/2026-11/2026-11-11-twelve-data-budget.json').read_bytes())
    assert ledger['credits']==11 and max(ledger['minutes'].values())==1
    with pytest.raises(r.OfficialStop): r.run_official(**kwargs)
    assert len(state['requests'])==11


def test_real_git_identity_rejection_is_retained_and_other_symbols_continue(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    doc=json.loads(responses['IBIT']);doc['meta']['mic_code']='XNAS'
    responses['IBIT']=core.encode(doc)
    result=r.run_official(**kwargs)
    assert result['outcomes'][0]==dict(symbol='IBIT',status='REJECTED',reason='IDENTITY_INVALID',new_rows=0)
    assert result['outcomes'][1]['status']=='APPENDED'
    assert len(core.read_series(r.market_tree(store,result['store_commit']),'IBIT'))==260


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
    assert state['requests']==([] if point=='debit' else ['IBIT'])
    assert not (store/f"capture-evidence/{kwargs['approval_digest']}/IWM").exists()
    if point in ('debit','transport','raw','facts'):
        assert not (store/f"capture-evidence/{kwargs['approval_digest']}/IBIT/terminal.json").exists()


def test_literal_candidate_publisher_never_executes_reference_transaction_hook(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    hook=store/'.git/hooks/reference-transaction'
    marker=store.parent/'reference-hook-ran'
    hook.write_text('#!/bin/sh\ntouch '+str(marker)+'\n');hook.chmod(0o700)
    writer=r.ExactPublisher(store,base,'2026-11-11','811-1')
    writer.pre_push_guard=lambda:None
    path=writer.safe_path(writer.budget_relative);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(core.encode(dict(trade_date='2026-11-11',credits=1,minutes={})))
    writer.publish([writer.budget_relative])
    assert not marker.exists()


def test_private_metadata_redirect_never_forwards_bearer(monkeypatch):
    import urllib.request
    import urllib.error
    observed=[]
    class Opener:
        def __init__(self,handler): self.handler=handler
        def open(self,req,timeout):
            observed.append(req.full_url)
            # urllib invokes this before constructing a redirected request.
            return self.handler.redirect_request(req,None,302,'redirect',{},'https://attacker.invalid/collect')
    monkeypatch.setattr(r.urllib.request,'build_opener',lambda handler:Opener(handler))
    with pytest.raises(r.OfficialStop): r.private_store_metadata('synthetic-store-token')
    assert observed==['https://api.github.com/repos/yuatom/stock-market-data-store']


@pytest.mark.parametrize('mutation',['main','ref','closure'])
def test_malformed_fact_receipt_stops_before_terminal_or_next_capture(real_fixture,monkeypatch,mutation):
    store,remote,base,kwargs,state,responses=real_fixture
    original=r.verify_literal_publication
    def verify(*args,**options):
        receipt=original(*args,**options)
        if mutation=='main': receipt['remote_main_sha']='0'*40
        elif mutation=='ref': receipt['durability_ref']='refs/tags/wrong'
        else: receipt['verified_required_outputs']=[]
        return receipt
    monkeypatch.setattr(r,'verify_literal_publication',verify)
    with pytest.raises(r.OfficialStop): r.run_official(**kwargs)
    assert state['requests']==['IBIT']
    assert not (store/f"capture-evidence/{kwargs['approval_digest']}/IBIT/terminal.json").exists()


@pytest.mark.parametrize('drift',['minute','day','source'])
def test_real_guard_drift_after_debit_prevents_http(real_fixture,drift):
    store,remote,base,kwargs,state,responses=real_fixture
    original_clock=kwargs['clock'];changed=[False]
    if drift=='day':
        kwargs['clock']=lambda:datetime(2026,11,12,5,tzinfo=timezone.utc) if changed[0] else original_clock()
    def transport(request,key,access,before):
        if drift=='minute':state['minute']+=1
        elif drift=='day':changed[0]=True
        else:(kwargs['compute']/'drift.txt').write_text('source changed')
        before()
        state['requests'].append(request['symbol'])
        raise AssertionError('guard must reject before HTTP')
    kwargs['transport']=transport
    with pytest.raises(r.OfficialStop):r.run_official(**kwargs)
    assert state['requests']==[]
    ledger=json.loads((store/'data/market-data/collector-state/2026-11/2026-11-11-twelve-data-budget.json').read_bytes())
    assert ledger['credits']==1
    assert not (store/f"capture-evidence/{kwargs['approval_digest']}/IBIT/response.raw").exists()


def test_staged_unapproved_path_prevents_fact_push(real_fixture,monkeypatch):
    store,remote,base,kwargs,state,responses=real_fixture
    original=r.publication_changes
    def changes(*args):
        result=original(*args)
        (store/'unauthorized.txt').write_text('synthetic unexpected change')
        git(store,'add','unauthorized.txt')
        return result
    monkeypatch.setattr(r,'publication_changes',changes)
    with pytest.raises(r.OfficialStop):r.run_official(**kwargs)
    assert state['requests']==['IBIT']
    assert 'unauthorized.txt' not in git(remote,'ls-tree','-r','--name-only','main').splitlines()
    assert not (store/f"capture-evidence/{kwargs['approval_digest']}/IBIT/terminal.json").exists()


def test_terminal_bad_readback_stops_after_confirmed_fact_without_next_capture(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    def retain(**args):
        result=r.retain_capture_object(**args)
        if args['name']=='terminal.json':result['blob_sha']='0'*40
        return result
    kwargs['retain']=retain
    with pytest.raises(r.OfficialStop):r.run_official(**kwargs)
    assert state['requests']==['IBIT']
    current=git(remote,'rev-parse','main')
    assert len(core.read_series(r.market_tree(store,current),'IBIT'))==265


def test_all_basis_rejections_are_blocked_with_eleven_durable_terminals(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    for symbol,raw in responses.items():
        doc=json.loads(raw);doc['values'][0]['open']='100.01';responses[symbol]=core.encode(doc)
    result=r.run_official(**kwargs)
    assert result['status']=='BLOCKED'
    assert result['appended_count']==0 and result['rejected_count']==11
    assert result['durable_terminal_receipts_confirmed'] is True
    assert result['requests']==11
    changed=git(store,'diff','--name-only',base,result['store_commit'],'--','data/market-data').splitlines()
    assert changed==['data/market-data/collector-state/2026-11/2026-11-11-twelve-data-budget.json']
    for symbol in core.SYMBOLS:
        terminal=store/f"capture-evidence/{kwargs['approval_digest']}/{symbol}/terminal.json"
        assert json.loads(terminal.read_bytes())['publication'] is None


def test_partial_cli_returns_nonzero_without_losing_certain_outcome(real_fixture,monkeypatch,capsys):
    store,remote,base,kwargs,state,responses=real_fixture
    monkeypatch.setattr(r.admission,'utc_now',kwargs['clock'])
    monkeypatch.setattr(r,'run_official',lambda **unused:dict(status='PARTIAL',appended_count=10,rejected_count=1,
                       durable_terminal_receipts_confirmed=True))
    code=r.main(['--approval-json',kwargs['approval_raw'].decode(),'--approval-sha256',kwargs['approval_digest'],
                 '--store-repo',str(store),'--request-id','771-1'])
    assert code==2
    assert json.loads(capsys.readouterr().out)['status']=='PARTIAL'


def test_one_market_read_with_five_hundred_unrelated_immutable_files(real_fixture,monkeypatch):
    import time
    store,remote,base,kwargs,state,responses=real_fixture
    for number in range(500):
        path=store/f'data/market-data/benchmark-fixtures/{number:04d}.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(core.encode({'synthetic_only':True,'payload':'x'*512,'number':number}))
    git(store,'add','.')
    git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','large synthetic fixture')
    base=git(store,'rev-parse','HEAD');git(store,'push','-q','origin','HEAD:main')
    approval=json.loads(kwargs['approval_raw'])
    old_contract=json.loads((store/f"capture-contracts/{approval['private_contract_sha256']}.json").read_bytes())
    pins={x['symbol']:x['files'] for x in old_contract['targets']}
    head,digest=install_private_contract(store,remote,base,pins)
    approval.update(expected_store_sha=head,private_contract_sha256=digest)
    kwargs.update(approval_raw=core.encode(approval),approval_digest=core.sha(core.encode(approval)))
    started=time.monotonic();result=r.run_official(**kwargs);elapsed=time.monotonic()-started
    assert result['status']=='COMPLETE' and state['base_reads']==1
    assert len(git(store,'ls-tree','-r','--name-only','HEAD','--','data/market-data/benchmark-fixtures').splitlines())==500
    print(f'500-file synthetic fixture: {elapsed:.2f}s; full market reads: {state["base_reads"]}')


@pytest.mark.parametrize('mutation',['digest','extra_commit','scope_count','scope_dates','blob','duplicate_symbol','extra_path'])
def test_private_contract_or_ancestry_drift_stops_before_claim(real_fixture,monkeypatch,mutation):
    store,remote,base,kwargs,state,responses=real_fixture
    approval=json.loads(kwargs['approval_raw'])
    if mutation=='digest':
        approval['private_contract_sha256']='0'*64
    elif mutation=='extra_commit':
        git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','unreviewed advance')
        git(store,'push','-q','origin','HEAD:main');approval['expected_store_sha']=git(store,'rev-parse','HEAD')
    else:
        original=json.loads((store/f"capture-contracts/{approval['private_contract_sha256']}.json").read_bytes())
        # Create a new explicit candidate contract from the old approved head.
        # Mutations must still be rejected despite a matching new approval digest.
        contract=dict(original,baseline_store_sha=base)
        if mutation=='scope_count':contract['targets'][0]['old_count']+=1
        elif mutation=='scope_dates':contract['targets'][0]['addition_dates'][0]='2099-01-01'
        elif mutation=='blob':contract['targets'][0]['files']['_index.json']='0'*40
        elif mutation=='duplicate_symbol':contract['targets'][-1]=contract['targets'][0]
        raw=core.encode(contract);digest=core.sha(raw)
        path=store/f'capture-contracts/{digest}.json';path.write_bytes(raw)
        git(store,'add',str(path.relative_to(store)))
        if mutation=='extra_path':
            (store/'unexpected.txt').write_text('synthetic extra path');git(store,'add','unexpected.txt')
        git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','synthetic malformed contract')
        git(store,'push','-q','origin','HEAD:main')
        approval.update(expected_store_sha=git(store,'rev-parse','HEAD'),private_contract_sha256=digest)
    kwargs.update(approval_raw=core.encode(approval),approval_digest=core.sha(core.encode(approval)))
    with pytest.raises(r.OfficialStop):r.run_official(**kwargs)
    assert state['requests']==[]
    assert not (store/'capture-evidence').exists()


def test_public_cli_redacts_private_runtime_results(real_fixture,monkeypatch,capsys):
    store,remote,base,kwargs,state,responses=real_fixture
    monkeypatch.setattr(r.admission,'utc_now',kwargs['clock'])
    monkeypatch.setattr(r,'run_official',lambda **unused:dict(status='COMPLETE',
        durable_terminal_receipts_confirmed=True,store_commit='PRIVATE_SENTINEL',
        outcomes=[{'private_count':123456,'volume_conflicts':['PRIVATE_DATE']}]))
    code=r.main(['--approval-json',kwargs['approval_raw'].decode(),'--approval-sha256',kwargs['approval_digest'],
                 '--store-repo',str(store),'--request-id','771-1'])
    assert code==0
    assert json.loads(capsys.readouterr().out)==dict(status='COMPLETE',durable_terminal_receipts_confirmed=True)


def test_git_replace_cannot_forge_private_contract_authority(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    approval=json.loads(kwargs['approval_raw'])
    contract=json.loads((store/f"capture-contracts/{approval['private_contract_sha256']}.json").read_bytes())
    git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','unauthorized advance')
    actual=git(store,'rev-parse','HEAD');git(store,'push','-q','origin','HEAD:main')
    forged=git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit-tree',
               git(store,'rev-parse',base+':'),'-p',contract['baseline_store_sha'],'-m','forged authority shape')
    git(store,'replace',actual,forged)
    approval['expected_store_sha']=actual
    with pytest.raises(r.OfficialStop):r.load_private_contract(store,approval)


def test_legacy_graft_file_is_not_private_contract_authority(real_fixture):
    store,remote,base,kwargs,state,responses=real_fixture
    (store/'.git/info/grafts').write_text('# synthetic untrusted graph override\n')
    with pytest.raises(r.OfficialStop):r.load_private_contract(store,json.loads(kwargs['approval_raw']))


def test_external_graft_cannot_forge_private_contract_authority(real_fixture, monkeypatch, tmp_path):
    store,remote,base,kwargs,state,responses=real_fixture
    approval=json.loads(kwargs['approval_raw'])
    contract=json.loads((store/f"capture-contracts/{approval['private_contract_sha256']}.json").read_bytes())
    git(store,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','unauthorized advance')
    actual=git(store,'rev-parse','HEAD')
    graft=tmp_path/'external-graft'
    graft.write_text(actual+' '+contract['baseline_store_sha']+'\n')
    monkeypatch.setenv('GIT_GRAFT_FILE',str(graft))
    approval['expected_store_sha']=actual
    with pytest.raises(r.OfficialStop):r.load_private_contract(store,approval)


def test_custom_replace_namespace_is_rejected(real_fixture, monkeypatch):
    store,remote,base,kwargs,state,responses=real_fixture
    monkeypatch.setenv('GIT_REPLACE_REF_BASE','refs/custom-replacements/')
    with pytest.raises(r.OfficialStop):r.load_private_contract(store,json.loads(kwargs['approval_raw']))
