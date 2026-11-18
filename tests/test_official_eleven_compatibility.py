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

